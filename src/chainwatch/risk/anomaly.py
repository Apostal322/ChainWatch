"""Layer B: behavioural anomaly detection (interpretable, non-parametric, point-in-time).

For each transaction, a handful of observable measurements are compared with a rolling
*reference window*: the last ``window`` transactions this collector fetched **before** it
(never later ones). For every measurement the detector reports the empirical tail
probability ``p = (#reference values at least as extreme + 1) / (n + 1)`` in the
measurement's direction, and a robust z-score ``(x - median) / (1.4826 * MAD)``.

* A measurement is *flagged* when ``p <= alpha`` and ``|robust z| >= min_z`` (the z-score
  guard avoids flagging ties of discrete counts).
* ``ANOMALY`` level ``strong``: flagged measurements in two or more *evidence groups*
  (size: vsize, inputs, outputs; fee; value; output shape: dust, equal values, script mix).
  Otherwise ``notable``. vsize is close to a linear function of the input and output
  counts, so a fan-out flags vsize too: robust-tail/2 counted that one fact twice and, on
  realistic correlated noise, raised about 1.2 priority-3 alert groups per hour at 12
  fetches/min (robust-tail/3 counts groups). Before robust-tail/2, one value outside the
  reference range also counted as strong (with a small window every flag is outside it).
* ``alpha`` is per measurement: with eight measurements, about 3-4 x alpha of the
  transactions get at least one flag under the null (at most 8 x alpha).
* ``INSUFFICIENT_BASELINE``: fewer than ``min_reference`` reference transactions.

The score is a tail probability under the sampled recent traffic, **not** a probability of
fraud. Unusual is not illicit: batch payouts, consolidations and CoinJoins are all unusual
and legitimate. Results depend on the sample and its order; they are deterministic for a
given sequence.
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections import deque
from dataclasses import dataclass
from typing import Any

DETECTOR_VERSION = "robust-tail/3"


@dataclass(frozen=True)
class Measurement:
    name: str
    label: str  # investigative label shown to users
    direction: str  # "high" | "low" | "both"
    unit: str
    group: str  # evidence group: measurements that largely encode the same fact

    def value(self, f: dict[str, Any]) -> float | None:
        if self.name == "fee_rate":
            return f["fee_rate"]
        if self.name == "vsize":
            return float(f["vsize"])
        if self.name == "n_inputs":
            return float(f["n_inputs"])
        if self.name == "n_outputs":
            return float(f["n_outputs"])
        if self.name == "output_value_total":
            return float(f["output_value_total"])
        if self.name == "n_dust_outputs":
            return float(f["n_dust_outputs"])
        if self.name == "max_equal_value_outputs":
            v = f.get("max_equal_value_outputs")
            return None if v is None else float(v)
        if self.name == "n_output_types":
            v = f.get("n_output_types")
            return None if v is None else float(v)
        raise KeyError(self.name)


MEASUREMENTS: tuple[Measurement, ...] = (
    Measurement("fee_rate", "Unusual fee pattern", "both", "sat/vB", "fee"),
    Measurement("vsize", "Unusual transaction size", "high", "vB", "size"),
    Measurement("n_inputs", "Unusual fan-in", "high", "inputs", "size"),
    Measurement("n_outputs", "Unusual fan-out", "high", "outputs", "size"),
    Measurement(
        "output_value_total",
        "Unusual value moved (sum of outputs incl. change)",
        "high",
        "sat",
        "value",
    ),
    Measurement(
        "n_dust_outputs", "Unusual output distribution (dust)", "high", "outputs", "output_shape"
    ),
    Measurement(
        "max_equal_value_outputs",
        "Unusual output distribution (equal values)",
        "high",
        "outputs",
        "output_shape",
    ),
    Measurement(
        "n_output_types", "Unusual output script mix", "high", "script types", "output_shape"
    ),
)


@dataclass(frozen=True)
class DetectorConfig:
    window: int = 2000
    min_reference: int = 1000
    alpha: float = 0.002  # per-measurement tail probability for a flag
    min_z: float = 3.0

    def __post_init__(self) -> None:
        if not (0 < self.alpha < 1) or not math.isfinite(self.min_z) or self.min_z < 0:
            raise ValueError("alpha must be in (0, 1) and min_z a finite number >= 0")
        if self.min_reference < 1 or self.window < self.min_reference:
            raise ValueError("need 1 <= min_reference <= window")
        # with n reference values the smallest attainable tail probability is 1/(n+1), and
        # 2/(n+1) for a two-sided measurement (the fee rate): a smaller baseline could never
        # flag it
        sides = 2 if any(m.direction == "both" for m in MEASUREMENTS) else 1
        if sides / (self.min_reference + 1) > self.alpha:
            raise ValueError(
                f"min_reference {self.min_reference} cannot reach alpha {self.alpha} for a "
                f"two-sided measurement; need at least {math.ceil(sides / self.alpha) - 1}"
            )


class RobustTailDetector:
    def __init__(self, cfg: DetectorConfig | None = None) -> None:
        self.cfg = cfg or DetectorConfig()
        self.ref: dict[str, deque[float]] = {
            m.name: deque(maxlen=self.cfg.window) for m in MEASUREMENTS
        }
        self.n = 0  # transactions in the reference window

    # ------------------------------------------------------------------ state
    def update(self, f: dict[str, Any]) -> None:
        """Add a scored transaction to the reference window (call after ``score``)."""
        for m in MEASUREMENTS:
            v = m.value(f)
            if v is not None and math.isfinite(v):
                self.ref[m.name].append(v)
        self.n = min(self.cfg.window, self.n + 1)

    def warm_start(self, history: list[dict[str, Any]]) -> None:
        """Rebuild the window from stored features, oldest first (restart recovery)."""
        for f in history[-self.cfg.window :]:
            self.update(f)

    # ------------------------------------------------------------------ scoring
    def score(self, f: dict[str, Any]) -> dict[str, Any]:
        detector = {"version": DETECTOR_VERSION, "reference_size": self.n,
                    "window": self.cfg.window, "alpha": self.cfg.alpha}  # fmt: skip
        if self.n < self.cfg.min_reference:
            return {
                "status": "INSUFFICIENT_BASELINE",
                "level": None,
                "score": None,
                "indicators": [],
                "detector": detector,
                "detail": f"{self.n} of {self.cfg.min_reference} reference transactions",
            }
        flagged, measured = [], 0
        for m in MEASUREMENTS:
            x = m.value(f)
            ref = self.ref[m.name]
            if x is None or not math.isfinite(x) or len(ref) < self.cfg.min_reference:
                continue
            measured += 1
            srt = sorted(ref)
            n = len(srt)
            hi = (n - bisect_left(srt, x) + 1) / (n + 1)  # P(ref >= x)
            lo = (bisect_right(srt, x) + 1) / (n + 1)  # P(ref <= x)
            if m.direction == "high":
                p, side = hi, "high"
            elif m.direction == "low":
                p, side = lo, "low"
            else:
                p, side = (hi, "high") if hi < lo else (lo, "low")
                p = min(1.0, 2 * p)  # two-sided
            med = srt[n // 2]
            mad = sorted(abs(v - med) for v in srt)[n // 2]
            scale = 1.4826 * mad
            z = (x - med) / scale if scale > 0 else (math.inf if x != med else 0.0)
            if p <= self.cfg.alpha and abs(z) >= self.cfg.min_z:
                flagged.append(
                    {
                        "measurement": m.name,
                        "group": m.group,
                        "label": m.label,
                        "unit": m.unit,
                        "value": x,
                        "direction": side,
                        "reference_median": med,
                        "tail_probability": round(p, 6),
                        "robust_z": None if math.isinf(z) else round(z, 2),
                        "outside_reference_range": x > srt[-1] or x < srt[0],
                        "explanation": _explain(m, x, med, srt, side),
                    }
                )
        if measured == 0:
            return {"status": "INSUFFICIENT_INFORMATION", "level": None, "score": None,
                    "indicators": [], "detector": detector}  # fmt: skip
        if not flagged:
            return {"status": "NO_ANOMALY", "level": None, "score": None, "indicators": [],
                    "detector": detector}  # fmt: skip
        strong = len({i["group"] for i in flagged}) >= 2  # independent pieces of evidence
        flagged.sort(key=lambda i: i["tail_probability"])
        return {
            "status": "ANOMALY",
            "level": "strong" if strong else "notable",
            # -log10 of the smallest tail probability: larger = rarer in recent traffic
            "score": round(-math.log10(flagged[0]["tail_probability"]), 2),
            "indicators": flagged,
            "detector": detector,
        }


def _explain(m: Measurement, x: float, med: float, srt: list[float], side: str) -> str:
    # the share of reference values strictly beyond x, from the counts (the two-sided p is
    # doubled and would understate it)
    n = len(srt)
    below = bisect_left(srt, x) if side == "high" else n - bisect_right(srt, x)
    share = below / n * 100
    word = "higher" if side == "high" else "lower"
    fmt = (lambda v: f"{v:,.0f}") if m.unit not in ("sat/vB",) else (lambda v: f"{v:,.2f}")
    return (
        f"{m.name} = {fmt(x)} {m.unit} is {word} than about {share:.2f}% of the {n} "
        f"previously sampled transactions (median {fmt(med)} {m.unit})."
    )
