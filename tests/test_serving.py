"""Phase 3 serving: registry safety, inference consistency, replay causality, API contract."""

from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from chainwatch.backtest.runner import run_backtest
from chainwatch.backtest.schedule import StrategySpec
from chainwatch.data import schema as S
from chainwatch.data.loader import load_processed, prepare
from chainwatch.data.synthetic import make_raw_frames
from chainwatch.features.preprocessing import fill_degrees_from_edges
from chainwatch.serving.app import ServiceState, create_app, research_results
from chainwatch.serving.export import export_registry, snapshot_version
from chainwatch.serving.registry import CARD_FILE, MODEL_FILE, RegistryError, load_bundle
from chainwatch.serving.security import SECURITY_HEADERS
from chainwatch.training.pipeline import evaluate, train
from chainwatch.utils import read_json, write_json
from conftest import make_test_config, write_raw


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("serving")
    raw_dir = write_raw(tmp / "raw", make_raw_frames(12, 60, 0))
    cfg = make_test_config(tmp, raw_dir)
    prepare(cfg.paths.raw_dir, cfg.paths.processed_dir)
    evaluate(cfg, train(cfg, "xgboost"))
    export_registry(cfg)
    state = ServiceState(cfg)
    return cfg, state


@pytest.fixture(scope="module")
def client(env):
    cfg, state = env
    with TestClient(create_app(cfg, state=state)) as c:
        yield c


def _labelled_ids(state, step):
    f = state.store.step_frame(step)
    return f[f[S.LABEL] != S.LABEL_UNKNOWN][S.TX_ID].tolist()


# --------------------------------------------------------------------------- registry
def test_registry_contents_and_causality(env):
    _, state = env
    meta = state.registry_meta
    assert meta["data_mode"] == "elliptic++"  # synthetic marker absent in this fixture
    assert len(meta["snapshots"]) == 8 and meta["phase1_reference"] == "phase1-xgboost-all"
    for v in meta["snapshots"]:
        card = state.bundles[v].card
        assert card["train_steps"][1] == card["trained_at_end_of_step"] - card["label_latency"]
        assert card["train_steps"][0] == 1
    for step in range(5, 13):
        assert state.engine.model_for(step, "expanding") == snapshot_version(step - 1, 1)
    assert state.engine.model_for(6, "static") == snapshot_version(4, 1)
    assert state.engine.model_for(9, "static") == snapshot_version(8, 1)


def test_tampered_model_file_is_refused(env, tmp_path):
    import shutil

    cfg, _ = env
    v = snapshot_version(4, 1)
    dst = tmp_path / v
    shutil.copytree(cfg.serving.registry_dir / v, dst)
    with open(dst / MODEL_FILE, "ab") as fh:
        fh.write(b"\0")
    with pytest.raises(RegistryError, match="hash"):
        load_bundle(tmp_path, v)


def test_library_version_mismatch_is_refused_unless_allowed(env, tmp_path):
    import shutil

    cfg, _ = env
    v = snapshot_version(5, 1)
    shutil.copytree(cfg.serving.registry_dir / v, tmp_path / v)
    card = read_json(tmp_path / v / CARD_FILE)
    card["environment"]["xgboost"] = "1.0.0"
    write_json(tmp_path / v / CARD_FILE, card)
    with pytest.raises(RegistryError, match="incompatible"):
        load_bundle(tmp_path, v)
    assert load_bundle(tmp_path, v, allow_version_mismatch=True).card["load_warnings"]


def test_service_refuses_mismatched_data(env, tmp_path):
    cfg, _ = env
    other = make_test_config(tmp_path, write_raw(tmp_path / "raw", make_raw_frames(12, 60, 1)))
    prepare(other.paths.raw_dir, other.paths.processed_dir)
    other.serving.registry_dir = cfg.serving.registry_dir  # registry built on different data
    with pytest.raises(RegistryError, match="fingerprint"):
        ServiceState(other)


# --------------------------------------------------------------------------- consistency
def test_replay_equals_phase2_backtest_predictions(env):
    """Serving must reproduce the research code path exactly (same data, params, seed)."""
    cfg, state = env
    tx = fill_degrees_from_edges(*_processed(cfg))
    bt = cfg.backtest
    for origin, end in ((4, 8), (8, 12)):
        res = run_backtest(
            tx,
            [StrategySpec("static", "static"), StrategySpec("expanding", "expanding")],
            origin,
            end,
            bt.label_latency,
            [cfg.serving.snapshot_seed],
            S.feature_columns(bt.feature_set),
            bt.xgboost,
        )
        p = res.predictions
        for policy in ("static", "expanding"):
            for step in range(origin + 1, end + 1):
                ours = state.engine.ranking(step, policy).set_index(S.TX_ID)["score"]
                theirs = p[(p.strategy == policy) & (p[S.TIME_STEP] == step)].set_index(S.TX_ID)[
                    "score"
                ]
                np.testing.assert_allclose(
                    ours.loc[theirs.index].to_numpy(), theirs.to_numpy(), rtol=0, atol=1e-7
                )


def _processed(cfg):
    ds = load_processed(cfg.paths.processed_dir)
    return ds.transactions, ds.edges


def test_feature_store_applies_training_degree_repair(env):
    cfg, state = env
    tx, edges = _processed(cfg)
    missing = tx[tx["in_txs_degree"].isna()][S.TX_ID].tolist()
    assert missing, "fixture should contain rows with missing degrees"
    repaired = fill_degrees_from_edges(tx, edges).set_index(S.TX_ID)
    cols = S.ALL_FEATURES
    pd.testing.assert_frame_equal(
        state.store.features(missing, cols), repaired.loc[missing, cols], check_names=False
    )
    # skipping the repair would feed NaN degrees (median-imputed by the pipeline) instead
    unrepaired = tx.set_index(S.TX_ID).loc[missing, S.DEGREE_FEATURES]
    assert unrepaired.isna().all().all()
    assert not state.store.features(missing, S.DEGREE_FEATURES).isna().any().any()


def test_feature_endpoint_matches_id_endpoint(client, env):
    _, state = env
    ids = _labelled_ids(state, 10)[:5]
    v = state.engine.model_for(10, "expanding")
    by_id = client.post("/v1/predict/batch", json={"tx_ids": ids}).json()["predictions"]
    feats = state.store.features(ids, S.ALL_FEATURES)
    inst = [
        {
            k: (None if (isinstance(x, float) and math.isnan(x)) else float(x))
            for k, x in row.items()
        }
        for row in feats.to_dict(orient="records")
    ]
    r = client.post("/v1/predict/features", json={"model_version": v, "instances": inst})
    assert r.status_code == 200, r.text
    np.testing.assert_allclose(r.json()["risk_scores"], [p["risk_score"] for p in by_id], atol=1e-7)
    assert "anonymised" in r.json()["feature_space"]


# --------------------------------------------------------------------------- API
def test_health_and_info(client):
    assert client.get("/health/live").json() == {"status": "ok"}
    ready = client.get("/health/ready").json()
    assert ready["status"] == "ready" and ready["models_loaded"] == 9
    info = client.get("/v1/info").json()
    assert info["label_latency_steps"] == 1 and "not evidence" in info["disclaimer"]
    assert set(info["policies"]) == {"expanding", "static", "phase1"}


def test_models_endpoints(client):
    models = client.get("/v1/models").json()
    assert len(models) == 9 and all(len(m["model_sha256"]) == 64 for m in models)
    one = client.get(f"/v1/models/{snapshot_version(6, 1)}").json()
    assert one["n_features"] == 182 and one["trained_at_end_of_step"] == 6
    r = client.get("/v1/models/nope")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


def test_ranking_sorted_and_never_maps_unknown_to_licit(client, env):
    _, state = env
    r = client.get("/v1/replay/steps/10/ranking?limit=1000").json()
    scores = [x["risk_score"] for x in r["rows"]]
    assert scores == sorted(scores, reverse=True)
    assert r["total"] == len(state.store.step_frame(10)) == len(r["rows"])
    truth = state.store.step_frame(10).set_index(S.TX_ID)[S.RAW_LABEL]
    for row in r["rows"]:
        expected = {1: "illicit", 2: "licit", 3: "unknown"}[int(truth.loc[row["tx_id"]])]
        assert row["label_status"] == expected
    unk = client.get("/v1/replay/steps/10/ranking?labels=unknown&limit=1000").json()
    assert {x["label_status"] for x in unk["rows"]} == {"unknown"}
    assert r["model_trained_at_end_of_step"] == 9 and r["labels_revealed_at_step"] == 11


def test_step_metrics_use_labelled_rows_only(client, env):
    _, state = env
    m = client.get("/v1/replay/steps/10/metrics").json()
    f = state.store.step_frame(10)
    assert m["n_labelled"] == int((f[S.LABEL] != S.LABEL_UNKNOWN).sum())
    assert m["n_unknown"] + m["n_labelled"] == m["n_transactions"]
    b = m["budgets"]["10"]
    assert b["illicit_found_all_transactions"] <= b["illicit_found_labelled_only"] + 1e-9
    tl = client.get("/v1/replay/timeline?policy=static").json()
    assert [x["step"] for x in tl] == list(range(5, 13))


def test_transaction_detail_explanation_is_exact(client, env):
    _, state = env
    tx = _labelled_ids(state, 11)[0]
    d = client.get(f"/v1/transactions/{tx}").json()
    e = d["explanation"]
    full = state.engine.explain(d["model_version"], tx, top_k=182)
    total = sum(c["contribution"] for c in full["top_contributions"]) + full["base_value"]
    assert total == pytest.approx(full["logit"], abs=1e-4)
    assert 1 / (1 + math.exp(-full["logit"])) == pytest.approx(d["risk_score"], abs=1e-5)
    assert len(e["top_contributions"]) == 10
    assert d["model_trained_at_end_of_step"] == 10 and d["label_available_at_step"] == 12


def test_neighbourhood_is_within_step(client, env):
    _, state = env
    tx = _labelled_ids(state, 7)[0]
    nb = client.get(f"/v1/transactions/{tx}/neighbourhood?hops=2").json()
    ids = {n["tx_id"] for n in nb["nodes"]}
    assert tx in ids and next(n for n in nb["nodes"] if n["tx_id"] == tx)["hop"] == 0
    steps = state.store.step_of(list(ids))
    assert set(steps) == {7}
    assert all(e["source"] in ids and e["target"] in ids for e in nb["edges"])


def test_batch_partial_errors_order_and_temporal_validity(client, env):
    _, state = env
    a, b = _labelled_ids(state, 9)[:2]
    r = client.post("/v1/predict/batch", json={"tx_ids": [b, 999, a]}).json()
    assert [p["tx_id"] for p in r["predictions"]] == [b, a]
    assert r["errors"][0]["tx_id"] == 999 and r["errors"][0]["code"] == "unknown_transaction"
    assert all(p["temporally_valid"] for p in r["predictions"])
    # explicit future model -> scored, but flagged as not a valid historical replay
    fut = client.post(
        "/v1/predict/batch", json={"tx_ids": [a], "model_version": snapshot_version(11, 1)}
    ).json()["predictions"][0]
    assert fut["temporally_valid"] is False and "look-ahead" in fut["note"]


@pytest.mark.parametrize(
    "body, field",
    [
        ({"tx_ids": []}, "tx_ids"),
        ({"tx_ids": [1, 1]}, "tx_ids"),
        ({"tx_ids": [1], "bogus": 1}, "bogus"),
        ({"tx_ids": [1], "policy": "yolo"}, "policy"),
        ({"tx_ids": ["abc"]}, "tx_ids"),
    ],
)
def test_batch_validation_errors_use_envelope(client, body, field):
    r = client.post("/v1/predict/batch", json=body, headers={"x-request-id": "abc123"})
    assert r.status_code == 422
    j = r.json()
    assert j["error"]["code"] == "validation_error" and j["request_id"] == "abc123"
    assert any(field in json.dumps(d["loc"]) for d in j["error"]["details"])
    assert r.headers["x-request-id"] == "abc123"


def test_batch_too_large(client):
    r = client.post("/v1/predict/batch", json={"tx_ids": list(range(1, 52))})
    assert r.status_code == 413 and r.json()["error"]["code"] == "payload_too_large"


def test_feature_vector_validation(client, env):
    _, state = env
    v = state.engine.model_for(10, "expanding")
    base = {c: 0.0 for c in S.ALL_FEATURES}
    cases = [
        {k: v_ for k, v_ in base.items() if k != "Local_feature_1"},  # missing feature
        base | {"extra": 1.0},  # unexpected feature
        base | {"in_txs_degree": None},  # degree must be present
        base | {"Local_feature_9": None},  # anonymised features must be present
    ]
    for inst in cases:
        r = client.post("/v1/predict/features", json={"model_version": v, "instances": [inst]})
        assert r.status_code == 422, inst
    ok = client.post(
        "/v1/predict/features", json={"model_version": v, "instances": [base | {"fees": None}]}
    )
    assert ok.status_code == 200
    nan = client.post(
        "/v1/predict/features",
        content=json.dumps({"model_version": v, "instances": [base | {"fees": float("nan")}]}),
        headers={"content-type": "application/json"},
    )
    assert nan.status_code == 422
    assert (
        client.post(
            "/v1/predict/features", json={"model_version": "nope", "instances": [base]}
        ).status_code
        == 404
    )


def test_replay_errors(client, env):
    r = client.get("/v1/replay/steps/3/ranking")
    assert r.status_code == 400 and r.json()["error"]["code"] == "replay_unavailable"
    r = client.get("/v1/replay/steps/7/ranking?policy=phase1")  # validation steps: not OOS
    assert r.status_code == 400
    assert client.get("/v1/replay/steps/9/ranking?policy=phase1").status_code == 200
    assert client.get("/v1/transactions/999").json()["error"]["code"] == "unknown_transaction"
    assert client.get("/v1/does-not-exist").status_code == 404


def test_research_results_are_the_committed_reports():
    from chainwatch.config import REPO_ROOT

    res = research_results(REPO_ROOT / "reports")
    assert {
        "phase1_baselines",
        "phase2_per_step",
        "phase2_bootstrap_confirm",
        "phase2_verdicts",
    } <= set(res)
    p1 = {(r["model"], r["features"]): r["AP (PR-AUC)"] for r in res["phase1_baselines"]}
    assert p1[("xgboost", "all")] == pytest.approx(0.7969, abs=1e-4)


# --------------------------------------------------------------------------- Phase 4 audit
def _app_with(env, tmp_path=None, **serving_overrides):
    import dataclasses

    cfg, state = env
    sv = dataclasses.replace(cfg.serving, **serving_overrides)
    return TestClient(create_app(dataclasses.replace(cfg, serving=sv), state=state))


def test_oversized_body_rejected_before_parsing(env):
    with _app_with(env, max_body_bytes=10_000) as c:
        big = {"tx_ids": list(range(1, 3000))}
        r = c.post("/v1/predict/batch", json=big)
        assert r.status_code == 413 and r.json()["error"]["code"] == "payload_too_large"
        assert c.get("/health/live").status_code == 200  # service unaffected


def test_list_size_hard_cap_is_schema_level(client):
    r = client.post("/v1/predict/batch", json={"tx_ids": list(range(1, 1002))})
    assert r.status_code == 422  # rejected by validation, before handler code runs


def test_request_id_is_sanitised(client):
    ok = client.get("/health/live", headers={"x-request-id": "trace-123"})
    assert ok.headers["x-request-id"] == "trace-123"
    bad = client.get("/health/live", headers={"x-request-id": "x" * 500})
    assert bad.headers["x-request-id"] != "x" * 500 and len(bad.headers["x-request-id"]) == 16


def test_routing_errors_use_envelope(client):
    r = client.put("/v1/info")
    assert r.status_code == 405 and r.json()["error"]["code"] == "method_not_allowed"
    assert client.get("/v1/nope").json()["error"]["code"] == "not_found"


def test_security_headers(client):
    h = client.get("/v1/info").headers
    assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY"


def test_rate_limit_returns_429_and_exempts_health(env):
    with _app_with(env, rate_limit_per_minute=60) as c:  # burst of 15
        codes = [c.get("/v1/info").status_code for _ in range(30)]
        assert codes[0] == 200 and 429 in codes
        r = c.get("/v1/info")
        assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
        assert r.json()["error"]["code"] == "rate_limited"
        assert c.get("/health/ready").status_code == 200


def test_token_bucket_refills():
    from chainwatch.serving.security import RateLimitMiddleware

    rl = RateLimitMiddleware(app=None, per_minute=60, burst=2)
    assert rl.allow("a", 0.0)[0] and rl.allow("a", 0.0)[0]
    ok, retry = rl.allow("a", 0.0)
    assert not ok and retry == pytest.approx(1.0)
    assert rl.allow("a", 1.01)[0]  # one token per second at 60/min
    assert rl.allow("b", 0.0)[0]  # buckets are per client


def test_rate_limit_client_key_cannot_be_spoofed():
    from chainwatch.serving.security import RateLimitMiddleware

    def scope(*xff):
        return {"client": ("10.0.0.9", 1), "headers": [(b"x-forwarded-for", v) for v in xff]}

    direct = RateLimitMiddleware(app=None, per_minute=60)
    assert direct._client(scope(b"1.2.3.4")) == "10.0.0.9"  # header ignored by default
    proxied = RateLimitMiddleware(app=None, per_minute=60, trusted_proxy_hops=1)
    # the platform proxy appends the real peer; client-supplied entries sit to its left
    assert proxied._client(scope(b"6.6.6.6, 203.0.113.7")) == "203.0.113.7"
    assert proxied._client(scope(b"6.6.6.6", b"203.0.113.7")) == "203.0.113.7"
    assert proxied._client(scope(b"203.0.113.7")) == "203.0.113.7"
    two = RateLimitMiddleware(app=None, per_minute=60, trusted_proxy_hops=2)
    assert two._client(scope(b"6.6.6.6, 203.0.113.7, 198.51.100.1")) == "203.0.113.7"
    assert two._client(scope(b"203.0.113.7")) == "10.0.0.9"  # too few hops: fall back to peer


def test_docs_can_be_disabled_for_public_demo(env):
    with _app_with(env, expose_docs=False) as c:
        assert c.get("/docs").status_code == 404
        assert c.get("/openapi.json").status_code == 200


def test_spa_routing_is_exact(env, tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>app</html>")
    (dist / "assets" / "a.js").write_text("js")
    with _app_with(env, frontend_dist=dist) as c:
        assert c.get("/").text == "<html>app</html>"
        assert "content-security-policy" in c.get("/").headers
        assert c.get("/some/client/route").text == "<html>app</html>"
        assert c.get("/assets/a.js").text == "js"
        assert c.get("/v1").status_code == 404 and c.get("/health").status_code == 404
        assert c.get("/favicon.ico").status_code == 404
        assert c.get("/docsx").status_code == 200  # only exact segments are reserved
        trav = c.get("/..%2F..%2Fpyproject.toml")  # traversal never serves files outside dist
        assert trav.status_code == 404 and "[project]" not in trav.text


def test_phase1_not_temporally_valid_on_its_validation_steps(client, env):
    _, state = env
    val_step_tx = _labelled_ids(state, 7)[0]  # fixture split: val = 7-8, test from 9
    test_step_tx = _labelled_ids(state, 9)[0]
    r = client.post(
        "/v1/predict/batch",
        json={"tx_ids": [val_step_tx, test_step_tx], "model_version": "phase1-xgboost-all"},
    ).json()
    valid = {p["tx_id"]: p["temporally_valid"] for p in r["predictions"]}
    assert valid == {val_step_tx: False, test_step_tx: True}


def test_registry_version_names_cannot_escape(env, tmp_path):
    from chainwatch.serving.registry import bundle_dir

    cfg, _ = env
    for bad in ("../x", "/etc/passwd", "a/b", "..", "", "x" * 200):
        with pytest.raises(RegistryError):
            bundle_dir(cfg.serving.registry_dir, bad)
    assert bundle_dir(cfg.serving.registry_dir, "snap-expanding-L1-t004").name.endswith("t004")


def test_load_registry_checks_each_card(env, tmp_path):
    import shutil

    from chainwatch.serving.registry import load_registry

    cfg, state = env
    reg = tmp_path / "reg"
    shutil.copytree(cfg.serving.registry_dir, reg)
    v = snapshot_version(6, 1)
    fp = state.registry_meta["data_fingerprint"]
    assert len(load_registry(reg, versions=[v], expected_fingerprint=fp, expected_latency=1)) == 1
    with pytest.raises(RegistryError, match="label latency"):
        load_registry(reg, versions=[v], expected_latency=2)
    with pytest.raises(RegistryError, match="different data"):
        load_registry(reg, versions=[v], expected_fingerprint="0" * 16)
    with pytest.raises(RegistryError, match="not in the registry index"):
        load_registry(reg, versions=["snap-expanding-L9-t999"])


def test_service_routes_only_bundles_listed_in_registry_meta(env):
    _, state = env
    assert set(state.bundles) == set(state.registry_meta["snapshots"]) | {"phase1-xgboost-all"}


def test_internal_errors_carry_the_security_headers(env):
    """A 500 is sent by Starlette's outermost ServerErrorMiddleware, which bypassed the
    header hook: the answer had no nosniff, frame or HSTS headers."""
    import dataclasses

    cfg, state = env
    app = create_app(dataclasses.replace(cfg), state=state)

    @app.get("/v1/boom")
    def boom():
        raise RuntimeError("internal detail")

    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/v1/boom", headers={"x-request-id": "trace-500"})
    assert r.status_code == 500 and r.json()["error"]["code"] == "internal_error"
    assert "internal detail" not in r.text
    for k, v in SECURITY_HEADERS.items():
        assert r.headers[k] == v
    assert r.headers["x-request-id"] == "trace-500"


def test_api_answers_are_not_cached(client):
    assert client.get("/v1/info").headers["cache-control"] == "no-store"


def test_the_synthetic_demo_does_not_claim_the_research_dataset():
    """The synthetic demo's Limitations tab showed "Historical replay on the Elliptic++
    research dataset", contradicting its SYNTHETIC banner (v0.8.1 frontend audit)."""
    from types import SimpleNamespace

    from chainwatch.serving.app import DISCLAIMER, disclaimer_for

    synthetic = disclaimer_for(SimpleNamespace(registry_meta={"data_mode": "synthetic"}))
    assert "Synthetic demonstration" in synthetic and "Elliptic++ research dataset" not in synthetic
    assert disclaimer_for(SimpleNamespace(registry_meta={"data_mode": "elliptic++"})) == DISCLAIMER
