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
    assert r.reference == "OFAC SDN profile 9001" and r.listed_on is None  # not per address
    assert r.details["party_designated_on"] == "2022-04-05"
    assert r.details["programs"] == ["CYBER2"]
    assert snap.published_at is not None and snap.rejected[0][0].startswith("TUCs")


def test_sdn_xml_streams_from_a_file_with_bounded_memory(tmp_path):
    """The official file is ~100 MB: it is parsed as a stream from disk. A synthetic file
    with 20,000 parties (most without Bitcoin addresses) must parse with modest memory."""
    import tracemalloc

    filler = "".join(
        f'<DistinctParty FixedRef="{i}"><Profile ID="{i}"><Feature ID="f{i}" FeatureTypeID="1">'
        f"<FeatureVersion><VersionDetail>passport {i}</VersionDetail></FeatureVersion></Feature>"
        "</Profile></DistinctParty>"
        for i in range(20_000)
    )
    big = SDN_XML.replace(b"<DistinctParties>", f"<DistinctParties>{filler}".encode())
    path = tmp_path / "sdn_advanced.xml"
    path.write_bytes(big)
    already_tracing = tracemalloc.is_tracing()  # e.g. python -X tracemalloc
    tracemalloc.start()
    tracemalloc.reset_peak()
    base = tracemalloc.get_traced_memory()[0]
    snap = parse_ofac_sdn_advanced(path, retrieved_at=1.0)
    peak = tracemalloc.get_traced_memory()[1] - base
    if not already_tracing:
        tracemalloc.stop()
    assert [r.address for r in snap.records] == [LISTED]
    assert peak < 0.5 * len(big) + 5_000_000, f"peak {peak} for a {len(big)}-byte file"


def test_xml_with_dtd_or_entities_is_refused(tmp_path):
    evil = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><Sanctions/>'
    with pytest.raises(ValueError, match="DTD"):
        parse_ofac_sdn_advanced(evil)
    # a long comment before the DOCTYPE must not hide it (the check covered 64 KB before)
    padded = b'<?xml version="1.0"?><!--' + b"x" * (3 << 20) + b"-->" + evil[21:]
    path = tmp_path / "padded.xml"
    path.write_bytes(padded)
    with pytest.raises(ValueError, match="DTD"):
        parse_ofac_sdn_advanced(path)
    with pytest.raises(ValueError, match="DTD"):
        parse_ofac_sdn_advanced(padded)


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


def _trained(n=1000, seed=0) -> RobustTailDetector:
    rng = random.Random(seed)
    det = RobustTailDetector(DetectorConfig(window=2000, min_reference=1000))
    for _ in range(n):
        det.update(_f(fee_rate=rng.uniform(1, 30), n_out=rng.choice([1, 2, 2, 3]),
                      n_in=rng.choice([1, 1, 2]), vsize=rng.randint(110, 600),
                      value=rng.randint(10_000, 5_000_000)))  # fmt: skip
    return det


def test_anomaly_needs_a_baseline_and_flags_only_tails():
    det = RobustTailDetector()
    assert det.score(_f())["status"] == "INSUFFICIENT_BASELINE"
    det = _trained(1200)
    assert det.score(_f(fee_rate=10, n_out=2, vsize=300, value=200_000))["status"] == "NO_ANOMALY"
    out = det.score(_f(n_out=400, vsize=9000, value=10**11))
    assert out["status"] == "ANOMALY" and out["level"] == "strong"
    names = {i["measurement"] for i in out["indicators"]}
    assert {"n_outputs", "vsize", "output_value_total"} <= names
    top = out["indicators"][0]
    assert top["outside_reference_range"] and "previously sampled" in top["explanation"]
    assert out["score"] > 2  # -log10(tail probability), not a fraud probability


def test_detector_config_must_be_able_to_reach_alpha():
    """Regression: min_reference 200 with alpha 0.002 could never flag (1/201 > 0.002), and
    the two-sided fee rate needs 2/(n+1) <= alpha: at n = 500 a fee of 1e6 sat/vB was not
    flagged."""
    with pytest.raises(ValueError, match="cannot reach alpha"):
        DetectorConfig(min_reference=200, alpha=0.002)
    with pytest.raises(ValueError, match="two-sided"):
        DetectorConfig(window=900, min_reference=500, alpha=0.002)
    for bad in ({"alpha": 0}, {"alpha": float("nan")}, {"min_z": float("nan")},
                {"window": 0, "min_reference": 0}):  # fmt: skip
        with pytest.raises(ValueError):
            DetectorConfig(**bad)
    det = _trained(1000)
    assert det.score(_f(fee_rate=1e6))["status"] == "ANOMALY"  # reachable at min_reference


def test_strong_means_several_measurements_not_a_small_window():
    """robust-tail/1 called every single flag "strong" when the window was small (any flag is
    then outside the reference range), so pure noise raised priority-3 alerts."""
    det = _trained(1000)
    one = det.score(_f(n_out=400))  # one unusual measurement: notable, no alert
    assert one["status"] == "ANOMALY" and one["level"] == "notable"
    assert one["indicators"][0]["outside_reference_range"]
    many = det.score(_f(n_out=400, vsize=9000))
    assert many["level"] == "strong"


def test_anomaly_reference_window_excludes_the_scored_and_later_transactions():
    det = _trained(1200)
    extreme = _f(n_out=500)
    first = det.score(extreme)
    det.update(extreme)  # only after scoring, as the collector does
    assert det.n == 1201 and first["status"] == "ANOMALY"
    again = _trained(1200)
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
    rows = reader.list(limit=10, sort="id", descending=True, outcome=None)[1]
    assert all(r["risk_assessed"] for r in rows)  # committed with the observation


def test_failed_risk_write_keeps_the_observation_and_leaves_no_partial_record(
    tmp_path, monkeypatch
):
    cfg = _cfg(tmp_path)
    store = LiveStore(cfg.db_path)
    _ingest(store, [LISTED], at=1.0)
    col = Collector(cfg, store, http=httpx.AsyncClient(), rng=random.Random(0))
    col._build_engine()
    f, tx = _features("boom", out_addresses=[LISTED])

    def fail(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(rp, "save_risk", fail)
    col._store_and_assess(f, [], tx)
    reader = LiveReader(cfg.db_path)
    row = reader.get(f["txid"])
    assert row is not None and row["risk"] is None  # observation kept, no record
    assert store.db.execute("SELECT COUNT(*) FROM tx_address").fetchone()[0] == 0  # rolled back
    assert reader.alerts(None, 10) == []
    assert col.c.fetched == 1 and col.c.risk_errors == 1 and col.c.risk_assessed == 0
    listed = reader.list(limit=5, sort="id", descending=True, outcome=None)[1][0]
    assert listed["risk_assessed"] is False and listed["risk_priority"] is None


def test_alert_titles_name_the_source_without_repeating_a_mirror_label():
    from chainwatch.risk.sources import SOURCES, source_title

    assert SOURCES["ofac-sdn-mirror"].label == ""  # the mirror names no parties
    assert source_title("ofac-sdn-mirror", "") == "OFAC SDN (third-party mirror)"
    assert source_title("ofac-sdn", "LAZARUS GROUP") == "OFAC SDN (LAZARUS GROUP)"
    assert source_title("local-list", None) == "local-list"


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
    assert alert["title"].endswith("now listed by test-src (TEST LIST)")
    ev = alert["evidence"]
    assert ev["label"] == "TEST LIST" and ev["category"] == "sanctions"  # like a live match
    assert {"programs", "party_designated_on"} <= set(ev)
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
    src = s["summary"]["sources"][0]
    assert src["source_id"] == "test-src" and src["stale"] is True and src["age_s"] > 86400
    assert s["engine"]["indicator_max_age_days"] == 7.0
    assert s["engine"]["anomaly_min_reference"] == 1000
    assert s["summary"]["alerts_by_status_scope"] == "all_time"
    assert "illicit" not in json.dumps(s["layers"])
    fd = edge.get("/v1/live/findings", headers=auth).json()["findings"]
    assert fd[0]["txid"] == tid and fd[0]["screening"]["matches"][0]["address"] == LISTED
    assert fd[0]["insufficient_evidence"] is False and fd[0]["errors"] == []
    al = edge.get("/v1/live/alerts?status=new", headers=auth).json()["alerts"]
    assert al[0]["kind"] == "source_match" and al[0]["txids"] == [tid]
    assert edge.get("/v1/live/alerts?status=guilty", headers=auth).status_code == 422
    detail = edge.get(f"/v1/live/transactions/{tid}", headers=auth).json()
    assert detail["risk"]["priority"]["level"] == 1 and "lifecycle" in detail
    rows = edge.get("/v1/live/transactions?outcome=all", headers=auth).json()["rows"]
    assert rows[0]["risk_priority"] == 1 and rows[0]["risk_assessed"] is True

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

    def fake_fetch(url, timeout=120.0, dest=None):
        calls.append(url)
        if "treasury.gov" in url:
            raise httpx.ConnectError("blocked")
        dest.write_bytes(f"{LISTED}\n".encode())
        return dest

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
    monkeypatch.setattr(sources, "fetch", lambda *a, **k: (_ for _ in ()).throw(
        httpx.ConnectError("down")))  # fmt: skip
    assert live_cli.main(argv) == 1  # previous version stays in use
    assert len(calls) == 2


def test_authentic_sdn_excerpt_parses_with_real_structure():
    """A byte-identical excerpt of the official sdn_advanced.xml (DateOfIssue 2026-01-30;
    US government work, public domain; provenance in the file's header comment)."""
    from pathlib import Path

    path = Path(__file__).parent / "fixtures" / "ofac" / "sdn_advanced_excerpt.xml"
    snap = parse_ofac_sdn_advanced(path, retrieved_at=0.0)
    import datetime as dt

    assert dt.datetime.fromtimestamp(snap.published_at, dt.UTC).date().isoformat() == "2026-01-30"
    programs = {p for r in snap.records for p in r.details["programs"]}
    assert programs == {"CYBER2", "ILLICIT-DRUGS-EO14059", "RUSSIA-EO14024"}
    assert len(snap.records) == 5 and all(r.listed_on is None for r in snap.records)
    assert all(r.details["party_designated_on"] for r in snap.records)
    # the TRON-style address OFAC filed under "Digital Currency Address - XBT" is rejected
    assert [raw for raw, _ in snap.rejected] == ["TUCsTq7TofTCJRRoHk6RvhMoS2mJLm5Yzq"]


# ---------------------------------------------------------------- v0.8 audit (R-findings)
def _many(n, prefix="m"):
    return [p2wpkh_address(f"{prefix}-{i}") for i in range(n)]


def _active(store, source_id="test-src"):
    q = "SELECT COUNT(*) FROM indicators WHERE source_id = ? AND removed_at IS NULL"
    return store.db.execute(q, (source_id,)).fetchone()[0]


def test_empty_or_mass_delisting_snapshot_is_refused(tmp_path):
    """An error page, an empty body or a changed schema parsed to 0 or few records and
    silently delisted everything (afterwards a listed address screened as no match)."""
    store = _store(tmp_path)
    addrs = _many(20)
    _ingest(store, addrs, at=1.0)
    with pytest.raises(rp.SnapshotRejected, match="no valid address"):
        _ingest(store, [], at=2.0)
    with pytest.raises(rp.SnapshotRejected, match="would delist 18 of 20"):
        _ingest(store, addrs[:2], at=3.0)
    assert _active(store) == 20  # nothing changed
    assert _screener(store).screen(_addrs(outputs=[addrs[5]]), 4.0)["status"] == "SCREENING_MATCH"
    snap = parse_address_list(
        "\n".join(addrs[:2]).encode(), source_id="test-src", url="https://example.invalid/l",
        licence="t", category="sanctions", label="TEST LIST", retrieved_at=5.0,
    )  # fmt: skip
    assert rp.ingest_snapshot(store.db, snap, 5.0, allow_mass_removal=True)["removed"] == 18


def test_content_reverting_to_an_earlier_version_relists_it(tmp_path):
    """A = {X, Y}, B = {Y}, A again: the third snapshot was taken as "unchanged" (its sha256
    existed) and X stayed delisted although the current list contains it."""
    store = _store(tmp_path)
    first = _ingest(store, [LISTED, LISTED_2], at=1.0)
    _ingest(store, [LISTED_2], at=2.0)
    third = _ingest(store, [LISTED, LISTED_2], at=3.0)
    assert third["status"] == "new_version" and third["added"] == 1
    assert third["sha256"] == first["sha256"]
    assert _screener(store).screen(_addrs(outputs=[LISTED]), 4.0)["status"] == "SCREENING_MATCH"
    assert _ingest(store, [LISTED, LISTED_2], at=5.0)["status"] == "unchanged"


def test_official_list_and_its_mirror_are_one_list(tmp_path):
    """A mirror loaded once (fallback) stayed active forever: OFAC delisting X never reached
    it, and every listed address produced one alert per copy of the list."""
    store = _store(tmp_path)
    _ingest(store, [LISTED, LISTED_2], at=1.0, source_id="ofac-sdn-mirror")
    report = _ingest(store, [LISTED_2], at=2.0, source_id="ofac-sdn")  # X delisted by OFAC
    assert report["superseded_elsewhere"] == 1
    sc = _screener(store)
    assert sc.screen(_addrs(outputs=[LISTED]), 3.0)["status"] == "NO_KNOWN_SCREENING_MATCH"
    hit = sc.screen(_addrs(outputs=[LISTED_2]), 3.0)
    assert [(m["source_id"], m["authority"]) for m in hit["matches"]] == [("ofac-sdn", "ofac-sdn")]
    status = {s["source_id"]: s for s in rp.source_status(store.db)}
    assert status["ofac-sdn-mirror"]["superseded_by"] == "ofac-sdn"
    assert status["ofac-sdn"]["superseded_by"] is None
    f, tx = _features("both", out_addresses=[LISTED_2])
    rec = RiskEngine(sc, RobustTailDetector(), SupervisedLayer(None), clock=lambda: 3.0).assess(
        f, feat.extract_addresses(tx), []
    )
    assert [s["group_key"] for s in alert_specs(rec)] == [f"screening:ofac-sdn:out:{LISTED_2}"]


def test_alert_group_includes_the_role(tmp_path):
    """Dismissing a 'pays to X' alert used to suppress a later 'X spends' alert too."""
    store = _store(tmp_path)
    _ingest(store, [LISTED], at=1.0)
    eng = RiskEngine(_screener(store), RobustTailDetector(), SupervisedLayer(None),
                     clock=lambda: 2.0)  # fmt: skip
    pay, tx1 = _features("pay", out_addresses=[LISTED])
    spend, tx2 = _features("spend", in_addresses=[LISTED])
    for f, tx in ((pay, tx1), (spend, tx2)):
        for spec in alert_specs(eng.assess(f, feat.extract_addresses(tx), [])):
            rp.upsert_alert(store.db, txid=f["txid"], at=2.0, **spec)
    titles = sorted(a["title"] for a in rp.alerts(store.db))
    assert len(titles) == 2 and "pays to" in titles[0] and "spends from" in titles[1]


def test_alert_upserts_are_idempotent_beyond_the_displayed_txids(tmp_path):
    """Dedupe used the ``txids`` column, which keeps only the latest MAX_ALERT_TXIDS, so a
    re-run over an older transaction counted it again."""
    store = _store(tmp_path)
    spec = dict(group_key="g", layer="screening", kind="source_match", priority=1,
                title="t", evidence={})  # fmt: skip
    n = rp.MAX_ALERT_TXIDS + 10
    for i in range(n):
        rp.upsert_alert(store.db, txid=f"{i:064x}", at=float(i), **spec)
    for i in range(n):  # the same transactions again, e.g. a repeated retrospective pass
        rp.upsert_alert(store.db, txid=f"{i:064x}", at=float(n + i), **spec)
    (a,) = rp.alerts(store.db)
    assert a["occurrences"] == n and len(a["txids"]) == rp.MAX_ALERT_TXIDS


def test_retro_screening_covers_the_reload_window_and_is_idempotent(tmp_path):
    """A transaction assessed after a snapshot was stored but before the collector loaded it
    was missed by live and retrospective screening alike (retro only looked at
    fetched_at < available_at). Re-runs must not count anything twice, and a transaction
    that matched live gets no retrospective alert."""
    store = _store(tmp_path)
    _ingest(store, [LISTED_2], at=1.0)
    eng = RiskEngine(_screener(store), RobustTailDetector(), SupervisedLayer(None),
                     clock=lambda: 30.0)  # fmt: skip
    rows = []
    for name, outs in (("window", [LISTED]), ("live", [LISTED_2])):
        f, tx = _features(name, out_addresses=outs)
        f["fetched_at"] = 30.0  # after the new snapshot below (available at 20)
        tx_id = store.add_fetched_id(f, [])
        rec = eng.assess(f, feat.extract_addresses(tx), [])  # the old, loaded list
        rp.save_addresses(store.db, tx_id, feat.extract_addresses(tx))
        rp.save_risk(store.db, tx_id, rec)
        rows.append(rec)
    report = _ingest(store, [LISTED, LISTED_2], at=20.0)
    assert rows[0]["screening"]["status"] == "NO_KNOWN_SCREENING_MATCH"
    assert rp.retro_screen(store.db, report["version_id"], 40.0) == 1  # only the window tx
    assert rp.retro_screen(store.db, report["version_id"], 41.0) == 1
    retro = [a for a in rp.alerts(store.db) if a["retrospective"]]
    assert len(retro) == 1 and retro[0]["occurrences"] == 1  # idempotent
    assert retro[0]["evidence"]["source_version_id"] == report["version_id"]
    assert rp.risk_record(store.db, txid("window"))["screening"]["matches"] == []  # unchanged


def test_collector_retro_screens_when_it_loads_a_new_snapshot(tmp_path):
    store = _store(tmp_path)
    cfg = _cfg(tmp_path, indicator_max_age_days=3650)
    now = [1.8e9]
    _ingest(store, [LISTED_2], at=now[0] - 3600)
    col = Collector(cfg, store, http=None, clock=lambda: now[0])
    col._build_engine()
    now[0] += 20
    f, tx = _features("between", out_addresses=[LISTED])
    f["first_seen"] = f["fetched_at"] = now[0]
    col._store_and_assess(f, [], tx)  # assessed with the old list
    _ingest(store, [LISTED, LISTED_2], at=now[0] - 10)  # stored 10 s before: the window
    col._reload_indicators_if_changed()
    assert col.c.retrospective_matches == 1
    assert [a["kind"] for a in rp.alerts(store.db)] == ["retrospective_source_match"]


def test_graph_relations_screen_parents_at_the_childs_time(tmp_path):
    """The relation reused the parent's frozen screening result: a listing added after the
    parent was assessed was never related, and a delisted address still was."""
    store = _store(tmp_path)
    _ingest(store, [LISTED_2], at=1.0)  # LISTED not listed yet
    parent, ptx = _features("parent", in_addresses=[LISTED, LISTED, LISTED])
    pid = store.add_fetched_id(parent, [])
    eng0 = RiskEngine(_screener(store), RobustTailDetector(), SupervisedLayer(None),
                      clock=lambda: 2.0)  # fmt: skip
    rp.save_addresses(store.db, pid, feat.extract_addresses(ptx))
    rp.save_risk(store.db, pid, eng0.assess(parent, feat.extract_addresses(ptx), []))
    _ingest(store, [LISTED, LISTED_2], at=3.0, source_id="ofac-sdn-mirror")
    _ingest(store, [LISTED, LISTED_2], at=3.5, source_id="ofac-sdn")  # two copies of one list

    def lookup(txids):
        return rp.observed_parent_inputs(store.db, txids), rp.count_observed(store.db, txids), 1

    child, ctx = _features("child")
    eng = RiskEngine(_screener(store), RobustTailDetector(), SupervisedLayer(None), lookup,
                     clock=lambda: 4.0)  # fmt: skip
    rec = eng.assess(child, feat.extract_addresses(ctx), [parent["txid"]])
    g = rec["graph"]
    assert g["status"] == "RELATION_FOUND" and rec["priority"]["level"] == 2
    assert len(g["relations"]) == 1  # 3 inputs x 2 copies of the list -> one relation
    assert g["coverage"] == {"parents": 1, "parents_checked": 1, "parents_observed": 1}
    _ingest(store, [LISTED_2], at=5.0, source_id="ofac-sdn")  # LISTED delisted
    eng.screener = _screener(store)
    eng.clock = lambda: 6.0
    assert (
        eng.assess(child, feat.extract_addresses(ctx), [parent["txid"]])["graph"]["status"]
        == "NO_KNOWN_RELATION"
    )


def test_truncated_official_xml_falls_back_and_file_inputs_never_do(tmp_path, monkeypatch, capsys):
    from chainwatch.config import REPO_ROOT
    from chainwatch.live import cli as live_cli
    from chainwatch.risk import sources

    def fake_fetch(url, timeout=120.0, dest=None):
        if "treasury.gov" in url:
            dest.write_bytes(SDN_XML[: len(SDN_XML) // 2])  # cut off mid-download
        else:
            dest.write_bytes(f"{LISTED}\n".encode())
        return dest

    monkeypatch.setattr(sources, "fetch", fake_fetch)
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        f"extends: {REPO_ROOT / 'configs' / 'live.yaml'}\nlive:\n  db_path: {tmp_path / 'i.db'}\n"
    )
    argv = ["--config", str(cfg), "indicators", "update", "--source", "ofac-sdn",
            "--fallback", "ofac-sdn-mirror"]  # fmt: skip
    assert live_cli.main(argv) == 0  # ParseError used to escape the fallback
    out = json.loads(capsys.readouterr().out)
    assert out["source_id"] == "ofac-sdn-mirror" and "ParseError" in out["failures"][0]["error"]
    bad = tmp_path / "list.xml"
    bad.write_bytes(SDN_XML)
    with pytest.raises(SystemExit, match="--fallback cannot be combined with --file"):
        live_cli.main([*argv, "--file", str(bad)])
    with pytest.raises(SystemExit, match="needs --id"):
        live_cli.main(["--config", str(cfg), "alerts", "set-status", "--status", "resolved"])


def test_utf16_and_bom_inputs(tmp_path):
    """The DTD scan works on bytes, so a UTF-16 document could hide a DOCTYPE (and its
    entities) from it: only UTF-8 is accepted. A UTF-8 BOM no longer costs the first line."""
    doc = SDN_XML.decode().replace('<?xml version="1.0" encoding="utf-8"?>', "")
    for enc in ("utf-16", "utf-16-le", "utf-16-be"):
        with pytest.raises(ValueError, match="UTF-8"):
            parse_ofac_sdn_advanced(doc.encode(enc), retrieved_at=1.0)
    with pytest.raises(ValueError, match="UTF-8"):
        parse_ofac_sdn_advanced(b'<?xml version="1.0" encoding="latin-1"?><Sanctions/>')
    snap = parse_address_list(
        f"﻿{LISTED}\n{LISTED_2}\n".encode(), source_id="m", url="https://e.invalid/l",
        licence="t", category="sanctions", label="", retrieved_at=1.0,
    )  # fmt: skip
    assert len(snap.records) == 2 and snap.rejected == []


def test_fetch_refuses_a_redirect_through_plain_http(monkeypatch):
    from chainwatch.risk import sources

    def handler(request):
        if request.url.host == "start.example":
            return httpx.Response(302, headers={"location": "http://middle.example/x"})
        if request.url.host == "middle.example":
            return httpx.Response(302, headers={"location": "https://end.example/x"})
        return httpx.Response(200, content=b"x")

    real = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    )
    with pytest.raises(ValueError, match="non-https"):
        sources.fetch("https://start.example/list")


def test_supervised_gate_rejects_empty_or_malformed_evidence(tmp_path):
    art = tmp_path / "model.bin"
    art.write_bytes(b"x")
    import hashlib

    good = {"name": "m", "version": "1", "feature_schema": feat.FEATURE_SCHEMA_VERSION,
            "validated_for_live": True, "sha256": hashlib.sha256(b"x").hexdigest(),
            "evaluation": {"holdout": None, "primary_metrics": [], "label_source": "",
                           "time_separated": True}}  # fmt: skip
    with pytest.raises(ModelGateError, match="non-empty"):
        check_model_card(good, art)
    good["evaluation"] = "trust me"
    with pytest.raises(ModelGateError, match="object"):
        check_model_card(good, art)
    (tmp_path / "model_card.json").write_text(json.dumps({**good, "evaluation": ["x"]}))
    layer = SupervisedLayer(tmp_path)  # a malformed card abstains instead of raising
    assert layer.predict({})["status"] == "NO_VALIDATED_MODEL" and layer.gate_error
