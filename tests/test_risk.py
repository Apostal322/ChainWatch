"""Risk intelligence (docs/RISK_ENGINE.md): address validation, indicator versioning,
point-in-time screening, observed-graph relations, anomaly detection, the supervised gate,
failure isolation, alerts, lifecycle and retrospective screening. Local fixtures only."""

from __future__ import annotations

import asyncio
import json
import random
import time

import httpx
import pytest

from chainwatch.config import LiveConfig
from chainwatch.live import features as feat
from chainwatch.live.collector import Collector
from chainwatch.live.store import LiveReader, LiveStore
from chainwatch.risk import persistence as rp
from chainwatch.risk.addresses import InvalidAddress, canonical, parse_address
from chainwatch.risk.anomaly import DetectorConfig, RobustTailDetector
from chainwatch.risk.engine import RiskEngine, alert_specs
from chainwatch.risk.indicators import parse_address_list, parse_ofac_sdn_advanced
from chainwatch.risk.screening import Screener
from chainwatch.risk.supervised import ModelGateError, SupervisedLayer, check_model_card
from live_fixtures import delta, esplora_tx, p2wpkh_address, txid

LISTED = p2wpkh_address("listed-1")
LISTED_2 = p2wpkh_address("listed-2")


# ---------------------------------------------------------------- addresses
@pytest.mark.parametrize(
    "addr,kind",
    [
        ("BC1QW508D6QEJXTDG4Y5R3ZARVARY0C5XW7KV8F3T4", "p2wpkh"),  # BIP173 (upper case)
        ("bc1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qccfmv3", "p2wsh"),
        ("bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj0", "p2tr"),  # BIP350
        ("1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2", "p2pkh"),
        ("3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy", "p2sh"),
    ],
)
def test_valid_addresses_are_canonicalised(addr, kind):
    a = parse_address(addr)
    assert a.kind == kind and a.value == (addr.lower() if addr[:3].lower() == "bc1" else addr)


@pytest.mark.parametrize(
    "addr",
    [
        "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t5",  # bad bech32 checksum
        "bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqh2y7hd",  # bech32 for v1
        "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx",  # testnet
        "1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN3",  # bad base58 checksum
        "TUCsTq7TofTCJRRoHk6RvhMoS2mJLm5Yzq",  # a Tron address filed under XBT in the SDN list
        "bc1qW508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",  # mixed case
        "",
        "x" * 200,
    ],
)
def test_invalid_addresses_are_rejected(addr):
    with pytest.raises(InvalidAddress):
        parse_address(addr)
    assert canonical(addr) is None


# ---------------------------------------------------------------- indicator sources
def test_address_list_is_validated_deduplicated_and_versioned():
    content = (
        f"# header\n{LISTED}\n{LISTED.upper()}\nTUCsTq7TofTCJRRoHk6RvhMoS2mJLm5Yzq\n\n".encode()
    )
    snap = parse_address_list(
        content, source_id="s", url="https://x", licence="pd", category="sanctions", label="L"
    )
    assert [r.address for r in snap.records] == [LISTED]  # bech32 case-folded, deduplicated
    assert snap.rejected == [("TUCsTq7TofTCJRRoHk6RvhMoS2mJLm5Yzq", "not a Bitcoin mainnet "
                              "address prefix (1, 3 or bc1)")]  # fmt: skip
    assert len(snap.sha256) == 64


NS = "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/ADVANCED_XML"
# CONSTRUCTED from the documented ADVANCED_XML structure (docs/RISK_DATA_FEASIBILITY.md); the
# addresses are generated test values, the party names are fictitious.
SDN_XML = f"""<?xml version="1.0" encoding="utf-8"?>
<Sanctions xmlns="{NS}">
  <DateOfIssue><Year>2026</Year><Month>10</Month><Day>1</Day></DateOfIssue>
  <ReferenceValueSets><FeatureTypeValues>
    <FeatureType ID="344" FeatureTypeGroupID="1">Digital Currency Address - XBT</FeatureType>
    <FeatureType ID="345" FeatureTypeGroupID="1">Digital Currency Address - ETH</FeatureType>
  </FeatureTypeValues></ReferenceValueSets>
  <DistinctParties>
    <DistinctParty FixedRef="9001"><Profile ID="9001" PartySubTypeID="3">
      <Identity ID="1" FixedRef="9001" Primary="true"><Alias FixedRef="9001" AliasTypeID="1403" Primary="true">
        <DocumentedName ID="1"><DocumentedNamePart><NamePartValue NamePartGroupID="1">EXAMPLE EXCHANGE LTD</NamePartValue></DocumentedNamePart></DocumentedName>
      </Alias></Identity>
      <Feature ID="11" FeatureTypeID="344"><FeatureVersion ID="12"><VersionDetail DetailTypeID="1432">{LISTED}</VersionDetail></FeatureVersion></Feature>
      <Feature ID="13" FeatureTypeID="344"><FeatureVersion ID="14"><VersionDetail DetailTypeID="1432">TUCsTq7TofTCJRRoHk6RvhMoS2mJLm5Yzq</VersionDetail></FeatureVersion></Feature>
      <Feature ID="15" FeatureTypeID="345"><FeatureVersion ID="16"><VersionDetail DetailTypeID="1432">0x0000000000000000000000000000000000000000</VersionDetail></FeatureVersion></Feature>
    </Profile></DistinctParty>
  </DistinctParties>
  <SanctionsEntries><SanctionsEntry ID="77" ProfileID="9001" ListID="1550">
    <EntryEvent ID="78" EntryEventTypeID="1" LegalBasisID="1"><Date><Year>2022</Year><Month>4</Month><Day>5</Day></Date></EntryEvent>
    <SanctionsMeasure ID="79" SanctionsTypeID="1"><Comment>CYBER2</Comment></SanctionsMeasure>
  </SanctionsEntry></SanctionsEntries>
</Sanctions>""".encode()  # noqa: E501


def test_official_sdn_xml_parser_keeps_provenance_and_rejects_non_bitcoin():
    snap = parse_ofac_sdn_advanced(SDN_XML, retrieved_at=100.0)
    assert len(snap.records) == 1
    r = snap.records[0]
    assert r.address == LISTED and r.label == "EXAMPLE EXCHANGE LTD"
    assert r.reference == "OFAC SDN profile 9001" and r.listed_on == "2022-04-05"
    assert r.details["programs"] == ["CYBER2"]
    assert snap.published_at is not None and snap.rejected[0][0].startswith("TUCs")


def test_xml_with_dtd_or_entities_is_refused():
    evil = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><Sanctions/>'
    with pytest.raises(ValueError, match="DTD"):
        parse_ofac_sdn_advanced(evil)


def _store(tmp_path) -> LiveStore:
    return LiveStore(tmp_path / "live.sqlite")


def _ingest(store, addresses, at, source_id="test-src"):
    content = "\n".join(addresses).encode()
    snap = parse_address_list(
        content, source_id=source_id, url="https://example.invalid/list", licence="test",
        category="sanctions", label="TEST LIST", retrieved_at=at,
    )  # fmt: skip
    return rp.ingest_snapshot(store.db, snap, at)


def test_snapshots_version_availability_and_removal(tmp_path):
    store = _store(tmp_path)
    r1 = _ingest(store, [LISTED], at=1000.0)
    assert r1["status"] == "new_version" and r1["added"] == 1
    assert _ingest(store, [LISTED], at=1500.0)["status"] == "unchanged"  # freshness only
    r3 = _ingest(store, [LISTED_2], at=2000.0)
    assert r3["added"] == 1 and r3["removed"] == 1
    rows = {r[2]: r for r in rp.indicator_rows(store.db)}
    assert rows[LISTED][8] == 1000.0 and rows[LISTED][9] == 2000.0  # [available, removed)
    assert rows[LISTED_2][8] == 2000.0 and rows[LISTED_2][9] is None
    assert rp.source_status(store.db)[0]["checked_at"] == 2000.0


# ---------------------------------------------------------------- screening (point in time)
def _addrs(inputs=(), outputs=(), **kw):
    return {"inputs": list(enumerate(inputs)), "outputs": list(enumerate(outputs)),
            "inputs_without_address": kw.get("in_none", 0), "inputs_missing_prevout": 0,
            "outputs_without_address": kw.get("out_none", 0), "truncated": False}  # fmt: skip


def _screener(store, max_age_days=7.0):
    return Screener(rp.indicator_rows(store.db), rp.source_status(store.db), max_age_days * 86400)


def test_screening_is_point_in_time(tmp_path):
    store = _store(tmp_path)
    _ingest(store, [LISTED], at=1000.0)
    _ingest(store, [LISTED_2], at=2000.0)  # LISTED delisted at 2000
    sc = _screener(store)
    assert sc.screen(_addrs(outputs=[LISTED]), at=999.0)["status"] == "SCREENING_DATA_UNAVAILABLE"
    hit = sc.screen(_addrs(outputs=[LISTED]), at=1500.0)
    assert hit["status"] == "SCREENING_MATCH" and hit["matches"][0]["role"] == "out"
    # the listing was not known at 1500 for LISTED_2 and no longer active at 2500 for LISTED
    assert sc.screen(_addrs(inputs=[LISTED_2]), at=1500.0)["status"] == "NO_KNOWN_SCREENING_MATCH"
    assert sc.screen(_addrs(outputs=[LISTED]), at=2500.0)["status"] == "NO_KNOWN_SCREENING_MATCH"
    assert sc.screen(_addrs(inputs=[LISTED_2]), at=2500.0)["matches"][0]["role"] == "in"
    assert sc.screen(_addrs(out_none=2), at=2500.0)["status"] == "INSUFFICIENT_INFORMATION"
    stale = _screener(store, max_age_days=0.001).screen(_addrs(outputs=[LISTED]), at=99_999.0)
    assert stale["sources"][0]["stale"] is True


# ---------------------------------------------------------------- anomaly detection
def _f(fee_rate=5.0, n_out=2, n_in=1, vsize=200, value=100_000, **kw):
    return {
        "txid": txid(random.random()),
        "first_seen": 0.0,
        "fetched_at": 0.0,
        "fee_rate": fee_rate,
        "vsize": vsize,
        "n_inputs": n_in,
        "n_outputs": n_out,
        "output_value_total": value,
        "n_dust_outputs": kw.get("dust", 0),
        "max_equal_value_outputs": kw.get("eq", 1),
        "n_output_types": kw.get("types", 1),
    }


def _trained(n=500, seed=0) -> RobustTailDetector:
    rng = random.Random(seed)
    det = RobustTailDetector(DetectorConfig(window=1000, min_reference=500))
    for _ in range(n):
        det.update(_f(fee_rate=rng.uniform(1, 30), n_out=rng.choice([1, 2, 2, 3]),
                      n_in=rng.choice([1, 1, 2]), vsize=rng.randint(110, 600),
                      value=rng.randint(10_000, 5_000_000)))  # fmt: skip
    return det


def test_anomaly_needs_a_baseline_and_flags_only_tails():
    det = RobustTailDetector()
    assert det.score(_f())["status"] == "INSUFFICIENT_BASELINE"
    det = _trained(600)
    assert det.score(_f(fee_rate=10, n_out=2, vsize=300, value=200_000))["status"] == "NO_ANOMALY"
    out = det.score(_f(n_out=400, vsize=9000, value=10**11))
    assert out["status"] == "ANOMALY" and out["level"] == "strong"
    names = {i["measurement"] for i in out["indicators"]}
    assert {"n_outputs", "vsize", "output_value_total"} <= names
    top = out["indicators"][0]
    assert top["outside_reference_range"] and "previously sampled" in top["explanation"]
    assert out["score"] > 2  # -log10(tail probability), not a fraud probability


def test_detector_config_must_be_able_to_reach_alpha():
    """Regression: min_reference 200 with alpha 0.002 could never flag (1/201 > 0.002)."""
    with pytest.raises(ValueError, match="cannot reach alpha"):
        DetectorConfig(min_reference=200, alpha=0.002)


def test_anomaly_reference_window_excludes_the_scored_and_later_transactions():
    det = _trained(600)
    extreme = _f(n_out=500)
    first = det.score(extreme)
    det.update(extreme)  # only after scoring, as the collector does
    assert det.n == 601 and first["status"] == "ANOMALY"
    again = _trained(600)
    assert again.score(extreme) == first  # deterministic for the same history


# ---------------------------------------------------------------- supervised gate
def test_supervised_layer_abstains_and_gate_rejects_unvalidated_cards(tmp_path):
    layer = SupervisedLayer(None)
    assert layer.predict({})["status"] == "NO_VALIDATED_MODEL"
    art = tmp_path / "model.bin"
    art.write_bytes(b"x")
    card = {"name": "m", "version": "1", "feature_schema": "elliptic++/183",
            "validated_for_live": True, "evaluation": {}, "sha256": "0"}  # fmt: skip
    with pytest.raises(ModelGateError, match="live schema"):
        check_model_card(card, art)
    card["feature_schema"] = feat.FEATURE_SCHEMA_VERSION
    card["validated_for_live"] = False
    with pytest.raises(ModelGateError, match="not validated"):
        check_model_card(card, art)
    (tmp_path / "model_card.json").write_text(json.dumps(card))
    assert SupervisedLayer(tmp_path).predict({})["status"] == "NO_VALIDATED_MODEL"


# ---------------------------------------------------------------- engine
def _features(tid="t", **kw):
    tx = esplora_tx(txid(tid), **kw)
    return feat.extract(tx, txid=txid(tid), first_seen=1.0, fetched_at=2.0), tx


def test_engine_priorities_and_failure_isolation(tmp_path):
    store = _store(tmp_path)
    _ingest(store, [LISTED], at=1.0)

    class Broken(RobustTailDetector):
        def score(self, f):
            raise RuntimeError("detector exploded")

    f, tx = _features(out_addresses=[LISTED])
    eng = RiskEngine(_screener(store), Broken(), SupervisedLayer(None), clock=lambda: 10.0)
    rec = eng.assess(f, feat.extract_addresses(tx), [])
    assert rec["priority"]["level"] == 1 and rec["screening"]["status"] == "SCREENING_MATCH"
    assert rec["anomaly"]["status"] == "ERROR" and rec["errors"][0]["layer"] == "anomaly"
    assert rec["supervised"]["status"] == "NO_VALIDATED_MODEL"
    assert "not a probability" in rec["priority"]["semantics"]
    specs = alert_specs(rec)
    assert specs[0]["priority"] == 1 and LISTED in specs[0]["group_key"]


def test_unknown_is_not_negative_no_source_means_insufficient_evidence():
    f, tx = _features()
    eng = RiskEngine(Screener([], []), RobustTailDetector(), SupervisedLayer(None))
    rec = eng.assess(f, feat.extract_addresses(tx), [])
    assert rec["screening"]["status"] == "SCREENING_DATA_UNAVAILABLE"
    assert rec["priority"]["level"] is None and rec["insufficient_evidence"] is True


# ---------------------------------------------------------------- alerts
def test_alert_grouping_suppression_reopen_and_audit_log(tmp_path):
    store = _store(tmp_path)
    db = store.db
    kw = dict(group_key="g", layer="screening", kind="source_match", priority=1, title="t",
              evidence={})  # fmt: skip
    aid = rp.upsert_alert(db, txid="a", at=1, **kw)
    assert rp.upsert_alert(db, txid="a", at=2, **kw) == aid  # same tx: idempotent
    rp.upsert_alert(db, txid="b", at=3, **kw)
    assert rp.alerts(db)[0]["occurrences"] == 2
    rp.set_alert_status(db, aid, "dismissed", 4, "benign: known exchange batch")
    rp.upsert_alert(db, txid="c", at=5, **kw)
    a = rp.alerts(db)[0]
    assert a["status"] == "dismissed" and a["suppressed"] == 1  # no re-notification
    rp.set_alert_status(db, aid, "resolved", 6)
    rp.upsert_alert(db, txid="d", at=7, **kw)
    assert rp.alerts(db)[0]["status"] == "new"  # recurrence after resolution reopens
    events = [e["event"] for e in rp.alert_history(db, aid)]
    assert events == ["created", "occurrence", "status:dismissed", "suppressed",
                      "status:resolved", "reopened"]  # fmt: skip
    with pytest.raises(ValueError):
        rp.set_alert_status(db, aid, "guilty", 8)


# ---------------------------------------------------------------- end to end in the collector
def _cfg(tmp_path, **kw) -> LiveConfig:
    base = dict(enabled=True, db_path=tmp_path / "live.sqlite", sample_rate=1.0,
                fetch_per_minute=6000, fetch_burst=100, queue_max=50, reconnect_base_s=0.01,
                reconnect_max_s=0.05, heartbeat_s=0.05, stale_after_s=5.0)  # fmt: skip
    base.update(kw)
    return LiveConfig(**base)


def test_collector_screens_relates_and_tracks_lifecycle(tmp_path):
    from websockets.asyncio.server import serve

    parent, child, plain = txid("p"), txid("c"), txid("x")
    payloads = {
        parent: esplora_tx(parent, in_addresses=[LISTED]),  # spends from a listed address
        child: esplora_tx(child, parents=[parent]),  # spends the parent's output
        plain: esplora_tx(plain),
    }
    rest = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=payloads[r.url.path.rsplit("/", 1)[-1]])
        )
    )

    async def handler(ws):
        await ws.recv()
        await ws.send(delta([parent], seq=1))
        await asyncio.sleep(0.3)
        await ws.send(delta([child, plain], seq=2))
        await asyncio.sleep(0.3)
        await ws.send(
            json.dumps(
                {
                    "mempool-txids": {
                        "sequence": 3,
                        "added": [],
                        "removed": [],
                        "mined": [plain],
                        "replaced": [],
                    }
                }
            )
        )
        await ws.wait_closed()

    async def go():
        async with serve(handler, "127.0.0.1", 0) as srv:
            cfg = _cfg(tmp_path, ws_url=f"ws://127.0.0.1:{srv.sockets[0].getsockname()[1]}")
            store = LiveStore(cfg.db_path)
            _ingest(store, [LISTED], at=time.time() - 10)
            col = Collector(cfg, store, http=rest, rng=random.Random(0))
            await col.run(duration=1.5)
            return cfg, col

    cfg, col = asyncio.run(go())
    reader = LiveReader(cfg.db_path)
    p = reader.get(parent)["risk"]
    assert p["screening"]["status"] == "SCREENING_MATCH" and p["priority"]["level"] == 1
    assert p["screening"]["matches"][0]["role"] == "in"
    c = reader.get(child)["risk"]
    assert c["screening"]["status"] == "NO_KNOWN_SCREENING_MATCH"
    assert c["graph"]["status"] == "RELATION_FOUND" and c["priority"]["level"] == 2
    assert c["graph"]["relations"][0]["path"] == [LISTED, parent, "this transaction"]
    x = reader.get(plain)
    assert x["risk"]["priority"]["level"] is None and x["lifecycle"]["state"] == "mined"
    assert x["features"]["first_seen"] <= x["risk"]["evaluated_at"]  # event-time order
    kinds = {a["kind"] for a in reader.alerts(None, 10)}
    assert kinds == {"source_match", "spends_output_of_tx_funded_by_listed_address"}
    assert col.c.screening_matches == 1 and col.c.graph_relations == 1
    assert col.c.risk_assessed == 3 and col.c.risk_errors == 0


def test_retrospective_screening_does_not_rewrite_history(tmp_path):
    store = _store(tmp_path)
    _ingest(store, [LISTED_2], at=1.0)
    f, tx = _features("old", out_addresses=[LISTED])
    tx_id = store.add_fetched_id(f, [])
    eng = RiskEngine(_screener(store), RobustTailDetector(), SupervisedLayer(None),
                     clock=lambda: 3.0)  # fmt: skip
    rec = eng.assess(f, feat.extract_addresses(tx), [])
    rp.save_addresses(store.db, tx_id, feat.extract_addresses(tx))
    rp.save_risk(store.db, tx_id, rec)
    report = _ingest(store, [LISTED, LISTED_2], at=50.0)  # LISTED becomes known later
    assert rp.retro_screen(store.db, report["version_id"], 51.0) == 1
    alert = rp.alerts(store.db)[0]
    assert alert["retrospective"] is True and alert["kind"] == "retrospective_source_match"
    # the point-in-time record keeps what was known at evaluation time
    assert rp.risk_record(store.db, f["txid"])["screening"]["status"] == "NO_KNOWN_SCREENING_MATCH"


# ---------------------------------------------------------------- API (edge + remote proxy)
def _populated(tmp_path):
    from chainwatch.risk.anomaly import RobustTailDetector

    store = _store(tmp_path)
    _ingest(store, [LISTED], at=1.0)
    f, tx = _features("api", out_addresses=[LISTED])
    tx_id = store.add_fetched_id(f, [])
    now = time.time()
    eng = RiskEngine(_screener(store), RobustTailDetector(), SupervisedLayer(None),
                     clock=lambda: now)  # fmt: skip
    rec = eng.assess(f, feat.extract_addresses(tx), [])
    rp.save_addresses(store.db, tx_id, feat.extract_addresses(tx))
    rp.save_risk(store.db, tx_id, rec)
    for spec in alert_specs(rec):
        rp.upsert_alert(store.db, txid=f["txid"], at=now, **spec)
    store.heartbeat(state="connected", updated_at=time.time(), started_at=1.0)
    return store, f["txid"]


def test_risk_routes_on_the_edge_and_through_the_remote_proxy(tmp_path):
    from fastapi.testclient import TestClient

    from chainwatch.live.edge import create_edge_app
    from chainwatch.serving.live_api import RemoteBackend

    token = "k" * 48
    store, tid = _populated(tmp_path)
    live = LiveConfig(enabled=True, db_path=store.path, edge_trusted_proxy_hops=0)
    edge = TestClient(create_edge_app(live, token))
    auth = {"authorization": f"Bearer {token}"}
    assert edge.get("/v1/live/findings").status_code == 401  # token required
    s = edge.get("/v1/live/risk/summary?hours=1", headers=auth).json()
    assert (
        s["summary"]["by_priority"] == {"1": 1} and "not proof of fraud" in s["layers"]["screening"]
    )
    assert s["summary"]["sources"][0]["source_id"] == "test-src"
    fd = edge.get("/v1/live/findings", headers=auth).json()["findings"]
    assert fd[0]["txid"] == tid and fd[0]["screening"]["matches"][0]["address"] == LISTED
    al = edge.get("/v1/live/alerts?status=new", headers=auth).json()["alerts"]
    assert al[0]["kind"] == "source_match" and al[0]["txids"] == [tid]
    assert edge.get("/v1/live/alerts?status=guilty", headers=auth).status_code == 422
    detail = edge.get(f"/v1/live/transactions/{tid}", headers=auth).json()
    assert detail["risk"]["priority"]["level"] == 1 and "lifecycle" in detail
    rows = edge.get("/v1/live/transactions?outcome=all", headers=auth).json()["rows"]
    assert rows[0]["risk_priority"] == 1

    remote_live = LiveConfig(enabled=True, remote_url="http://testserver", remote_cache_s=0.0)
    be = RemoteBackend(remote_live, token, transport=edge._transport)
    assert be.findings(4, 10, None)[0]["txid"] == tid
    assert be.alerts("new", 10)[0]["priority"] == 1
    assert be.risk_summary(24)["layers"]["supervised"].startswith("No supervised model")


def test_reader_tolerates_a_database_from_before_risk_tables(tmp_path):
    import sqlite3

    from chainwatch.live.store import SCHEMA

    db = tmp_path / "old.sqlite"
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    con.close()
    reader = LiveReader(db)
    assert reader.findings(4, 10, None) == [] and reader.alerts(None, 10) == []
    assert reader.risk_summary(0.0) is None


def test_indicator_update_falls_back_and_never_breaks_on_failure(tmp_path, monkeypatch, capsys):
    from chainwatch.live import cli as live_cli
    from chainwatch.risk import sources

    calls = []

    def fake_fetch(url, timeout=120.0):
        calls.append(url)
        if "treasury.gov" in url:
            raise httpx.ConnectError("blocked")
        return f"{LISTED}\n".encode()

    monkeypatch.setattr(sources, "fetch", fake_fetch)
    cfg = tmp_path / "c.yaml"
    from chainwatch.config import REPO_ROOT

    base = REPO_ROOT / "configs" / "live.yaml"
    cfg.write_text(f"extends: {base}\nlive:\n  db_path: {tmp_path / 'i.sqlite'}\n")
    argv = ["--config", str(cfg), "indicators", "update", "--source", "ofac-sdn",
            "--fallback", "ofac-sdn-mirror"]  # fmt: skip
    assert live_cli.main(argv) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["source_id"] == "ofac-sdn-mirror" and out["failures"][0]["source"] == "ofac-sdn"
    monkeypatch.setattr(sources, "fetch", lambda url, timeout=120.0: (_ for _ in ()).throw(
        httpx.ConnectError("down")))  # fmt: skip
    assert live_cli.main(argv) == 1  # previous version stays in use
    assert len(calls) == 2
