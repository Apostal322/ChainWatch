import { useEffect, useMemo, useState } from "react";
import { Bar, BarChart, CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { ApiError, type Info } from "../lib/api";
import {
  ago,
  connectionLabel,
  filterSort,
  formatDuration,
  formatSat,
  liveApi,
  OUTAGE_LABEL,
  outageDuration,
  mergeFeed,
  shortTxid,
  type Connection,
  type FeedFilter,
  type LiveDetail,
  type LiveRow,
  type LiveStats,
  type LiveStatus,
  type Outage,
  type SortKey,
} from "../lib/live";
import { Tile } from "./common";

const SORTS: Record<SortKey, string> = {
  newest: "Newest first",
  fee_rate: "Fee rate (high first)",
  vsize: "Virtual size (large first)",
  n_outputs: "Outputs (most first)",
  fee: "Absolute fee (high first)",
};

function ConnectionBadge({ c }: { c: Connection | null }) {
  const cls = c === "connected" ? "ok" : c === "reconnecting" ? "warn" : "off";
  return (
    <span className={`badge conn ${cls}`} role="status">
      <i aria-hidden="true" /> {connectionLabel(c)}
    </span>
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

export default function LiveView({ info }: { info: Info }) {
  const [status, setStatus] = useState<LiveStatus | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [stats, setStats] = useState<LiveStats | null>(null);
  const [outages, setOutages] = useState<Outage[]>([]);
  const [feed, setFeed] = useState<LiveRow[]>([]);
  const [stream, setStream] = useState<"open" | "reconnecting">("reconnecting");
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<LiveDetail | null>(null);
  const [filter, setFilter] = useState<FeedFilter>({ minFeeRate: null, flag: null, showFailed: false, sort: "newest" });
  const [now, setNow] = useState(Date.now() / 1000);

  const enabled = info.live_enabled;

  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    const poll = () =>
      liveApi
        .status()
        .then((s) => alive && (setStatus(s), setProblem(null)))
        .catch((e) => alive && setProblem(e instanceof ApiError && e.code === "live_no_data"
          ? "No live data yet: start `chainwatch --config configs/live.yaml live-collect`."
          : `Live API unavailable: ${String(e.message ?? e)}`));
    const pollStats = () => {
      liveApi.stats(60).then((s) => alive && setStats(s)).catch(() => undefined);
      liveApi.outages(24).then((o) => alive && setOutages(o.outages)).catch(() => undefined);
    };
    poll();
    pollStats();
    liveApi.recent(200).then((r) => alive && setFeed((f) => mergeFeed(f, r.rows))).catch(() => undefined);
    const t1 = setInterval(poll, 5000);
    const t2 = setInterval(pollStats, 15000);
    const t3 = setInterval(() => setNow(Date.now() / 1000), 1000);
    // Server-sent events; the browser reconnects automatically with Last-Event-ID.
    const es = new EventSource("/v1/live/events");
    es.onopen = () => setStream("open");
    es.onerror = () => setStream("reconnecting");
    es.addEventListener("tx", (ev) => {
      const row = JSON.parse((ev as MessageEvent).data) as LiveRow;
      setFeed((f) => mergeFeed(f, [row]));
    });
    return () => {
      alive = false;
      clearInterval(t1);
      clearInterval(t2);
      clearInterval(t3);
      es.close();
    };
  }, [enabled]);

  useEffect(() => {
    if (!selected) return setDetail(null);
    liveApi.tx(selected).then(setDetail).catch(() => setDetail(null));
  }, [selected]);

  const rows = useMemo(() => filterSort(feed, filter).slice(0, 100), [feed, filter]);
  if (!enabled) return <Disabled />;

  const c = status?.collector?.counters ?? {};
  const lastMinute = stats?.per_minute.length ? stats.per_minute[stats.per_minute.length - 1] : null;
  const throughput = (stats?.per_minute ?? []).map((m) => ({
    t: new Date(m.t * 1000).toISOString().slice(11, 16),
    announced: m.received,
  }));
  const hist = (stats?.fee_rate_histogram ?? []).map((b) => ({
    bin: b.to === null ? `≥${b.from}` : `${b.from}–${b.to}`,
    count: b.count,
  }));
  const flags = Object.keys(status?.indicators ?? {});

  return (
    <div>
      <div className="live-head">
        {status && !status.source_is_public_mempool ? (
          <span className="badge synthetic">LOCAL MOCK / CUSTOM SOURCE: not real network data</span>
        ) : (
          <span className="badge real">REAL BITCOIN NETWORK DATA</span>
        )}
        <ConnectionBadge c={status?.connection ?? null} />
        <span className="muted">
          event stream: {stream === "open" ? "open" : "reconnecting"} · source: {status?.source_url ?? "—"}
          {status?.via ? ` · via ${status.via}` : ""}
        </span>
      </div>
      <div className="notice">
        {status?.notice ??
          "Real Bitcoin mempool observations. Coverage is partial (sampled, rate-limited); indicators are rule-based, not fraud predictions."}{" "}
        Sampling: {status ? `${(status.coverage.sample_rate * 100).toFixed(0)}% of announced txids` : "—"}, at most{" "}
        {status?.coverage.fetch_per_minute ?? "—"} detail requests/min.
      </div>
      {problem && <p className="err">{problem}</p>}

      <div className="tiles">
        <Tile k="Announced txids (last minute)" v={lastMinute ? String(lastMinute.received) : "—"} h="as received by this collector" />
        <Tile k="Details fetched (60 min)" v={stats ? String(stats.totals.fetched) : "—"}
              h={stats ? `${stats.totals.failed} failed · ${stats.totals.dropped} dropped (backpressure)` : undefined} />
        <Tile k="Fetched share of announced" v={status?.coverage.fetched_share_of_announced != null
              ? `${(status.coverage.fetched_share_of_announced * 100).toFixed(1)}%` : "—"} h="since collector start" />
        <Tile k="Median fee rate (60 min)" v={stats?.fee_rate_quantiles ? `${stats.fee_rate_quantiles.p50.toFixed(1)} sat/vB` : "—"}
              h={stats?.fee_rate_quantiles ? `p10 ${stats.fee_rate_quantiles.p10.toFixed(1)} · p90 ${stats.fee_rate_quantiles.p90.toFixed(1)}` : undefined} />
        <Tile k="Median virtual size" v={stats?.vsize_quantiles ? `${stats.vsize_quantiles.p50} vB` : "—"}
              h={stats?.vsize_quantiles ? `p90 ${stats.vsize_quantiles.p90} vB` : undefined} />
        <Tile k="Reconnects / rate-limited" v={`${status?.collector?.reconnects ?? 0} / ${c.rate_limited ?? 0}`} h="since collector start" />
        <Tile k="Last successful observation" v={status?.last_observation_at ? ago(status.last_observation_at, now) : "—"}
              h={status?.last_observation_at ? new Date(status.last_observation_at * 1000).toISOString().replace("T", " ").slice(0, 19) + " UTC" : "no transaction fetched yet"} />
        <Tile k="Observed share (24 h)" v={status?.availability_24h?.observed_share != null ? `${(status.availability_24h.observed_share * 100).toFixed(1)}%` : "—"}
              h={status?.availability_24h ? `down ${formatDuration(status.availability_24h.down_s)} of ${formatDuration(status.availability_24h.window_s)} · ${status.availability_24h.sequence_gaps} missed-update gaps` : undefined} />
      </div>

      <div className="split">
        <div className="card">
          <h2>Announced transactions per minute</h2>
          <ResponsiveContainer width="100%" height={180}>
            <LineChart data={throughput} margin={{ top: 8, right: 16, bottom: 4, left: 0 }}>
              <CartesianGrid stroke="var(--grid)" vertical={false} />
              <XAxis dataKey="t" stroke="var(--axis)" tick={{ fill: "var(--muted)", fontSize: 11 }} tickLine={false} minTickGap={24} />
              <YAxis stroke="var(--axis)" tick={{ fill: "var(--muted)", fontSize: 11 }} tickLine={false} width={44} allowDecimals={false} />
              <Tooltip cursor={{ stroke: "var(--axis)" }} contentStyle={{ fontSize: 12 }} formatter={(v) => [v, "announced txids"]} />
              <Line dataKey="announced" stroke="var(--s1)" strokeWidth={2} dot={false} isAnimationActive={false} />
            </LineChart>
          </ResponsiveContainer>
          <p className="muted small">UTC minutes; the latest minute is still filling. Counts what this collector received, not network totals.</p>
        </div>
        <div className="card">
          <h2>Fee rate of fetched transactions (sat/vB, 60 min)</h2>
          <ResponsiveContainer width="100%" height={180}>
            <BarChart data={hist} margin={{ top: 8, right: 16, bottom: 4, left: 0 }}>
              <CartesianGrid stroke="var(--grid)" vertical={false} />
              <XAxis dataKey="bin" stroke="var(--axis)" tick={{ fill: "var(--muted)", fontSize: 11 }} tickLine={false} />
              <YAxis stroke="var(--axis)" tick={{ fill: "var(--muted)", fontSize: 11 }} tickLine={false} width={44} allowDecimals={false} />
              <Tooltip cursor={{ fill: "var(--surface-2)" }} contentStyle={{ fontSize: 12 }} formatter={(v) => [v, "transactions"]} />
              <Bar dataKey="count" fill="var(--s1)" radius={[4, 4, 0, 0]} isAnimationActive={false} />
            </BarChart>
          </ResponsiveContainer>
          <p className="muted small">Sampled transactions only; the sample is hash-based and independent of fee.</p>
        </div>
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <h2>Collection gaps and outages (24 h)</h2>
        {outages.length === 0 ? (
          <p className="muted">No gaps recorded in the last 24 hours (or the collector has not run that long).</p>
        ) : (
          <div className="scroll">
            <table>
              <thead>
                <tr><th>Started (UTC)</th><th>Type</th><th className="num">Duration</th><th>Detail</th></tr>
              </thead>
              <tbody>
                {outages.map((o) => (
                  <tr key={o.id}>
                    <td className="nowrap">{new Date(o.started_at * 1000).toISOString().replace("T", " ").slice(0, 19)}</td>
                    <td>{OUTAGE_LABEL[o.kind]}{o.ended_at === null ? " (ongoing)" : ""}</td>
                    <td className="num">{o.kind === "sequence_gap" ? "—" : formatDuration(outageDuration(o, now))}</td>
                    <td className="muted">{o.detail}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="muted small">
          Transactions announced during a gap were not observed. Even without gaps the feed is a
          sample of the public mempool.space view, never complete network coverage.
        </p>
      </div>

      <div className="split" style={{ marginTop: 16 }}>
        <div className="card">
          <h2>Incoming transactions</h2>
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
            <table>
              <thead>
                <tr>
                  <th>Seen</th><th>Transaction</th><th className="num">Fee rate</th><th className="num">vsize</th>
                  <th className="num">In / out</th><th>Indicators</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.id} className="clickable" aria-selected={selected === r.txid} onClick={() => setSelected(r.txid)}>
                    <td className="muted nowrap">{ago(r.first_seen, now)}</td>
                    <td><code className="nowrap">{shortTxid(r.txid)}</code></td>
                    <td className="num">{r.outcome === "fetched" ? `${r.fee_rate?.toFixed(1)}` : <span className="muted">{r.error}</span>}</td>
                    <td className="num">{r.vsize ?? "—"}</td>
                    <td className="num">{r.outcome === "fetched" ? `${r.n_inputs} / ${r.n_outputs}` : "—"}</td>
                    <td>{r.flags.map((f) => <span key={f} className="chip flag">{f}</span>)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {rows.length === 0 && <p className="muted">No observations match yet.</p>}
          </div>
          <p className="muted small">Showing up to 100 of the latest {feed.length} observations held in this browser.</p>
        </div>

        <div className="card">
          <h2>Transaction detail</h2>
          {!detail && <p className="muted">Select a transaction.</p>}
          {detail && (
            <div>
              <p>
                <code style={{ wordBreak: "break-all" }}>{detail.txid}</code>{" "}
                <a href={detail.explorer_url} target="_blank" rel="noopener noreferrer">View on mempool.space ↗</a>
              </p>
              {detail.outcome === "failed" ? (
                <p className="muted">Details could not be fetched ({detail.error}). The transaction may have been replaced, mined or evicted.</p>
              ) : detail.features && (
                <>
                  <dl className="kv">
                    <dt>First seen</dt><dd>{new Date(detail.first_seen * 1000).toISOString().replace("T", " ").slice(0, 19)} UTC</dd>
                    <dt>Confirmed when fetched</dt><dd>{detail.features.confirmed_at_fetch ? "yes" : "no (in mempool)"}</dd>
                    <dt>Fee / fee rate</dt><dd>{formatSat(detail.features.fee)} · {detail.features.fee_rate.toFixed(2)} sat/vB</dd>
                    <dt>Size / vsize / weight</dt><dd>{detail.features.size} B · {detail.features.vsize} vB · {detail.features.weight} WU</dd>
                    <dt>Inputs / outputs</dt><dd>{detail.features.n_inputs} / {detail.features.n_outputs} (from {detail.features.n_parent_txs} parent tx)</dd>
                    <dt>Version / locktime</dt><dd>{detail.features.version} · {detail.features.locktime} ({detail.features.locktime_kind})</dd>
                    <dt>SegWit · BIP125 signal</dt><dd>{detail.features.segwit ? "yes" : "no"} · {detail.features.rbf_signal ? "yes" : "no"}</dd>
                    <dt>Sum of outputs</dt><dd>{formatSat(detail.features.output_value_total)} <span className="muted">(includes any change; not a payment amount)</span></dd>
                  </dl>
                  <h3>Output values ({detail.features.output_values.length} of {detail.features.n_outputs} shown)</h3>
                  <p className="small">{detail.features.output_values.map(formatSat).join(" · ")}</p>
                  <h3>Spent outputs (direct input references)</h3>
                  <ul className="refs">
                    {detail.features.input_refs.map((ref) => {
                      const [parent, vout] = ref.split(":");
                      return (
                        <li key={ref}>
                          <a href={`${detail.explorer_url.replace(detail.txid, parent)}`} target="_blank" rel="noopener noreferrer">
                            <code>{shortTxid(parent)}</code>
                          </a>
                          :{vout}
                        </li>
                      );
                    })}
                  </ul>
                  <p className="muted small">Links between transactions are observable spends. They do not identify owners or wallets.</p>
                </>
              )}
              <h3>Rule-based indicators</h3>
              {detail.flags.length === 0 ? <p className="muted">None.</p> : (
                <ul>{detail.flags.map((f) => <li key={f}><b>{f}</b>: {detail.flag_descriptions[f]}</li>)}</ul>
              )}
              <p className="muted small">Indicators flag observable properties only. They are not risk scores and say nothing about legality.</p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
