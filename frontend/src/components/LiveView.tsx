import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Bar, BarChart, CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { ApiError, type Info } from "../lib/api";
import { revealIfStacked } from "../lib/dom";
import {
  ago,
  applyRecentPage,
  confirmedLabel,
  connectionView,
  eventsUrl,
  explorerHref,
  FEED_LAG_S,
  feedLag,
  filterSort,
  formatDuration,
  formatSat,
  liveApi,
  maxRowId,
  mergeFeed,
  OUTAGE_LABEL,
  outageDuration,
  reconnectDelay,
  relatedExplorerHref,
  shortTxid,
  sourceBadge,
  throughputSeries,
  utcStamp,
  type FeedFilter,
  type LiveDetail,
  type LiveRow,
  type LiveStats,
  type LiveStatus,
  type Outage,
  type SortKey,
  type ThroughputPoint,
} from "../lib/live";
import {
  filterAlerts,
  priorityByTxid,
  riskApi,
  riskState as riskStateOf,
  type Alert,
  type Finding,
  type PriorityLevel,
  type RiskSummary,
} from "../lib/risk";
import { useNow } from "../lib/useNow";
import { Tile } from "./common";
import {
  InvestigationQueue,
  PriorityLegend,
  PriorityMarker,
  RecentFindings,
  RiskAssessment,
  RiskOverview,
  type AlertFilter,
  type DataKind,
} from "./RiskPanels";

const SORTS: Record<SortKey, string> = {
  newest: "Newest first",
  fee_rate: "Fee rate (high first)",
  vsize: "Virtual size (large first)",
  n_outputs: "Outputs (most first)",
  fee: "Absolute fee (high first)",
};

const STATUS_POLL_MS = 5000;
const STATS_POLL_MS = 15000;
const RISK_POLL_MS = 15000;
/** Fallback for the event stream: poll the newest rows while it is not open or the feed lags. */
const FEED_POLL_MS = 15000;
const ALERTS_LIMIT = 200;

// Recharts' default tooltip is white with inherited text colour: unreadable in dark mode.
const TOOLTIP = {
  contentStyle: {
    fontSize: 12,
    background: "var(--surface)",
    border: "1px solid var(--border)",
    borderRadius: 6,
    color: "var(--ink)",
  },
  labelStyle: { color: "var(--ink-2)" },
  itemStyle: { color: "var(--ink)" },
} as const;
const AXIS_TICK = { fill: "var(--muted)", fontSize: 11 };

const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));
const num = (v: number | string | null | undefined) => (typeof v === "number" ? v : null);

function describeRiskError(e: unknown): string {
  if (e instanceof ApiError && e.status === 404) return "not available on this server (older version without risk intelligence)";
  return errText(e);
}

type LiveProblem = { noData: true } | { noData: false; text: string };

function describeLiveError(e: unknown): LiveProblem {
  if (e instanceof ApiError && e.code === "live_no_data") return { noData: true };
  return { noData: false, text: `Live API unavailable: ${errText(e)}` };
}

function SourceBadge({ status }: { status: LiveStatus | null }) {
  const b = sourceBadge(status);
  const cls = b.kind === "real" ? "real" : b.kind === "mock" ? "synthetic" : "unknown";
  return <span className={`badge ${cls}`}>{b.text}</span>;
}

/** Re-renders each second (staleness depends on the clock); nothing else does. */
function ConnectionBadge(props: {
  status: LiveStatus | null;
  statusAt: number | null;
  apiError: boolean;
  noData: boolean;
  refreshing: boolean;
}) {
  const now = useNow();
  const v = connectionView(props.status, {
    now, statusAt: props.statusAt, apiError: props.apiError, noData: props.noData, refreshing: props.refreshing,
  });
  return (
    <>
      <span className={`badge conn ${v.tone}`} role="status">
        <i aria-hidden="true" /> {v.label}
      </span>
      {v.detail && <span className={v.tone === "warn" ? "conn-detail warn" : "conn-detail"}>{v.detail}</span>}
    </>
  );
}

/** `skew` (server minus client clock) puts server timestamps on the client's clock. */
function Ago({ ts, skew = 0 }: { ts: number; skew?: number }) {
  return <>{ago(ts, useNow() + skew)}</>;
}

function LastObservationTile({ status, skew }: { status: LiveStatus | null; skew: number }) {
  const now = useNow() + skew;
  const t = status?.last_observation_at ?? null;
  return (
    <Tile
      k="Last successful observation"
      v={t !== null ? ago(t, now) : "—"}
      h={!status ? "status not loaded" : t !== null ? `${utcStamp(t)} UTC` : "no transaction fetched yet"}
    />
  );
}

function Disabled() {
  return (
    <div className="card prose">
      <h2>Live monitor is not enabled in this deployment</h2>
      <p>
        The public demo shows synthetic data only. The live monitor reads real Bitcoin mempool
        observations collected by a separate, opt-in process, so it runs locally:
      </p>
      <pre className="code">
        {"chainwatch --config configs/live.yaml live-collect   # terminal 1: talks to mempool.space\n" +
          "chainwatch --config configs/live.yaml serve          # terminal 2: this dashboard"}
      </pre>
      <p className="muted">See docs/LIVE_MONITOR.md for limits, coverage and data semantics.</p>
    </div>
  );
}

const ThroughputChart = memo(function ThroughputChart({ points }: { points: ThroughputPoint[] }) {
  return (
    <ResponsiveContainer width="100%" height={180}>
      <LineChart data={points} margin={{ top: 8, right: 16, bottom: 4, left: 4 }}>
        <CartesianGrid stroke="var(--grid)" vertical={false} />
        <XAxis dataKey="label" stroke="var(--axis)" tick={AXIS_TICK} tickLine={false} minTickGap={24} />
        <YAxis stroke="var(--axis)" tick={AXIS_TICK} tickLine={false} width={52} allowDecimals={false}
               label={{ value: "txids / min", angle: -90, position: "insideLeft", fill: "var(--ink-2)", fontSize: 11 }} />
        <Tooltip cursor={{ stroke: "var(--axis)" }} {...TOOLTIP}
                 formatter={(v) => [v ?? "no data", "announced txids"]} labelFormatter={(l) => `${l} UTC`} />
        <Line dataKey="announced" stroke="var(--s1)" strokeWidth={2} dot={{ r: 2 }} connectNulls={false}
              isAnimationActive={false} />
      </LineChart>
    </ResponsiveContainer>
  );
});

const FeeHistogram = memo(function FeeHistogram({ bins }: { bins: { bin: string; count: number }[] }) {
  return (
    <ResponsiveContainer width="100%" height={180}>
      <BarChart data={bins} margin={{ top: 8, right: 16, bottom: 4, left: 4 }}>
        <CartesianGrid stroke="var(--grid)" vertical={false} />
        <XAxis dataKey="bin" stroke="var(--axis)" tick={AXIS_TICK} tickLine={false} />
        <YAxis stroke="var(--axis)" tick={AXIS_TICK} tickLine={false} width={52} allowDecimals={false}
               label={{ value: "transactions", angle: -90, position: "insideLeft", fill: "var(--ink-2)", fontSize: 11 }} />
        <Tooltip cursor={{ fill: "var(--surface-2)" }} {...TOOLTIP}
                 formatter={(v) => [v, "transactions"]} labelFormatter={(l) => `${l} sat/vB`} />
        <Bar dataKey="count" fill="var(--s1)" radius={[4, 4, 0, 0]} isAnimationActive={false} />
      </BarChart>
    </ResponsiveContainer>
  );
});

function OutageDuration({ o }: { o: Outage }) {
  const now = useNow();
  return <>{o.kind === "sequence_gap" ? "—" : formatDuration(outageDuration(o, now))}</>;
}

const OutagesTable = memo(function OutagesTable({ outages }: { outages: Outage[] }) {
  return (
    <div className="scroll">
      <table>
        <thead>
          <tr><th scope="col">Started (UTC)</th><th scope="col">Type</th><th scope="col" className="num">Duration</th><th scope="col">Detail</th></tr>
        </thead>
        <tbody>
          {outages.map((o) => (
            <tr key={o.id}>
              <td className="nowrap">{utcStamp(o.started_at)}</td>
              <td>{OUTAGE_LABEL[o.kind] ?? o.kind}{o.ended_at === null ? " (ongoing)" : ""}</td>
              <td className="num"><OutageDuration o={o} /></td>
              <td className="muted">{o.detail}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
});

/** Rows re-render only when the feed, filter or selection changes; "Seen" cells tick on their own. */
const FeedTable = memo(function FeedTable(props: {
  rows: LiveRow[];
  selected: string | null;
  onSelect: (txid: string) => void;
  /** Priorities known from /findings, for rows the event stream sent before their assessment. */
  findingPriority: Map<string, PriorityLevel>;
  skew: number;
}) {
  const { rows, selected, onSelect, findingPriority, skew } = props;
  return (
    <table>
      <thead>
        <tr>
          <th scope="col"><abbr title="Review priority (P1 = review first; not a score)">Prio.</abbr></th>
          <th scope="col">Seen</th><th scope="col">Transaction</th><th scope="col" className="num">Fee rate</th>
          <th scope="col" className="num">vsize</th><th scope="col" className="num">In / out</th><th scope="col">Indicators</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => {
          const isSel = selected === r.txid;
          return (
            <tr key={r.id} className={isSel ? "clickable selected" : "clickable"} onClick={() => onSelect(r.txid)}>
              <td><PriorityMarker level={r.risk_priority ?? findingPriority.get(r.txid) ?? null} assessed={r.risk_assessed} state={r.risk_state} /></td>
              <td className="muted nowrap"><Ago ts={r.first_seen} skew={skew} /></td>
              <td>
                <button type="button" className="rowbtn" aria-pressed={isSel}
                        aria-label={`Show details for transaction ${r.txid}`}
                        onClick={(e) => { e.stopPropagation(); onSelect(r.txid); }}>
                  <code className="nowrap">{shortTxid(r.txid)}</code>
                </button>
              </td>
              <td className="num">
                {r.outcome === "fetched"
                  ? (r.fee_rate !== null ? r.fee_rate.toFixed(1) : "—")
                  : <span className="muted">{r.error}</span>}
              </td>
              <td className="num">{r.vsize ?? "—"}</td>
              <td className="num nowrap">{r.outcome === "fetched" ? `${r.n_inputs} / ${r.n_outputs}` : "—"}</td>
              <td>{r.flags.map((f) => <span key={f} className="chip flag">{f}</span>)}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
});

function locktimeText(f: NonNullable<LiveDetail["features"]>): string {
  // Enforcement only matters for a non-zero locktime.
  const enforced = f.locktime === 0 ? ""
    : f.locktime_enforced === true ? ", enforced" : f.locktime_enforced === false ? ", not enforced" : "";
  return `${f.version} · ${f.locktime} (${f.locktime_kind}${enforced})`;
}

function Detail({ detail, onSelect, kind }: { detail: LiveDetail; onSelect: (txid: string) => void; kind: DataKind }) {
  const f = detail.features;
  const href = explorerHref(detail.explorer_url, kind);
  return (
    <div>
      <p>
        <code style={{ wordBreak: "break-all" }}>{detail.txid}</code>{" "}
        {href ? <a href={href} target="_blank" rel="noopener noreferrer">View on mempool.space ↗</a>
          : <span className="muted small">{kind === "real" ? "(no explorer link)" : "(mock data: no explorer link)"}</span>}
      </p>
      {detail.outcome === "failed" ? (
        <p className="muted">Details could not be fetched ({detail.error}). The transaction may have been replaced, mined or evicted.</p>
      ) : f && (
        <>
          <dl className="kv">
            <dt>First seen</dt><dd>{utcStamp(detail.first_seen)} UTC</dd>
            <dt>Confirmed when fetched</dt><dd>{confirmedLabel(f.confirmed_at_fetch)}</dd>
            <dt>Fee / fee rate</dt><dd>{formatSat(f.fee)} · {f.fee_rate.toFixed(2)} sat/vB</dd>
            <dt>Size / vsize / weight</dt><dd>{f.size} B · {f.vsize} vB · {f.weight} WU</dd>
            <dt>Inputs / outputs</dt><dd>{f.n_inputs} / {f.n_outputs} (from {f.n_parent_txs} parent tx)</dd>
            <dt>Version / locktime</dt><dd>{locktimeText(f)}</dd>
            <dt>SegWit · BIP125 signal</dt><dd>{f.segwit ? "yes" : "no"} · {f.rbf_signal ? "yes" : "no"}</dd>
            <dt>Sum of outputs</dt><dd>{formatSat(f.output_value_total)} <span className="muted">(includes any change; not a payment amount)</span></dd>
            {f.max_equal_value_outputs !== undefined && (
              <><dt>Equal-value outputs · script types</dt><dd>max {f.max_equal_value_outputs} with the same value · {f.n_output_types ?? "—"} output script type{f.n_output_types === 1 ? "" : "s"}</dd></>
            )}
            {f.schema_version && <><dt>Feature schema</dt><dd><code>{f.schema_version}</code>{f.coinbase ? " · coinbase" : ""}</dd></>}
          </dl>
          <h3>Output values ({f.output_values.length} of {f.n_outputs} shown)</h3>
          <p className="small">{f.output_values.map(formatSat).join(" · ")}</p>
          <h3>Spent outputs (direct input references)</h3>
          <ul className="refs">
            {f.input_refs.map((ref) => {
              const [parent = "", vout = ""] = ref.split(":");
              const parentHref = relatedExplorerHref(detail.explorer_url, kind, detail.txid, parent);
              return (
                <li key={ref}>
                  {parentHref ? (
                    <a href={parentHref} target="_blank" rel="noopener noreferrer">
                      <code>{shortTxid(parent)}</code>
                    </a>
                  ) : <code>{shortTxid(parent)}</code>}
                  :{vout}
                </li>
              );
            })}
          </ul>
          <p className="muted small">Links between transactions are observable spends. They do not identify owners or wallets.</p>
        </>
      )}
      <RiskAssessment risk={detail.risk} lifecycle={detail.lifecycle} onSelect={onSelect} kind={kind} />
      <h3>Rule-based indicators</h3>
      {detail.flags.length === 0 ? <p className="muted">None.</p> : (
        <ul>{detail.flags.map((fl) => <li key={fl}><b>{fl}</b>: {detail.flag_descriptions[fl]}</li>)}</ul>
      )}
      <p className="muted small">Indicators flag observable properties only. They are not risk scores and say nothing about legality.</p>
    </div>
  );
}

export default function LiveView({ info }: { info: Info }) {
  const [status, setStatus] = useState<LiveStatus | null>(null);
  const [statusAt, setStatusAt] = useState<number | null>(null); // client seconds of last good status
  const [problem, setProblem] = useState<LiveProblem | null>(null);
  const [refreshing, setRefreshing] = useState(false); // tab visible again, fresh status pending
  const [stats, setStats] = useState<{ data: LiveStats; serverAt: number } | null>(null);
  const [statsError, setStatsError] = useState<string | null>(null);
  const [outages, setOutages] = useState<Outage[] | null>(null);
  const [outagesError, setOutagesError] = useState<string | null>(null);
  const [feed, setFeed] = useState<LiveRow[]>([]);
  const [stream, setStream] = useState("connecting");
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<LiveDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [filter, setFilter] = useState<FeedFilter>({ minFeeRate: null, flag: null, showFailed: false, sort: "newest" });
  const [risk, setRisk] = useState<RiskSummary | null>(null);
  const [riskError, setRiskError] = useState<string | null>(null);
  const [findings, setFindings] = useState<Finding[] | null>(null);
  const [findingsError, setFindingsError] = useState<string | null>(null);
  const [alerts, setAlerts] = useState<{ list: Alert[]; filter: AlertFilter } | null>(null);
  const [alertsError, setAlertsError] = useState<string | null>(null);
  const [alertFilter, setAlertFilter] = useState<AlertFilter>(null);
  const [skew, setSkew] = useState(0); // server minus client clock, for source ages
  const alertFilterRef = useRef<AlertFilter>(null);
  const alertsSeq = useRef(0);
  const detailRef = useRef<HTMLDivElement>(null);
  const detailHeadingRef = useRef<HTMLHeadingElement>(null);
  const skewRef = useRef(0); // server clock minus client clock, from the last status
  const statusRef = useRef<LiveStatus | null>(null);
  const feedRef = useRef<LiveRow[]>([]);
  feedRef.current = feed;

  const enabled = info.live_enabled;

  /** Latest request wins (a filter change can overlap a poll). A specific status is filtered by
   * the API (complete up to the limit); "open" is filtered here (the API returns open first). */
  const loadAlerts = useCallback(async () => {
    const seq = ++alertsSeq.current;
    const f = alertFilterRef.current;
    try {
      const r = await riskApi.alerts(f === null || f === "open" ? null : f, ALERTS_LIMIT);
      if (seq !== alertsSeq.current) return;
      setAlerts({ list: r.alerts ?? [], filter: f });
      setAlertsError(null);
    } catch (e) {
      if (seq === alertsSeq.current) setAlertsError(describeRiskError(e));
    }
  }, []);

  const onAlertFilter = useCallback((f: AlertFilter) => {
    alertFilterRef.current = f;
    setAlertFilter(f);
    void loadAlerts();
  }, [loadAlerts]);

  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    const busy = { status: false, stats: false, risk: false };

    // Polls skip while a previous request is in flight (no out-of-order overwrites) and while
    // the browser tab is hidden; they catch up immediately when it becomes visible again.
    const pollStatus = async () => {
      if (busy.status || document.hidden) return;
      busy.status = true;
      try {
        const s = await liveApi.status();
        if (!alive) return;
        const at = Date.now() / 1000;
        if (Number.isFinite(s.server_time)) {
          skewRef.current = s.server_time - at;
          setSkew((old) => (Math.abs(old - skewRef.current) > 2 ? skewRef.current : old));
        }
        statusRef.current = s;
        setStatus(s);
        setStatusAt(at);
        setProblem(null);
      } catch (e) {
        if (alive) setProblem(describeLiveError(e));
      } finally {
        busy.status = false;
        if (alive) setRefreshing(false);
      }
    };
    const pollStats = async () => {
      if (busy.stats || document.hidden) return;
      busy.stats = true;
      try {
        const [s, o] = await Promise.allSettled([liveApi.stats(60), liveApi.outages(24)]);
        if (!alive) return;
        if (s.status === "fulfilled") {
          setStats({ data: s.value, serverAt: Date.now() / 1000 + skewRef.current });
          setStatsError(null);
        } else setStatsError(errText(s.reason));
        if (o.status === "fulfilled") {
          setOutages(o.value.outages);
          setOutagesError(null);
        } else setOutagesError(errText(o.reason));
      } finally {
        busy.stats = false;
      }
    };
    // Risk intelligence: layer health, findings and the investigation queue, polled together.
    const pollRisk = async () => {
      if (busy.risk || document.hidden) return;
      busy.risk = true;
      try {
        const [s, f] = await Promise.allSettled([riskApi.summary(24), riskApi.findings(4, 50), loadAlerts()]);
        if (!alive) return;
        if (s.status === "fulfilled") {
          setRisk(s.value);
          setRiskError(null);
        } else setRiskError(describeRiskError(s.reason));
        if (f.status === "fulfilled") {
          setFindings(f.value.findings ?? []);
          setFindingsError(null);
        } else setFindingsError(describeRiskError(f.reason));
      } finally {
        busy.risk = false;
      }
    };
    const onVisibility = () => {
      if (!document.hidden) {
        setRefreshing(true);
        void pollStatus();
        void pollFeed(true);
        void pollStats();
        void pollRisk();
      }
    };
    void pollStatus();
    void pollStats();
    void pollRisk();
    const t1 = setInterval(pollStatus, STATUS_POLL_MS);
    const t2 = setInterval(pollStats, STATS_POLL_MS);
    const t3 = setInterval(pollRisk, RISK_POLL_MS);
    const t4 = setInterval(() => void pollFeed(false), FEED_POLL_MS);
    document.addEventListener("visibilitychange", onVisibility);

    // Server-sent events. On network errors the browser reconnects by itself (with Last-Event-ID).
    // On an HTTP error (e.g. 503 "too many streams") it gives up for good, so retry with backoff.
    let es: EventSource | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;
    let attempt = 0;
    let lastId: number | null = null;
    let streamOpen = false;
    let feedBusy = false;
    /** Fallback while the stream is not open (HTTP 429/503, backoff) or the feed lags behind the
     * collector; also detects a restored / reset database (server ids below `lastId`). */
    const pollFeed = async (force: boolean) => {
      if (feedBusy || document.hidden) return;
      if (!force && streamOpen && feedLag(statusRef.current, feedRef.current) === null) return;
      feedBusy = true;
      const sentAfter = lastId;
      try {
        const r = await liveApi.recent(200);
        if (!alive) return;
        // lastId / reset depend only on the page; the feed itself is merged functionally so a
        // stream row that arrived meanwhile is kept
        const next = applyRecentPage([], sentAfter, r.rows, lastId);
        lastId = next.lastId;
        setFeed((f) => (next.reset ? next.feed : mergeFeed(f, r.rows)));
        if (next.reset) {
          // the open stream resumes after the old database's ids and would never deliver: reopen
          es?.close();
          if (retry !== null) clearTimeout(retry);
          retry = null;
          attempt = 0;
          open();
        }
      } catch {
        // the status poll reports API problems
      } finally {
        feedBusy = false;
      }
    };
    const open = () => {
      if (!alive) return;
      streamOpen = false;
      setStream("connecting");
      const src = new EventSource(eventsUrl(lastId));
      es = src;
      src.onopen = () => {
        if (es !== src) return;
        attempt = 0;
        streamOpen = true;
        setStream("open");
      };
      src.onerror = () => {
        if (es !== src) return;
        streamOpen = false;
        if (src.readyState === EventSource.CLOSED) {
          src.close();
          if (!alive) return;
          const delay = reconnectDelay(attempt++);
          setStream(`closed by server, retrying in ${Math.round(delay / 1000)}s`);
          retry = setTimeout(open, delay);
        } else {
          setStream("reconnecting");
        }
      };
      src.addEventListener("tx", (ev) => {
        if (es !== src) return;
        let row: LiveRow;
        try {
          row = JSON.parse((ev as MessageEvent<string>).data) as LiveRow;
        } catch {
          return;
        }
        lastId = Math.max(lastId ?? 0, row.id);
        setFeed((f) => mergeFeed(f, [row]));
      });
    };
    // Open the stream only after the initial page, starting right after its newest row, so no
    // observation stored in between is missed.
    liveApi
      .recent(200)
      .then((r) => {
        if (!alive) return;
        lastId = maxRowId(r.rows);
        setFeed((f) => mergeFeed(f, r.rows));
      })
      .catch(() => undefined)
      .finally(open);

    return () => {
      alive = false;
      clearInterval(t1);
      clearInterval(t2);
      clearInterval(t3);
      clearInterval(t4);
      if (retry !== null) clearTimeout(retry);
      document.removeEventListener("visibilitychange", onVisibility);
      es?.close();
    };
  }, [enabled, loadAlerts]);

  useEffect(() => {
    if (!selected) {
      setDetail(null);
      setDetailError(null);
      setDetailLoading(false);
      return;
    }
    let ignore = false; // a slower response for an earlier selection must not win
    setDetailLoading(true);
    setDetailError(null);
    liveApi
      .tx(selected)
      .then((d) => { if (!ignore) setDetail(d); })
      .catch((e) => { if (!ignore) { setDetail(null); setDetailError(errText(e)); } })
      .finally(() => { if (!ignore) setDetailLoading(false); });
    return () => { ignore = true; };
  }, [selected]);

  const onSelect = useCallback((txid: string) => {
    setSelected(txid);
    requestAnimationFrame(() => revealIfStacked(detailRef.current));
  }, []);

  /** From the risk panels (far from the detail panel): always bring the panel into view and move
   * focus to its heading, so keyboard and screen-reader users land on the new content. */
  const onSelectFar = useCallback((txid: string) => {
    setSelected(txid);
    requestAnimationFrame(() => {
      const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
      detailRef.current?.scrollIntoView({ block: "start", behavior: reduce ? "auto" : "smooth" });
      detailHeadingRef.current?.focus({ preventScroll: true });
    });
  }, []);

  const rows = useMemo(() => filterSort(feed, filter).slice(0, 100), [feed, filter]);
  const throughput = useMemo(
    () => (stats ? throughputSeries(stats.data.per_minute, stats.serverAt) : { points: [], lastComplete: null }),
    [stats],
  );
  const hist = useMemo(
    () => (stats?.data.fee_rate_histogram ?? []).map((b) => ({
      bin: b.to === null ? `≥${b.from}` : `${b.from}–${b.to}`,
      count: b.count,
    })),
    [stats],
  );
  const binEdges = useMemo(() => (stats?.data.fee_rate_histogram ?? []).map((b) => b.from).join(", "), [stats]);
  const flags = useMemo(() => Object.keys(status?.indicators ?? {}), [status?.indicators]);
  const findingPriority = useMemo(() => priorityByTxid(findings ?? []), [findings]);
  const shownAlerts = useMemo(
    () => (alerts && alerts.filter === alertFilter ? filterAlerts(alerts.list, alertFilter) : null),
    [alerts, alertFilter],
  );
  const kind: DataKind = sourceBadge(status).kind;
  const riskState = riskStateOf(risk, Date.now() / 1000 + skew);
  const lag = problem === null ? feedLag(status, feed) : null;

  if (!enabled) return <Disabled />;

  const st = stats?.data ?? null;
  const c = status?.collector?.counters ?? {};
  const upstream = num(c.upstream_errors);
  const internal = num(c.internal_errors);
  const riskUnavailable = typeof c.risk_unavailable === "string" && c.risk_unavailable ? c.risk_unavailable : null;
  const lastFull = throughput.lastComplete;
  const shownDetail = detail && detail.txid === selected ? detail : null;

  return (
    <div>
      <div className="live-head">
        <SourceBadge status={status} />
        <ConnectionBadge status={status} statusAt={statusAt} apiError={problem !== null && !problem.noData}
                         noData={problem?.noData === true} refreshing={refreshing} />
        <span className="muted">
          event stream: {stream} · source: {status?.source_url ?? "—"}
          {status?.via ? ` · via ${status.via}` : ""}
        </span>
      </div>
      <div className="notice">
        {status?.notice ??
          "Live monitor: waiting for the collector status, so the data source is not confirmed yet. Coverage is partial (sampled, rate-limited); indicators are rule-based, not fraud predictions."}{" "}
        Sampling: {status ? `${(status.coverage.sample_rate * 100).toFixed(0)}% of announced txids` : "—"}, at most{" "}
        {status?.coverage.fetch_per_minute ?? "—"} detail requests/min.
      </div>
      {problem && !problem.noData && <p className="err" role="alert">{problem.text}</p>}
      {problem?.noData && (
        <p className="notice" role="status">
          No live data yet (collector not started). Start it with{" "}
          <code>chainwatch --config configs/live.yaml live-collect</code>.
        </p>
      )}

      <div className="tiles">
        <Tile k="Announced txids (last full minute)" v={lastFull?.announced != null ? String(lastFull.announced) : "—"}
              h={lastFull ? `${lastFull.label} UTC, as received by this collector` : "as received by this collector"} />
        <Tile k="Details fetched (60 min)" v={st ? String(st.totals.fetched) : "—"}
              h={st ? `${st.totals.failed} failed · ${st.totals.dropped} dropped (backpressure)` : undefined} />
        <Tile k="Fetched share of announced" v={status?.coverage.fetched_share_of_announced != null
              ? `${(status.coverage.fetched_share_of_announced * 100).toFixed(1)}%` : "—"} h="since collector start" />
        <Tile k="Median fee rate (60 min)" v={st?.fee_rate_quantiles ? `${st.fee_rate_quantiles.p50.toFixed(1)} sat/vB` : "—"}
              h={st?.fee_rate_quantiles ? `p10 ${st.fee_rate_quantiles.p10.toFixed(1)} · p90 ${st.fee_rate_quantiles.p90.toFixed(1)}` : undefined} />
        <Tile k="Median virtual size" v={st?.vsize_quantiles ? `${st.vsize_quantiles.p50} vB` : "—"}
              h={st?.vsize_quantiles ? `p90 ${st.vsize_quantiles.p90} vB` : undefined} />
        <Tile k="Reconnects / rate-limited"
              v={status?.collector ? `${status.collector.reconnects} / ${num(c.rate_limited) ?? 0}` : "—"}
              h={status?.collector
                ? `since collector start${upstream !== null || internal !== null ? ` · ${upstream ?? 0} upstream / ${internal ?? 0} internal errors` : ""}`
                : undefined} />
        <LastObservationTile status={status} skew={skew} />
        <Tile k="Observed share (24 h)" v={status?.availability_24h?.observed_share != null ? `${(status.availability_24h.observed_share * 100).toFixed(1)}%` : "—"}
              h={status?.availability_24h ? `down ${formatDuration(status.availability_24h.down_s)} of ${formatDuration(status.availability_24h.window_s)} · ${status.availability_24h.sequence_gaps} missed-update gaps` : undefined} />
      </div>

      <RiskOverview data={risk} error={riskError} skew={skew} kind={kind} riskUnavailable={riskUnavailable} />
      <div className="split" style={{ marginTop: 16 }}>
        <InvestigationQueue alerts={shownAlerts} error={alertsError} filter={alertFilter} onFilter={onAlertFilter}
                            onSelect={onSelectFar} skew={skew} kind={kind} riskState={riskState} />
        <RecentFindings findings={findings} error={findingsError} selected={selected} onSelect={onSelectFar}
                        skew={skew} kind={kind} riskState={riskState} />
      </div>

      <div className="split" style={{ marginTop: 16 }}>
        <div className="card">
          <h2>Announced transactions per minute</h2>
          {statsError && <p className="err" role="status">{st ? "Could not refresh statistics (showing the last loaded data)" : "Could not load statistics"}: {statsError}</p>}
          {!st && !statsError ? <p className="muted">Loading…</p>
            : throughput.points.length === 0 ? <p className="muted">No complete minute collected in the last 60 minutes.</p>
            : <ThroughputChart points={throughput.points} />}
          <p className="muted small">
            Complete UTC minutes only (the current minute is still filling); breaks in the line are
            minutes with no collection. Counts what this collector received, not network totals.
          </p>
        </div>
        <div className="card">
          <h2>Fee rate of fetched transactions (sat/vB, 60 min)</h2>
          {statsError && <p className="err" role="status">{st ? "Could not refresh statistics (showing the last loaded data)" : "Could not load statistics"}: {statsError}</p>}
          {!st && !statsError ? <p className="muted">Loading…</p>
            : !st || st.n_fetched === 0 ? <p className="muted">No transaction fetched in the last 60 minutes.</p>
            : <FeeHistogram bins={hist} />}
          <p className="muted small">
            Sampled transactions only; the sample is hash-based and independent of fee. Bins have
            unequal widths (lower edges {binEdges || "—"} sat/vB), so bar heights are counts per bin,
            not densities.
          </p>
        </div>
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <h2>Collection gaps and outages (24 h)</h2>
        {outagesError ? <p className="err" role="status">Could not load the outage log, so gaps cannot be shown: {outagesError}</p>
          : outages === null ? <p className="muted">Loading…</p>
          : outages.length === 0 ? <p className="muted">No gaps recorded in the last 24 hours (or the collector has not run that long).</p>
          : <OutagesTable outages={outages} />}
        <p className="muted small">
          Transactions announced during a gap were not observed. Even without gaps the feed is a
          sample of the public mempool.space view, never complete network coverage.
        </p>
      </div>

      <div className="split" style={{ marginTop: 16 }}>
        <div className="card">
          <h2 id="feed-h">Incoming transactions</h2>
          {lag !== null && (
            <p className="notice" role="status">
              This page’s feed has not updated for {formatDuration(lag)} although the collector reports
              newer observations (more than {FEED_LAG_S / 60} minutes behind). It is catching up by polling
              every {FEED_POLL_MS / 1000} s; reloading the page also helps.
            </p>
          )}
          <div className="filters">
            <label>
              Sort
              <select value={filter.sort} onChange={(e) => setFilter({ ...filter, sort: e.target.value as SortKey })}>
                {Object.entries(SORTS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
              </select>
            </label>
            <label>
              Min fee rate (sat/vB)
              <input type="number" min={0} step={1} value={filter.minFeeRate ?? ""} style={{ width: 90 }}
                     onChange={(e) => setFilter({ ...filter, minFeeRate: e.target.value === "" ? null : Number(e.target.value) })} />
            </label>
            <label>
              Indicator
              <select value={filter.flag ?? ""} onChange={(e) => setFilter({ ...filter, flag: e.target.value || null })}>
                <option value="">any</option>
                {flags.map((f) => <option key={f} value={f}>{f}</option>)}
              </select>
            </label>
            <label className="inline">
              <input type="checkbox" checked={filter.showFailed} onChange={(e) => setFilter({ ...filter, showFailed: e.target.checked })} />
              show failed fetches
            </label>
          </div>
          <div className="scroll feed">
            <FeedTable rows={rows} selected={selected} onSelect={onSelect} findingPriority={findingPriority} skew={skew} />
            {rows.length === 0 && <p className="muted">No observations match yet.</p>}
          </div>
          <PriorityLegend />
          <p className="muted small">Showing up to 100 of the latest {feed.length} observations held in this browser.</p>
        </div>

        <div className={selected ? "card detail has-selection" : "card detail"} ref={detailRef}>
          <h2 ref={detailHeadingRef} tabIndex={-1}>Transaction detail</h2>
          {!selected && <p className="muted">Select a transaction.</p>}
          {selected && detailLoading && !shownDetail && <p className="muted">Loading…</p>}
          {selected && detailError && <p className="err" role="status">Could not load details: {detailError}</p>}
          {shownDetail && <Detail detail={shownDetail} onSelect={onSelectFar} kind={kind} />}
        </div>
      </div>
    </div>
  );
}
