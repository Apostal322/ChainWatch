import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "./api";
import {
  ago,
  applyRecentPage,
  confirmedLabel,
  explorerHref,
  FEED_LAG_S,
  feedLag,
  relatedExplorerHref,
  connectionLabel,
  connectionView,
  eventsUrl,
  maxRowId,
  OBSERVATION_STALE_S,
  reconnectDelay,
  sourceBadge,
  STATUS_STALE_S,
  throughputSeries,
  type LiveStatus,
  filterSort,
  formatDuration,
  formatSat,
  liveApi,
  mergeFeed,
  outageDuration,
  shortTxid,
  type LiveRow,
} from "./live";

const row = (id: number, over: Partial<LiveRow> = {}): LiveRow => ({
  id,
  txid: id.toString(16).padStart(64, "0"),
  first_seen: 1000 + id,
  fetched_at: 1001 + id,
  outcome: "fetched",
  error: null,
  fee_rate: id,
  fee: id * 100,
  vsize: 200 - id,
  n_inputs: 1,
  n_outputs: id % 3,
  output_value_total: 1000,
  rbf_signal: id % 2 === 0,
  confirmed_at_fetch: false,
  flags: id % 2 === 0 ? ["rbf_signal"] : [],
  ...over,
});

describe("live feed helpers", () => {
  it("merges newest first, de-duplicates by id and caps the feed", () => {
    const feed = [row(3), row(2)];
    const merged = mergeFeed(feed, [row(4), row(3), row(5)], 3);
    expect(merged.map((r) => r.id)).toEqual([5, 4, 3]);
  });

  it("de-duplicates by txid and lets the newer copy of a row win", () => {
    const tx = "ab".repeat(32);
    // the same transaction stored again under a new id (e.g. re-observed): one row, the newest id
    const merged = mergeFeed([row(3, { txid: tx }), row(2)], [row(7, { txid: tx }), row(6, { txid: tx })]);
    expect(merged.map((r) => r.id)).toEqual([7, 2]);
    expect(merged.filter((r) => r.txid === tx)).toHaveLength(1);
    // an incoming copy of a held id replaces it (e.g. now carries its assessment)
    const updated = mergeFeed([row(3, { risk_assessed: false })], [row(3, { risk_assessed: true })]);
    expect(updated).toHaveLength(1);
    expect(updated[0].risk_assessed).toBe(true);
    expect(mergeFeed([row(1)], [])).toEqual([row(1)]);
  });

  it("applies a polled page and resets after a database restore or reset", () => {
    const first = applyRecentPage([], null, [row(10), row(9)]);
    expect(first).toMatchObject({ lastId: 10, reset: false });
    const more = applyRecentPage(first.feed, 10, [row(12), row(11), row(10)]);
    expect(more.feed.map((r) => r.id)).toEqual([12, 11, 10, 9]);
    expect(more).toMatchObject({ lastId: 12, reset: false });
    // a stream row (15) arrived while the request was in flight: not a reset, lastId stays 15
    expect(applyRecentPage(more.feed, 12, [row(12)], 15)).toMatchObject({ lastId: 15, reset: false });
    // server's newest id below the client's: restored / reset database; old rows dropped
    const reset = applyRecentPage(more.feed, 12, [row(3, { txid: "cd".repeat(32) }), row(2)]);
    expect(reset.reset).toBe(true);
    expect(reset.lastId).toBe(3);
    expect(reset.feed.map((r) => r.id)).toEqual([3, 2]);
    // emptied database
    expect(applyRecentPage(more.feed, 12, [])).toEqual({ feed: [], lastId: null, reset: true });
    expect(applyRecentPage([], null, [])).toEqual({ feed: [], lastId: null, reset: false });
  });

  it("reports a feed that lags behind the collector's observations", () => {
    const st = (over: Partial<LiveStatus> = {}) => status({ last_observation_at: 2000, ...over });
    expect(feedLag(st(), [row(1, { fetched_at: 2000 - FEED_LAG_S - 30 })])).toBe(FEED_LAG_S + 30);
    expect(feedLag(st(), [row(1, { fetched_at: 1990 })])).toBeNull();
    expect(feedLag(st({ connection: "reconnecting" }), [row(1, { fetched_at: 0 })])).toBeNull();
    expect(feedLag(st({ last_observation_at: null }), [row(1, { fetched_at: 0 })])).toBeNull();
    expect(feedLag(st(), [])).toBeNull();
    expect(feedLag(null, [row(1)])).toBeNull();
  });

  it("filters by fee rate, flag and outcome, then sorts", () => {
    const rows = [row(1), row(2), row(3), row(4, { outcome: "failed", fee_rate: null, flags: [] })];
    const base = { minFeeRate: null, flag: null, showFailed: false, sort: "newest" as const };
    expect(filterSort(rows, base).map((r) => r.id)).toEqual([3, 2, 1]);
    expect(filterSort(rows, { ...base, showFailed: true }).map((r) => r.id)).toEqual([4, 3, 2, 1]);
    expect(filterSort(rows, { ...base, minFeeRate: 2 }).map((r) => r.id)).toEqual([3, 2]);
    expect(filterSort(rows, { ...base, flag: "rbf_signal" }).map((r) => r.id)).toEqual([2]);
    expect(filterSort(rows, { ...base, sort: "vsize" }).map((r) => r.id)).toEqual([1, 2, 3]);
  });

  it("formats values without implying payment amounts", () => {
    expect(formatSat(546)).toBe("546 sat");
    expect(formatSat(150_000_000)).toBe("1.5 BTC");
    expect(formatSat(null)).toBe("—");
    expect(shortTxid("a".repeat(64))).toBe(`${"a".repeat(10)}…${"a".repeat(6)}`);
    expect(ago(100, 165)).toBe("1m ago");
    expect(ago(0, 47 * 3600)).toBe("47h ago");
    expect(ago(0, 49 * 3600)).toBe("2d ago");
    expect(connectionLabel(null)).toBe("Disconnected");
    expect(connectionLabel("reconnecting")).toBe("Reconnecting");
  });
});

describe("collection gaps", () => {
  it("formats durations and measures open outages until now", () => {
    expect(formatDuration(45)).toBe("45s");
    expect(formatDuration(725)).toBe("12m 5s");
    expect(formatDuration(11_040)).toBe("3h 4m");
    expect(formatDuration(3 * 86400 + 5 * 3600)).toBe("3d 5h");
    expect(formatDuration(null)).toBe("—");
    const base = { id: 1, started_at: 100, detail: null };
    expect(outageDuration({ ...base, kind: "collector_down", ended_at: 160 }, 1000)).toBe(60);
    expect(outageDuration({ ...base, kind: "disconnected", ended_at: null }, 1000)).toBe(900);
    expect(outageDuration({ ...base, kind: "sequence_gap", ended_at: 100 }, 1000)).toBeNull();
  });
});

const status = (over: Partial<LiveStatus> = {}): LiveStatus => ({
  mode: "live",
  source_url: "wss://mempool.space/api/v1/ws",
  source_is_public_mempool: true,
  data_origin: "real",
  connection: "connected",
  collector: {
    state: "connected", updated_at: 1000, started_at: 900, connected_since: 900, last_message_at: 999,
    reconnects: 0, counters: {}, settings: {},
  },
  coverage: { sample_rate: 0.05, fetch_per_minute: 30, fetched_share_of_announced: 0.05 },
  via: "local database",
  last_observation_at: 995,
  availability_24h: { window_s: 86400, observed_share: 1, down_s: 0, sequence_gaps: 0 },
  explorer_tx_url: "https://mempool.space/tx/{txid}",
  indicators: {},
  notice: "",
  server_time: 1000,
  ...over,
});

describe("data-origin badge", () => {
  it("labels data real only when the status explicitly says so", () => {
    expect(sourceBadge(status()).kind).toBe("real");
    expect(sourceBadge(status({ source_is_public_mempool: false })).kind).toBe("mock");
    expect(sourceBadge(null)).toEqual({ kind: "unknown", text: expect.stringContaining("UNKNOWN") });
    // a malformed / partial status (e.g. truncated body) is never "real"
    expect(sourceBadge({} as LiveStatus).kind).toBe("unknown");
    expect(sourceBadge({ source_is_public_mempool: "yes" } as unknown as LiveStatus).kind).toBe("unknown");
  });
});

describe("connection badge", () => {
  // client clock == server clock here; statusAt is when the status arrived (client seconds)
  const view = (s: LiveStatus | null, now: number, statusAt: number | null, apiError = false) =>
    connectionView(s, { now, statusAt, apiError });

  it("is unknown without a status, after a failed poll, or when the status is old", () => {
    expect(view(null, 1000, null)).toMatchObject({ tone: "off", label: "Status unknown", detail: "waiting for status" });
    expect(view(null, 1000, null, true).detail).toBe("live API unreachable");
    expect(view(status(), 1001, 1000, true)).toMatchObject({ tone: "off", label: "Status unknown" });
    expect(view(status(), 1000 + STATUS_STALE_S + 1, 1000).label).toBe("Status unknown");
    expect(view(status(), 1000 + STATUS_STALE_S - 1, 1000).label).toBe("Connected");
  });

  it("flags a connected collector whose last observation is stale", () => {
    expect(view(status(), 1000, 1000)).toEqual({ tone: "ok", label: "Connected", detail: null });
    const stale = view(status({ last_observation_at: 1000 - OBSERVATION_STALE_S - 600 }), 1005, 1000);
    expect(stale.tone).toBe("warn");
    expect(stale.detail).toContain("12m");
    expect(view(status({ last_observation_at: null }), 1000, 1000).detail).toBe("no transaction fetched yet");
    // v0.8.1: connected for longer than OBSERVATION_STALE_S without one observation is not "fresh"
    const never = status({ last_observation_at: null });
    never.collector!.connected_since = 1000 - OBSERVATION_STALE_S - 60;
    expect(view(never, 1000, 1000)).toMatchObject({ tone: "warn", label: "Connected, no data yet" });
  });

  it("says no live data yet (not unreachable) and refreshing after the tab was hidden", () => {
    expect(connectionView(null, { now: 1000, statusAt: null, apiError: false, noData: true }))
      .toMatchObject({ label: "No live data", detail: "no live data yet (collector not started)" });
    // tab hidden for 10 min: the old status is stale, but a refresh is under way
    expect(connectionView(status(), { now: 1600, statusAt: 1000, apiError: false, refreshing: true }))
      .toMatchObject({ label: "Refreshing…", detail: "refreshing…" });
    // a failed refresh is reported as such
    expect(connectionView(status(), { now: 1600, statusAt: 1000, apiError: true, refreshing: true }).label).toBe("Status unknown");
    // a fresh status is shown normally
    expect(connectionView(status(), { now: 1001, statusAt: 1000, apiError: false, refreshing: true }).label).toBe("Connected");
  });

  it("compares against the server clock, not the client clock", () => {
    // client clock 1 h ahead of the server: the observation is 5 s old on the server's clock
    expect(view(status({ server_time: 1000 }), 4600, 4600).label).toBe("Connected");
  });

  it("maps reconnecting, disconnected and failed", () => {
    expect(view(status({ connection: "reconnecting" }), 1000, 1000).tone).toBe("warn");
    expect(view(status({ connection: "disconnected" }), 1000, 1000)).toMatchObject({ tone: "off", label: "Disconnected" });
    expect(view(status({ connection: "failed" }), 1000, 1000).label).toBe("Collector failed");
    const failedState = status();
    failedState.collector!.state = "failed";
    expect(view(failedState, 1000, 1000).label).toBe("Collector failed");
    expect(connectionLabel("failed")).toBe("Collector failed");
  });
});

describe("throughput series", () => {
  const m = (t: number, received: number) => ({ t, received });

  it("drops the minute that is still filling", () => {
    const { points, lastComplete } = throughputSeries([m(600, 300), m(660, 20)], 690);
    expect(points.map((p) => p.t)).toEqual([600]);
    expect(lastComplete).toMatchObject({ t: 600, announced: 300, label: "00:10" });
  });

  it("fills missing minutes with null so gaps are not bridged", () => {
    const { points } = throughputSeries([m(600, 5), m(780, 7)], 900);
    expect(points.map((p) => [p.t, p.announced])).toEqual([[600, 5], [660, null], [720, null], [780, 7], [840, null]]);
  });

  it("is empty when nothing complete exists", () => {
    expect(throughputSeries([], 1000)).toEqual({ points: [], lastComplete: null });
    expect(throughputSeries([m(960, 3)], 1000)).toEqual({ points: [], lastComplete: null });
  });
});

describe("event stream helpers", () => {
  it("resumes after the newest row already loaded", () => {
    expect(eventsUrl(null)).toBe("/v1/live/events");
    expect(eventsUrl(42)).toBe("/v1/live/events?since=42");
    expect(maxRowId([])).toBeNull();
    expect(maxRowId([row(3), row(9), row(5)])).toBe(9);
  });

  it("backs off exponentially with jitter and a cap", () => {
    const hi = () => 1;
    const lo = () => 0;
    expect([0, 1, 2, 3].map((a) => reconnectDelay(a, hi))).toEqual([3000, 6000, 12000, 24000]);
    expect(reconnectDelay(10, hi)).toBe(60000);
    expect(reconnectDelay(0, lo)).toBe(1500);
    expect(reconnectDelay(10, lo)).toBe(30000);
    for (let a = 0; a < 8; a++) {
      const d = reconnectDelay(a);
      expect(d).toBeGreaterThanOrEqual(1500);
      expect(d).toBeLessThanOrEqual(60000);
    }
  });
});

describe("explorer links", () => {
  const tx = "ab".repeat(32);
  const parent = "cd".repeat(32);
  const url = `https://mempool.space/tx/${tx}`;
  it("links only real network data over https", () => {
    expect(explorerHref(url, "real")).toBe(url);
    expect(explorerHref(url, "mock")).toBeNull();
    expect(explorerHref(url, "unknown")).toBeNull();
    expect(explorerHref("javascript:alert(1)", "real")).toBeNull();
    expect(explorerHref(`http://mempool.space/tx/${tx}`, "real")).toBeNull();
    expect(explorerHref(null, "real")).toBeNull();
    expect(relatedExplorerHref(url, "real", tx, parent)).toBe(`https://mempool.space/tx/${parent}`);
    expect(relatedExplorerHref(url, "mock", tx, parent)).toBeNull();
    expect(relatedExplorerHref(url, "real", tx, "not-a-txid")).toBeNull();
  });
});

describe("detail labels", () => {
  it("shows unknown confirmation state as unknown", () => {
    expect(confirmedLabel(true)).toBe("yes");
    expect(confirmedLabel(false)).toBe("no (in mempool)");
    expect(confirmedLabel(null)).toBe("unknown");
  });
});

describe("live API client", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("rejects a 200 with a truncated body instead of returning null", async () => {
    vi.stubGlobal("fetch", async () => new Response('{"mode":"live","source_url":"ws://', { status: 200 }));
    await expect(liveApi.status()).rejects.toMatchObject({ code: "invalid_response" });
    await expect(liveApi.status()).rejects.toBeInstanceOf(ApiError);
  });

  it("surfaces the API error code", async () => {
    vi.stubGlobal("fetch", async () =>
      new Response(JSON.stringify({ error: { code: "live_no_data", message: "no live data yet" } }), { status: 503 }));
    await expect(liveApi.status()).rejects.toMatchObject({ status: 503, code: "live_no_data" });
  });
});
