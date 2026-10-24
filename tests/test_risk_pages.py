"""Paged, filtered findings and investigation queue (/v1/live/findings, /v1/live/alerts):
stable keyset pagination, filters, totals, concurrent writes, validation, permissions and
the remote proxy (docs/LIVE_MONITOR.md). Local constructed data only."""

from __future__ import annotations

import json
import random
import sqlite3
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from chainwatch.config import LiveConfig
from chainwatch.live.edge import create_edge_app
from chainwatch.live.store import LiveReader
from chainwatch.risk import persistence as rp
from chainwatch.risk.persistence import AlertsQuery, FindingsQuery
from chainwatch.serving.live_api import RemoteBackend
from risk_fixtures import PRIORITY, hexid, record, seed, triage_key

TOKEN = "p" * 48
AUTH = {"authorization": f"Bearer {TOKEN}"}
FINDING_STATES = set(PRIORITY)
INSERT_TX = "INSERT INTO tx (txid, first_seen, fetched_at, outcome) VALUES (?, 1, 1, 'fetched')"


@pytest.fixture(scope="module")
def data(tmp_path_factory):
    store, truth = seed(tmp_path_factory.mktemp("pages") / "live.sqlite", 3000, 600)
    store.close()
    return store.path, truth


def _edge(path):
    live = LiveConfig(enabled=True, db_path=path, edge_trusted_proxy_hops=0)
    return TestClient(create_edge_app(live, TOKEN))


def _walk(get, path: str, params: dict, key: str) -> tuple[list[dict], list[int]]:
    """Every page of a list; returns the items and the total reported with each page."""
    items, totals, cursor = [], [], None
    for _ in range(1000):
        q = {**params, **({"cursor": cursor} if cursor else {})}
        body = get(path, q)
        items += body[key]
        totals.append(body["page"]["total"])
        cursor = body["page"]["next_cursor"]
        if cursor is None:
            return items, totals
    raise AssertionError("pagination did not terminate")


def _getter(client):
    def get(path, params):
        r = client.get(path, params=params, headers=AUTH)
        assert r.status_code == 200, r.text
        return r.json()

    return get


# ---------------------------------------------------------------- findings
@pytest.mark.parametrize(
    "params,want",
    [
        ({}, lambda f: f["state"] in FINDING_STATES),
        ({"type": "source_match"}, lambda f: f["state"] == "source_match"),
        ({"type": "relation"}, lambda f: f["state"] == "relation"),
        ({"type": "anomaly"}, lambda f: f["state"].startswith("anomaly")),
        ({"type": "anomaly", "priority": 3}, lambda f: f["state"] == "anomaly_strong"),
        ({"max_priority": 2}, lambda f: f["state"] in ("source_match", "relation")),
        ({"priority": 4}, lambda f: f["state"] == "anomaly_notable"),
        ({"authority": "ofac-sdn"}, lambda f: f["source"] is not None),
        ({"authority": "someone-else"}, lambda f: False),
        ({"type": "insufficient_evidence"}, lambda f: f["state"] == "insufficient"),
        (
            {"since": 1_800_001_000, "until": 1_800_002_000},
            lambda f: f["state"] in FINDING_STATES and 1_800_001_000 <= f["at"] < 1_800_002_000,
        ),
    ],
)
def test_findings_pages_cover_exactly_the_filtered_rows_newest_first(data, params, want):
    path, truth = data
    expected = [f["id"] for f in sorted(truth.findings, key=lambda f: -f["id"]) if want(f)]
    items, totals = _walk(
        _getter(_edge(path)), "/v1/live/findings", {**params, "limit": 37}, "findings"
    )
    got = [f["id"] for f in items]
    assert got == expected  # no gap, no duplicate, stable order
    assert set(totals) == {len(expected)}  # the total is the filtered count on every page


def test_older_findings_stay_reachable(data):
    """Before v0.8.1 the dashboard showed the 50 newest findings; older ones disappeared."""
    path, truth = data
    oldest = min(f["id"] for f in truth.findings if f["state"] in FINDING_STATES)
    items, _ = _walk(_getter(_edge(path)), "/v1/live/findings", {"limit": 200}, "findings")
    assert items[-1]["id"] == oldest and len(items) > 200


def test_findings_keep_the_four_states_apart(data):
    """A source match, an anomaly, insufficient evidence and a failed assessment are never
    one list: insufficient evidence is not a finding, a failed assessment is neither."""
    path, truth = data
    get = _getter(_edge(path))
    by_id = {f["id"]: f["state"] for f in truth.findings}
    ins, _ = _walk(get, "/v1/live/findings", {"type": "insufficient_evidence"}, "findings")
    assert ins and {by_id[f["id"]] for f in ins} == {"insufficient"}
    assert all(f["priority"]["level"] is None and f["insufficient_evidence"] for f in ins)
    every, _ = _walk(get, "/v1/live/findings", {"limit": 200}, "findings")
    states = {by_id[f["id"]] for f in every}
    assert states == FINDING_STATES  # no failed, insufficient or no-finding row
    for f in every:
        assert "supervised" not in f and "probability" not in f  # no score of any kind
        assert f["priority"]["semantics"].startswith("ordinal review priority")


def test_new_findings_never_shift_later_pages(tmp_path):
    """Keyset by tx id: rows stored while someone pages appear on the first page only; a
    reader walking older pages sees every row that existed, once (offset paging duplicated
    or skipped rows whenever the collector stored new ones)."""
    store, truth = seed(tmp_path / "live.sqlite", 600, 0)
    reader = LiveReader(store.path)
    q = FindingsQuery(limit=10)
    first = reader.findings_page(q, 0.0)
    before = [
        f["id"]
        for f in sorted(truth.findings, key=lambda f: -f["id"])
        if f["state"] in FINDING_STATES
    ]
    seen = [f["id"] for f in first["items"]]
    cursor = first["next_cursor"]
    n = 0
    while cursor:
        for _ in range(3):  # the collector keeps storing findings between page requests
            n += 1
            cur = store.db.execute(
                INSERT_TX,
                (hexid(n, "new"),),
            )
            rec = record(hexid(n, "new"), "source_match", 9e9, "ofac-sdn", random.Random(n))
            rp.save_risk(store.db, cur.lastrowid, rec)
        page = reader.findings_page(FindingsQuery(limit=10, before_id=int(cursor)), 0.0)
        seen += [f["id"] for f in page["items"]]
        cursor = page["next_cursor"]
    assert seen == before
    assert reader.findings_page(q, 0.0)["total"] == len(before) + n


def test_the_total_and_the_page_come_from_one_snapshot(tmp_path):
    """COUNT and the page in separate read transactions disagreed while the collector wrote
    between them (a total smaller than the rows shown)."""
    store, _ = seed(tmp_path / "live.sqlite", 50, 0)
    stop = threading.Event()

    def writer():
        db = sqlite3.connect(store.path, isolation_level=None, timeout=5)
        i = 0
        while not stop.is_set():
            i += 1
            cur = db.execute(
                INSERT_TX,
                (hexid(i, "w"),),
            )
            rp.save_risk(
                db,
                cur.lastrowid,
                record(hexid(i, "w"), "anomaly_notable", 1.0, None, random.Random(i)),
            )
            if i >= 120:
                break
        db.close()

    t = threading.Thread(target=writer)
    t.start()
    reader = LiveReader(store.path)
    try:
        while t.is_alive():
            page = reader.findings_page(FindingsQuery(limit=200), 0.0)
            assert page["total"] == len(page["items"])
    finally:
        stop.set()
        t.join()


# ---------------------------------------------------------------- alerts
def _alert_expected(truth, pred):
    return [a["id"] for a in sorted(truth.alerts, key=triage_key) if pred(a)]


@pytest.mark.parametrize(
    "params,pred",
    [
        ({}, lambda a: True),
        ({"status": "open"}, lambda a: a["status"] in ("new", "under_review")),
        ({"status": "dismissed"}, lambda a: a["status"] == "dismissed"),
        ({"type": "source_match"}, lambda a: a["kind"] == "source_match"),
        ({"type": "retrospective"}, lambda a: a["kind"] == "retrospective_source_match"),
        ({"type": "relation"}, lambda a: a["layer"] == "graph"),
        ({"type": "anomaly"}, lambda a: a["layer"] == "anomaly"),
        ({"priority": 2}, lambda a: a["priority"] == 2),
        ({"max_priority": 2}, lambda a: a["priority"] <= 2),
        ({"authority": "ofac-sdn"}, lambda a: a["layer"] in ("screening", "graph")),
        ({"authority": "nobody"}, lambda a: False),
        (
            {"status": "new", "type": "source_match", "authority": "ofac-sdn"},
            lambda a: a["status"] == "new" and a["kind"] == "source_match",
        ),
    ],
)
def test_alert_pages_cover_exactly_the_filtered_queue_in_triage_order(data, params, pred):
    path, truth = data
    expected = _alert_expected(truth, pred)
    items, totals = _walk(
        _getter(_edge(path)), "/v1/live/alerts", {**params, "limit": 23}, "alerts"
    )
    assert [a["id"] for a in items] == expected
    assert set(totals) == {len(expected)}


def test_alert_time_range_and_newest_order(data):
    path, truth = data
    lo, hi = 1_800_000_500.0, 1_800_002_000.0
    expected = [
        a["id"]
        for a in sorted(truth.alerts, key=lambda a: -a["id"])
        if lo <= a["last_seen_at"] < hi
    ]
    items, totals = _walk(
        _getter(_edge(path)),
        "/v1/live/alerts",
        {"since": lo, "until": hi, "sort": "newest", "limit": 9},
        "alerts",
    )
    assert [a["id"] for a in items] == expected and set(totals) == {len(expected)}
    assert {a["retrospective"] for a in items} == {True, False}  # both kinds, told apart


def test_alerts_changing_while_paging_never_duplicate_or_skip_unchanged_ones(tmp_path):
    store, truth = seed(tmp_path / "live.sqlite", 200, 300, seed=7)
    reader = LiveReader(store.path)
    page = reader.alerts_page(AlertsQuery(limit=25), 0.0)
    got = [a["id"] for a in page["items"]]
    touched: set[int] = set()
    rng = random.Random(3)
    while page["next_cursor"]:
        # between requests: new alerts, new occurrences and status changes
        for k in range(2):
            rp.upsert_alert(
                store.db,
                group_key=f"new:{len(got)}:{k}",
                layer="screening",
                kind="source_match",
                priority=1,
                title="n",
                txid=hexid(k, "z"),
                evidence={},
                at=1.9e9,
            )
        aid = rng.choice(truth.alerts)["id"]
        touched.add(aid)
        rp.set_alert_status(store.db, aid, rng.choice(rp.ALERT_STATUSES), 1.9e9)
        page = reader.alerts_page(AlertsQuery(limit=25, cursor=page["next_cursor"]), 0.0)
        got += [a["id"] for a in page["items"]]
    unchanged = [a["id"] for a in sorted(truth.alerts, key=triage_key) if a["id"] not in touched]
    assert [i for i in got if i in set(unchanged)] == unchanged  # each once, in order


def test_old_queue_api_shape_is_unchanged(data):
    """Clients from before paging (status + limit, a list under "alerts") keep working."""
    path, truth = data
    edge = _edge(path)
    body = edge.get("/v1/live/alerts?status=new&limit=5", headers=AUTH).json()
    assert [a["id"] for a in body["alerts"]] == _alert_expected(
        truth, lambda a: a["status"] == "new"
    )[:5]
    fd = edge.get("/v1/live/findings?max_priority=4&limit=5", headers=AUTH).json()["findings"]
    nxt = edge.get(f"/v1/live/findings?limit=5&before_id={fd[-1]['id']}", headers=AUTH).json()
    assert nxt["findings"][0]["id"] < fd[-1]["id"]


# ---------------------------------------------------------------- validation and bounds
@pytest.mark.parametrize(
    "url,code",
    [
        ("/v1/live/alerts?limit=201", 422),
        ("/v1/live/findings?limit=0", 422),
        ("/v1/live/alerts?type=fraud", 422),
        ("/v1/live/findings?type=illicit", 422),
        ("/v1/live/alerts?authority=OFAC%20SDN", 422),
        ("/v1/live/alerts?authority=" + "a" * 65, 422),
        ("/v1/live/alerts?cursor=" + "A" * 201, 422),
        ("/v1/live/alerts?cursor=not%2Fbase64", 422),
        ("/v1/live/alerts?cursor=Zm9v", 400),  # "foo"
        ("/v1/live/alerts?since=nan", 422),
        ("/v1/live/alerts?since=1e12", 422),
        ("/v1/live/alerts?hours=721", 422),
        ("/v1/live/alerts?since=200&until=100", 400),
        ("/v1/live/findings?type=insufficient_evidence&priority=1", 400),
        ("/v1/live/findings?cursor=5&before_id=5", 400),
        ("/v1/live/findings?cursor=9999999999999999999", 400),
        ("/v1/live/alerts?sort=random", 422),
    ],
)
def test_invalid_page_requests_are_refused_without_a_500(data, url, code):
    r = _edge(data[0]).get(url, headers=AUTH)
    assert r.status_code == code, r.text
    assert "Traceback" not in r.text


def test_a_cursor_cannot_be_reused_with_another_sort(data):
    edge = _edge(data[0])
    nxt = edge.get("/v1/live/alerts?limit=2", headers=AUTH).json()["page"]["next_cursor"]
    assert edge.get(f"/v1/live/alerts?sort=newest&cursor={nxt}", headers=AUTH).status_code == 400
    forged = rp._encode_cursor(["t", 0, 1, float("inf"), 3]).replace("=", "")
    assert edge.get(f"/v1/live/alerts?cursor={forged}", headers=AUTH).status_code == 400


def test_page_responses_are_bounded(data):
    edge = _edge(data[0])
    for url in ("/v1/live/alerts?limit=200", "/v1/live/findings?limit=200"):
        r = edge.get(url, headers=AUTH)
        assert r.status_code == 200 and len(r.content) < 2_000_000  # the proxy's body limit


# ---------------------------------------------------------------- permissions
def test_paged_routes_need_the_token_and_are_read_only(data):
    edge = _edge(data[0])
    for url in (
        "/v1/live/alerts?status=open&type=retrospective&limit=5",
        "/v1/live/findings?type=source_match&authority=ofac-sdn",
    ):
        assert edge.get(url).status_code == 401
        assert edge.get(url, headers={"authorization": "Bearer " + "x" * 48}).status_code == 401
        assert edge.get(url, headers=AUTH).status_code == 200
        assert edge.post(url, headers=AUTH).status_code == 405


# ---------------------------------------------------------------- remote proxy
def test_the_remote_proxy_forwards_filters_and_cursors(data):
    path, truth = data
    edge = _edge(path)
    live = LiveConfig(enabled=True, remote_url="http://testserver", remote_cache_s=0.0)
    be = RemoteBackend(live, TOKEN, transport=edge._transport)
    q = AlertsQuery(status="open", type="source_match", limit=7)
    got, cursor = [], None
    while True:
        page = be.alerts_page(AlertsQuery(**{**q.__dict__, "cursor": cursor}))
        got += [a["id"] for a in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    want = _alert_expected(
        truth, lambda a: a["status"] in ("new", "under_review") and a["kind"] == "source_match"
    )
    assert got == want and page["total"] == len(want)
    relations = sum(f["state"] == "relation" for f in truth.findings)
    fp = be.findings_page(FindingsQuery(type="relation", hours=720, limit=5))
    assert fp["total"] == relations and len(fp["items"]) == 5  # hours forwarded (snapped)
    assert be.findings_page(FindingsQuery(type="relation", until=1e9))["total"] == 0
    # a bad request is answered by the proxy itself (400), never sent upstream
    sent = be.upstream_requests
    with pytest.raises(rp.InvalidQuery):
        be.alerts_page(AlertsQuery(cursor="Zm9v"))
    with pytest.raises(rp.InvalidQuery):
        be.findings_page(FindingsQuery(since=5.0, until=1.0))
    assert be.upstream_requests == sent


def test_a_dashboard_never_applies_filters_an_older_edge_ignores(tmp_path):
    """An edge API from before paging ignores unknown query parameters: passing the filter
    on and showing its unfiltered answer would mislabel every row."""
    legacy = {"mode": "live", "alerts": [{"id": 1}], "findings": [{"id": 9}]}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=legacy)

    live = LiveConfig(enabled=True, remote_url="http://localhost:9", remote_cache_s=0.0)
    be = RemoteBackend(live, TOKEN, transport=httpx.MockTransport(handler))
    page = be.alerts_page(AlertsQuery(status="new", limit=10))
    assert page["items"] == [{"id": 1}] and page["total"] is None  # unknown, not 1
    assert be.findings_page(FindingsQuery(limit=10))["total"] is None
    from chainwatch.serving.live_api import UpstreamUnavailable

    for q in (
        AlertsQuery(status="open"),
        AlertsQuery(type="retrospective"),
        AlertsQuery(sort="newest"),
    ):
        with pytest.raises(UpstreamUnavailable, match="upgrade the collector"):
            be.alerts_page(q)
    with pytest.raises(UpstreamUnavailable, match="upgrade the collector"):
        be.findings_page(FindingsQuery(type="anomaly"))


def test_a_database_without_risk_tables_reports_the_lists_unavailable(tmp_path):
    from chainwatch.live.store import SCHEMA

    db = tmp_path / "old.sqlite"
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    con.close()
    edge = _edge(db)
    for url, key in (("/v1/live/alerts", "alerts"), ("/v1/live/findings", "findings")):
        body = edge.get(url, headers=AUTH).json()
        assert body[key] == [] and body["page"]["available"] is False
        assert body["page"]["total"] == 0


def test_cli_lists_more_than_one_page_and_refuses_unknown_statuses(tmp_path, capsys):
    from chainwatch.config import REPO_ROOT
    from chainwatch.live import cli

    store, _ = seed(tmp_path / "live.sqlite", 10, 260)
    store.close()
    cfg = tmp_path / "c.yaml"
    base = REPO_ROOT / "configs" / "live.yaml"
    cfg.write_text(f"extends: {base}\nlive:\n  db_path: {store.path}\n")
    argv = ["--config", str(cfg), "alerts", "list"]
    assert cli.main([*argv, "--limit", "1000"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 260
    with pytest.raises(SystemExit, match="status must be"):
        cli.main([*argv, "--status", "guilty"])
