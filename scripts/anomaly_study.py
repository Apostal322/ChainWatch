"""Offline study of the behavioural anomaly layer (docs/RISK_ENGINE.md section 6).

    python scripts/anomaly_study.py --db data/live/live.sqlite --out reports/live/anomaly_study

Replays a collected live database in fetch order (point in time: every transaction is
scored only against transactions fetched before it) and compares:

* ``robust-tail``: the production detector (chainwatch.risk.anomaly), default settings;
* ``isolation-forest``: scikit-learn IsolationForest refitted every ``--refit`` transactions
  on the previous ``--window`` (log-transformed measurements), flagging the same overall
  rate as robust-tail so the two are compared at equal alert volume;
* ``fixed-rules``: the existing rule-based flags (fan-out >= 50, consolidation, dust).

Reported: flag rate and alerts per day, hour-to-hour stability (coefficient of variation),
sensitivity to window and alpha, measurement drift between the first and second half
(two-sample KS), agreement between methods (Jaccard of flagged sets, Spearman of scores),
what each method flags (by measurement), CPU cost per transaction, and how known benign
patterns (batch payout, consolidation, equal-output CoinJoin-like) injected into the stream
are treated. There are no labels: nothing here measures fraud detection. Needs numpy,
scipy and scikit-learn (the research environment, not the collector VM).
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import time
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from scipy.stats import ks_2samp, spearmanr
from sklearn.ensemble import IsolationForest

from chainwatch.risk.anomaly import MEASUREMENTS, DetectorConfig, RobustTailDetector


def load(db: Path) -> list[dict]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT features, flags FROM tx WHERE outcome = 'fetched' AND features IS NOT NULL "
        "ORDER BY fetched_at, id"
    ).fetchall()
    con.close()
    out = []
    for feats, flags in rows:
        f = json.loads(feats)
        f["_flags"] = json.loads(flags or "[]")
        out.append(f)
    return out


def vector(f: dict) -> list[float] | None:
    vals = [m.value(f) for m in MEASUREMENTS]
    if any(v is None for v in vals):
        return None
    return [math.log1p(max(0.0, v)) for v in vals]


def replay_robust(data: list[dict], cfg: DetectorConfig) -> tuple[list[dict], float]:
    det = RobustTailDetector(cfg)
    out = []
    t0 = time.process_time()
    for f in data:
        out.append(det.score(f))
        det.update(f)
    return out, (time.process_time() - t0) * 1000 / max(1, len(data))


def replay_iforest(data: list[dict], window: int, refit: int, min_ref: int, seed: int):
    scores: list[float | None] = []
    model = None
    t0 = time.process_time()
    vecs = [vector(f) for f in data]
    for i, v in enumerate(vecs):
        if i >= min_ref and (model is None or i % refit == 0):
            past = [x for x in vecs[max(0, i - window) : i] if x is not None]
            model = IsolationForest(n_estimators=100, random_state=seed).fit(np.array(past))
        if model is None or v is None:
            scores.append(None)
        else:  # higher = more anomalous
            scores.append(float(-model.score_samples(np.array([v]))[0]))
    return scores, (time.process_time() - t0) * 1000 / max(1, len(data))


def hourly(times: list[float], flags: list[bool]) -> dict:
    by_hour: dict[int, list[bool]] = defaultdict(list)
    for t, fl in zip(times, flags, strict=True):
        by_hour[int(t // 3600)].append(fl)
    rates = [sum(v) / len(v) for v in by_hour.values() if len(v) >= 20]
    if not rates:
        return {"hours": 0}
    mean = sum(rates) / len(rates)
    sd = (sum((r - mean) ** 2 for r in rates) / max(1, len(rates) - 1)) ** 0.5
    return {
        "hours": len(rates),
        "mean_rate": round(mean, 5),
        "cv": round(sd / mean, 3) if mean else None,
        "max_rate": round(max(rates), 5),
    }


def benign_patterns(base: dict) -> dict[str, dict]:
    """Known-benign shapes (not real transactions): what the detector does with them."""

    def make(**kw):
        f = dict(base)
        f.update(kw)
        return f

    return {
        "batch_payout_120_outputs": make(
            n_outputs=120,
            vsize=4200,
            output_value_total=5 * 10**9,
            n_output_types=3,
            max_equal_value_outputs=2,
        ),
        "consolidation_80_inputs": make(n_inputs=80, n_outputs=1, vsize=5500),
        "coinjoin_like_50_equal_outputs": make(
            n_inputs=50, n_outputs=100, vsize=8000, max_equal_value_outputs=50
        ),
        "ordinary_payment": make(),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--window", type=int, default=2000)
    ap.add_argument("--refit", type=int, default=500)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    data = load(args.db)
    if len(data) < 1000:
        raise SystemExit(f"only {len(data)} fetched transactions; need >= 1000 for a study")
    times = [f["fetched_at"] for f in data]
    span_days = max(1e-9, (times[-1] - times[0]) / 86400)

    base_cfg = DetectorConfig(window=args.window)
    robust, robust_ms = replay_robust(data, base_cfg)
    scored = [i for i, r in enumerate(robust) if r["status"] in ("ANOMALY", "NO_ANOMALY")]
    r_flag = [robust[i]["status"] == "ANOMALY" for i in scored]
    rate = sum(r_flag) / max(1, len(scored))

    iso, iso_ms = replay_iforest(data, args.window, args.refit, base_cfg.min_reference, args.seed)
    both = [i for i in scored if iso[i] is not None]
    iso_scores = np.array([iso[i] for i in both])
    cut = np.quantile(iso_scores, 1 - rate) if rate > 0 else np.inf
    i_flag = {i for i, s in zip(both, iso_scores, strict=True) if s >= cut}
    r_set = {i for i in both if robust[i]["status"] == "ANOMALY"}
    rule_set = {
        i for i in both if {"high_fan_out", "consolidation", "dust_output"} & set(data[i]["_flags"])
    }
    jacc = lambda a, b: round(len(a & b) / len(a | b), 3) if a | b else None  # noqa: E731
    r_score = [robust[i]["score"] or 0.0 for i in both]

    sensitivity = {}
    for window in (500, 1000, 2000):
        for alpha in (0.001, 0.002, 0.005):
            try:
                cfg = DetectorConfig(
                    window=max(window, 1000) if alpha == 0.001 else window,
                    min_reference=max(500, int(1 / alpha)),
                    alpha=alpha,
                )
            except ValueError:
                continue
            res, _ = replay_robust(data, cfg)
            sc = [r for r in res if r["status"] in ("ANOMALY", "NO_ANOMALY")]
            sensitivity[f"window={cfg.window},alpha={alpha}"] = round(
                sum(r["status"] == "ANOMALY" for r in sc) / max(1, len(sc)), 5
            )

    half = len(data) // 2
    drift = {}
    for m in MEASUREMENTS:
        a = [m.value(f) for f in data[:half]]
        b = [m.value(f) for f in data[half:]]
        a, b = [x for x in a if x is not None], [x for x in b if x is not None]
        if a and b:
            ks = ks_2samp(a, b)
            drift[m.name] = {"ks": round(float(ks.statistic), 4), "p": float(ks.pvalue)}

    det = RobustTailDetector(base_cfg)
    det.warm_start(data[-args.window :])
    benign = {k: det.score(v) for k, v in benign_patterns(data[-1]).items()}
    report = {
        "kind": "OFFLINE anomaly study on a collected sample; no labels, nothing about fraud",
        "database": str(args.db),
        "transactions": len(data),
        "span_days": round(span_days, 3),
        "robust_tail": {
            "scored": len(scored),
            "flag_rate": round(rate, 5),
            "flags_per_day": round(sum(r_flag) / span_days, 1),
            "strong_per_day": round(
                sum(robust[i].get("level") == "strong" for i in scored) / span_days, 1
            ),
            "hourly": hourly([times[i] for i in scored], r_flag),
            "by_measurement": dict(
                Counter(ind["measurement"] for i in scored for ind in robust[i]["indicators"])
            ),
            "cpu_ms_per_tx": round(robust_ms, 3),
        },
        "isolation_forest": {
            "flag_rate_matched": round(len(i_flag) / max(1, len(both)), 5),
            "cpu_ms_per_tx": round(iso_ms, 3),
        },
        "agreement": {
            "jaccard_robust_vs_iforest": jacc(r_set, i_flag),
            "jaccard_robust_vs_rules": jacc(r_set, rule_set),
            "spearman_scores_robust_vs_iforest": round(
                float(spearmanr(r_score, iso_scores).statistic), 3
            ),
        },
        "sensitivity_flag_rate": sensitivity,
        "drift_first_vs_second_half": drift,
        "benign_patterns": {
            k: {
                "status": v["status"],
                "level": v.get("level"),
                "flagged": [i["measurement"] for i in v["indicators"]],
            }
            for k, v in benign.items()
        },
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "anomaly_study.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
