// Risk intelligence UI for the live monitor: layer health, investigation queue, recent findings
// and the per-transaction assessment. Every finding is shown per layer with its evidence and
// limitations; nothing is combined into a single score.
import { memo, useState, type ReactNode } from "react";
import { ago, formatDuration, shortTxid, utcStamp } from "../lib/live";
import {
  ALERT_STATUSES,
  ANOMALY_MIN_REFERENCE,
  alertEvidence,
  alertStatusLabel,
  anomalyStatusText,
  baselineProgress,
  emptyRiskText,
  NO_SCREENING_TEXT,
  findingLayers,
  findingLines,
  formatIndicator,
  formatMs,
  graphStatusText,
  isOpenAlert,
  isPriority,
  LAYER_TEXT,
  layerBadge,
  lifecycleText,
  LISTING_CAVEAT,
  partyDesignatedText,
  PRIORITY_INFO,
  PRIORITY_SEMANTICS,
  priorityMarker,
  RETROSPECTIVE_TEXT,
  roleText,
  screeningAvailability,
  screeningStatusText,
  shortSha,
  sourceFreshness,
  sourceName,
  SOURCE_MAX_AGE_DAYS,
  splitList,
  STATUS_CLI_COMMAND,
  STATUS_CLI_NOTE,
  supervisedStatusText,
  supervisedTile,
  type Alert,
  type AlertStatus,
  type AnomalyIndicator,
  type Finding,
  type GraphRelation,
  type IndicatorSource,
  type Layer,
  type Lifecycle,
  type PriorityLevel,
  type RiskRecord,
  type RiskState,
  type RiskSummary,
  type ScreeningMatch,
  type Tone,
} from "../lib/risk";
import { useNow } from "../lib/useNow";

export type DataKind = "real" | "mock" | "unknown";
export type AlertFilter = AlertStatus | "open" | null;

function Ago({ ts, skew = 0 }: { ts: number; skew?: number }) {
  return <>{ago(ts, useNow() + skew)}</>;
}

/** P1..P4 marker; the accessible text is the full meaning. `decorative` when the meaning is
 * already written next to it. */
export function PriorityMarker({ level, decorative = false, assessed, state }: {
  level: number | null | undefined;
  decorative?: boolean;
  /** Feed rows: false shows "n/a" (not assessed), true "—" (assessed, no finding). */
  assessed?: boolean | null;
  /** Feed rows (v0.8.1): "insufficient_evidence" shows "?", "failed" shows "err". */
  state?: string | null;
}) {
  const m = priorityMarker(level, assessed, state);
  const cls = isPriority(level)
    ? `prio p${level}`
    : assessed === false || state === "failed" || state === "insufficient_evidence" || state === "screening_unavailable"
      ? "prio na"
      : "prio none";
  if (decorative) return <span className={cls} aria-hidden="true">{m.short}</span>;
  return (
    <span className={cls}>
      <span aria-hidden="true">{m.short}</span>
      <span className="sr-only">{m.aria}</span>
    </span>
  );
}

export function PriorityLegend() {
  return (
    <ul className="prio-legend" aria-label="Review priority legend">
      {([1, 2, 3, 4] as PriorityLevel[]).map((k) => (
        <li key={k}><PriorityMarker level={k} decorative /> <span><span className="sr-only">{PRIORITY_INFO[k].short}: </span>{PRIORITY_INFO[k].name}</span></li>
      ))}
      <li><span className="prio none" aria-hidden="true">—</span> <span>assessed, no finding (not a statement of legitimacy)</span></li>
      <li><span className="prio na" aria-hidden="true">?</span> <span>not screened (no indicator data or no screenable address), or insufficient evidence</span></li>
      <li><span className="prio na" aria-hidden="true">err</span> <span>assessment failed (not evaluated)</span></li>
      <li><span className="prio na" aria-hidden="true">n/a</span> <span>not assessed (no risk record: failed fetch, risk assessment off or unavailable)</span></li>
    </ul>
  );
}

export function LayerChip({ layer }: { layer: string }) {
  const b = layerBadge(layer);
  return <span className={`chip layer ${b.cls}`} title={b.caveat}>{b.text}</span>;
}

function StatusLine({ s }: { s: { text: string; tone: Tone } }) {
  return <p className={`layer-status ${s.tone}`}>{s.text}</p>;
}

/** Mock / unconfirmed source: findings are computed on non-network data. */
export function MockNotice({ kind }: { kind: DataKind }) {
  if (kind === "real") return null;
  return (
    <p className="banner small" role="note">
      {kind === "mock"
        ? "LOCAL MOCK / CUSTOM SOURCE: these assessments are computed on mock data. Findings here are not real detections."
        : "Data source not confirmed yet (live status not loaded): do not treat these findings as real detections."}
    </p>
  );
}

// ------------------------------------------------------------------ overview
function RiskTile(props: { k: string; v: ReactNode; h?: ReactNode; warn?: boolean; cls?: string }) {
  return (
    <div className={["tile", props.warn ? "warn" : "", props.cls ?? ""].filter(Boolean).join(" ")}>
      <div className="k">{props.k}</div>
      <div className="v">{props.v}</div>
      {props.h && <div className="h">{props.h}</div>}
    </div>
  );
}

function SourceTile({ s, skew, maxAgeDays }: { s: IndicatorSource; skew: number; maxAgeDays: number }) {
  const now = useNow() + skew;
  const f = sourceFreshness(s, now, maxAgeDays * 86400);
  const checked = s.checked_at ?? s.retrieved_at;
  if (f.state === "superseded") {
    return (
      <RiskTile
        cls="superseded"
        k="Screening source (superseded)"
        v={<>{sourceName(s.source_id)} <span className="chip">superseded by {sourceName(s.superseded_by ?? "")}</span></>}
        h={
          <>
            <code>{s.source_id}</code>{s.authority && <> · authority <code>{s.authority}</code></>} · last checked{" "}
            {checked ? ago(checked, now) : "never"} · not a current, independent source: the newer
            source of the same list is used
          </>
        }
      />
    );
  }
  return (
    <RiskTile
      warn={f.state !== "fresh"}
      k="Screening source"
      v={<>{sourceName(s.source_id)}{f.state === "stale" && <> <span className="chip stale">STALE</span></>}</>}
      h={
        <>
          <code>{s.source_id}</code>{s.authority && s.authority !== s.source_id && <> · authority <code>{s.authority}</code></>} · version <code>{shortSha(s.sha256)}</code> · last checked{" "}
          {checked ? ago(checked, now) : "never"} · {s.active_indicators.toLocaleString("en-US")} active listings
          {f.localFile && " · loaded from a local file (test or offline copy)"}
          {f.state === "stale" && ` · not refreshed within ${maxAgeDays} days: listings may be out of date`}
        </>
      }
    />
  );
}

export const RiskOverview = memo(function RiskOverview(props: {
  data: RiskSummary | null;
  error: string | null;
  skew: number;
  kind: DataKind;
  /** Collector counter `risk_unavailable`: why the risk engine could not start (null: it runs). */
  riskUnavailable?: string | null;
}) {
  const { data, error, skew, kind, riskUnavailable } = props;
  const now = Date.now() / 1000 + skew; // only for the availability headline (sources tick in SourceTile)
  const sum = data?.summary ?? null;
  const eng = data?.engine ?? null;
  const maxAgeDays = eng?.indicator_max_age_days ?? SOURCE_MAX_AGE_DAYS;
  const avail = screeningAvailability(sum?.sources, now, maxAgeDays);
  const need = eng?.anomaly_min_reference ?? ANOMALY_MIN_REFERENCE;
  const base = baselineProgress(eng?.anomaly_reference_size, need);
  const prio = sum?.by_priority ?? {};
  const openAlerts = (sum?.alerts_by_status?.new ?? 0) + (sum?.alerts_by_status?.under_review ?? 0);
  const lc = sum?.lifecycle ?? {};
  const sup = supervisedTile(eng?.supervised);
  const screened = data?.enabled === true && avail.available;

  return (
    <section className="card risk" aria-labelledby="risk-h" style={{ marginTop: 16 }}>
      <h2 id="risk-h">Risk intelligence</h2>
      <p className="muted small">
        Separate layers, each with its own evidence and limits. The review priority (P1–P4) is
        ordinal, not a score or probability; there is no single “risk score”.
      </p>
      <MockNotice kind={kind} />
      {error && <p className="err" role="status">{data ? "Could not refresh risk summary (showing the last loaded data)" : "Could not load the risk summary"}: {error}</p>}
      {!data && !error && <p className="muted">Loading…</p>}
      {riskUnavailable && (
        <p className="err small" role="note">
          The collector reports that risk assessment is unavailable: <code>{riskUnavailable}</code>
        </p>
      )}
      {data && !data.enabled && (
        <p className="notice">
          Risk intelligence has no data in this database (collected by an older collector, or risk
          assessment is disabled). Transactions below are observations only.
        </p>
      )}
      {data && (
        <div className="tiles">
          {data.enabled && avail.available
            ? (sum?.sources ?? []).map((s) => <SourceTile key={s.source_id} s={s} skew={skew} maxAgeDays={maxAgeDays} />)
            : (
              <RiskTile warn k="Screening source" v="Screening data unavailable"
                        h={<>no indicator source loaded, so nothing is screened. Load one with <code>chainwatch-live indicators update</code>.</>} />
            )}
          <RiskTile
            warn={!base.ready}
            k="Anomaly baseline"
            v={eng?.anomaly_reference_size == null ? "—"
              : base.ready ? `${eng.anomaly_reference_size.toLocaleString("en-US")} ready` : `${eng.anomaly_reference_size.toLocaleString("en-US")} / ${need.toLocaleString("en-US")}`}
            h={base.ready
              ? `reference transactions (${need.toLocaleString("en-US")} needed): anomalies are reported`
              : `reference transactions: ${base.text}; no anomaly is reported until then`}
          />
          <RiskTile k="Supervised model" v={sup.v} h={sup.h} />
          <RiskTile
            k="Assessment latency"
            v={eng?.risk_latency_ms_p50 == null && sum?.latency_ms.p50 == null ? "—" : `p50 ${formatMs(eng?.risk_latency_ms_p50 ?? sum?.latency_ms.p50)}`}
            h={`p95 ${formatMs(eng?.risk_latency_ms_p95 ?? sum?.latency_ms.p95)} (collector, recent) · ${data.window_hours} h max ${formatMs(sum?.latency_ms.max)}`}
          />
          <RiskTile
            warn={(eng?.layer_errors ?? 0) > 0 || (eng?.risk_errors ?? 0) > 0}
            k="Layer errors / failed assessments"
            v={`${eng?.layer_errors ?? "—"} / ${eng?.risk_errors ?? "—"}`}
            h={sum ? `since collector start · ${sum.assessments_with_layer_errors} assessments with a layer error in ${data.window_hours} h` : "since collector start"}
          />
          <RiskTile
            k={`Assessed (${data.window_hours} h)`}
            v={sum ? `${sum.assessed.toLocaleString("en-US")} / ${sum.fetched.toLocaleString("en-US")}` : "—"}
            warn={sum !== null && !screened}
            h={!sum ? undefined
              : screened
                ? `of fetched · P1 ${prio["1"] ?? 0} · P2 ${prio["2"] ?? 0} · P3 ${prio["3"] ?? 0} · P4 ${prio["4"] ?? 0} · no finding ${prio.none ?? 0}`
                : `of fetched · nothing was screened (no source loaded), so “no finding” is not meaningful · P3 ${prio["3"] ?? 0} · P4 ${prio["4"] ?? 0} · without an anomaly ${prio.none ?? 0}`}
          />
          <RiskTile
            k="Open alerts"
            v={sum ? String(openAlerts) : "—"}
            h={sum ? `new ${sum.alerts_by_status.new ?? 0} · under review ${sum.alerts_by_status.under_review ?? 0} · all time` : undefined}
          />
          <RiskTile
            k={`After observation (${data.window_hours} h)`}
            v={sum ? `${lc.mined ?? 0} mined` : "—"}
            h={sum ? `${lc.replaced ?? 0} replaced · ${lc.removed ?? 0} removed · ${lc.unknown ?? 0} no later state seen` : undefined}
          />
        </div>
      )}
      {data?.layers && (
        <>
          <h3>How to read each layer</h3>
          <dl className="kv layers">
            {data.layers.screening && <><dt><LayerChip layer="screening" /></dt><dd>{data.layers.screening}</dd></>}
            {data.layers.graph && <><dt><LayerChip layer="graph" /></dt><dd>{data.layers.graph}</dd></>}
            {data.layers.anomaly && <><dt><LayerChip layer="anomaly" /></dt><dd>{data.layers.anomaly}</dd></>}
            <dt><span className="chip layer layer-supervised">Supervised</span></dt>
            <dd>{data.layers.supervised ?? LAYER_TEXT.supervised}</dd>
          </dl>
        </>
      )}
    </section>
  );
});

// ------------------------------------------------------------------ evidence pieces
function MatchEvidence({ m, skew }: { m: Partial<ScreeningMatch>; skew?: number }) {
  const parties = splitList(m.label);
  const refs = splitList(m.reference);
  const designated = partyDesignatedText(m.party_designated_on);
  return (
    <div className="evidence">
      <p>
        <b>{roleText(m.role).replace(/^./, (c) => c.toUpperCase())}</b>
        {typeof m.index === "number" && ` (${m.role === "in" ? "input" : "output"} #${m.index})`}{" "}
        <code className="addr">{m.address ?? "—"}</code>
      </p>
      <p>
        Listed by <b>{sourceName(m.source_id ?? "an enabled source")}</b>
        {parties.length === 1 ? `: ${parties[0]}` : ""} <span className="muted">({LISTING_CAVEAT})</span>
      </p>
      <dl className="kv small">
        <dt>Source</dt><dd><code>{m.source_id ?? "—"}</code>{m.source_version ? <> version <code>{m.source_version}</code></> : null}
          {m.authority && m.authority !== m.source_id ? <> · authority <code>{m.authority}</code></> : null}
          {m.metadata_version ? <> · metadata <code>{m.metadata_version}</code></> : null}</dd>
        {m.category && <><dt>Category</dt><dd>{m.category}</dd></>}
        {parties.length > 1 && <><dt>Listed parties</dt><dd><ul className="plain">{parties.map((p) => <li key={p}>{p}</li>)}</ul></dd></>}
        <dt>Reference{refs.length > 1 ? "s" : ""}</dt>
        <dd>{refs.length > 1 ? <ul className="plain">{refs.map((r) => <li key={r}>{r}</li>)}</ul> : refs[0] ?? "—"}</dd>
        <dt>Listed on (source)</dt><dd>{m.listed_on ?? "no per-address listing date given by the source"}</dd>
        {designated && <><dt>Party designated</dt><dd>{designated}</dd></>}
        {m.programs && m.programs.length > 0 && <><dt>Programmes</dt><dd>{m.programs.join(", ")}</dd></>}
        <dt>Known to ChainWatch since</dt>
        <dd>{typeof m.available_at === "number" ? <>{utcStamp(m.available_at)} UTC (<Ago ts={m.available_at} skew={skew} />)</> : "—"}</dd>
      </dl>
    </div>
  );
}

function RelationEvidence({ r, onSelect }: { r: GraphRelation; onSelect?: (txid: string) => void }) {
  const [addr, parent] = r.path;
  return (
    <div className="evidence">
      <ol className="path" aria-label="Observed path, distance 2">
        <li>Address <code className="addr">{addr}</code>, listed by {sourceName(r.source_id)}{r.label ? ` (${r.label})` : ""}</li>
        <li>
          spent from by parent transaction{" "}
          {onSelect && parent ? (
            <button type="button" className="linkbtn" onClick={() => onSelect(parent)}
                    aria-label={`Show details for parent transaction ${parent}`}>
              <code>{shortTxid(parent)}</code>
            </button>
          ) : <code>{parent ? shortTxid(parent) : "—"}</code>}
        </li>
        <li>whose output this transaction spends</li>
      </ol>
      <p className="small muted">Reference {r.reference || "—"} · relation type <code>{r.type}</code> · distance {r.distance}</p>
    </div>
  );
}

function IndicatorEvidence({ list }: { list: AnomalyIndicator[] }) {
  return (
    <ul className="indicators">
      {list.map((i) => {
        const f = formatIndicator(i);
        return (
          <li key={i.measurement}>
            <b>{f.title}</b>: {f.sentence}
            {f.stats && <div className="small muted">{f.stats}</div>}
          </li>
        );
      })}
    </ul>
  );
}

// ------------------------------------------------------------------ investigation queue
function AlertItem({ a, onSelect, skew }: { a: Alert; onSelect: (txid: string) => void; skew: number }) {
  const ev = alertEvidence(a);
  const txids = a.txids ?? [];
  return (
    <li className={isOpenAlert(a.status) ? "alert-item open" : "alert-item"}>
      <div className="alert-head">
        <PriorityMarker level={a.priority} />
        <span className={`chip st st-${a.status}`}>{alertStatusLabel(a.status)}</span>
        <LayerChip layer={a.layer} />
        {a.retrospective && <span className="chip retro">{RETROSPECTIVE_TEXT}</span>}
      </div>
      <p className="alert-title">{a.title}</p>
      <p className="small muted">
        #{a.id} · {a.occurrences} occurrence{a.occurrences === 1 ? "" : "s"}
        {a.suppressed > 0 && ` · ${a.suppressed} suppressed (repeats after dismissal)`} · last seen{" "}
        <Ago ts={a.last_seen_at} skew={skew} /> · created {utcStamp(a.created_at)} UTC
      </p>
      {a.note && <p className="small">Reviewer note: {a.note}</p>}
      <details>
        <summary>Evidence and transactions<span className="sr-only"> for alert #{a.id}: {a.title}</span></summary>
        {ev.match && <MatchEvidence m={ev.match} skew={skew} />}
        {ev.relation && <RelationEvidence r={ev.relation} onSelect={onSelect} />}
        {ev.limitation && <p className="small muted">Limitation: {ev.limitation}</p>}
        {ev.indicators.length > 0 && <IndicatorEvidence list={ev.indicators} />}
        {a.layer === "anomaly" && <p className="small muted">{LAYER_TEXT.anomaly}.</p>}
        {a.layer === "graph" && <p className="small muted">{LAYER_TEXT.graph}.</p>}
        {!ev.match && !ev.relation && ev.indicators.length === 0 && <p className="muted small">No structured evidence recorded.</p>}
        <p className="small">Transactions ({txids.length}{txids.length >= 50 ? ", latest 50" : ""}):</p>
        <ul className="txlist">
          {txids.slice().reverse().map((t) => (
            <li key={t}>
              <button type="button" className="linkbtn" onClick={() => onSelect(t)} aria-label={`Show details for transaction ${t}`}>
                <code>{shortTxid(t)}</code>
              </button>
            </li>
          ))}
        </ul>
        <p className="small muted"><code>group {a.group_key}</code></p>
      </details>
    </li>
  );
}

/** Alerts rendered before "Show all": each one has interactive controls (tab stops). */
export const QUEUE_INITIAL = 20;

const FILTER_LABEL: Record<string, string> = { all: "All statuses", open: "Open (new or under review)" };

export const InvestigationQueue = memo(function InvestigationQueue(props: {
  alerts: Alert[] | null;
  error: string | null;
  filter: AlertFilter;
  onFilter: (f: AlertFilter) => void;
  onSelect: (txid: string) => void;
  skew: number;
  kind: DataKind;
  riskState?: RiskState;
}) {
  const { alerts, error, filter, onFilter, onSelect, skew, kind, riskState = "ok" } = props;
  const [showAll, setShowAll] = useState(false);
  const shown = alerts && !showAll ? alerts.slice(0, QUEUE_INITIAL) : alerts;
  const filterText = filter ? ` with status “${FILTER_LABEL[filter] ?? alertStatusLabel(filter)}”` : "";
  return (
    <section className="card" aria-labelledby="queue-h">
      <h2 id="queue-h">Investigation queue</h2>
      <p className="muted small">
        {STATUS_CLI_NOTE} <code>{STATUS_CLI_COMMAND}</code>
      </p>
      <MockNotice kind={kind} />
      <div className="filters">
        <label>
          Alert status
          <select value={filter ?? "all"} onChange={(e) => onFilter(e.target.value === "all" ? null : (e.target.value as AlertFilter))}>
            <option value="all">{FILTER_LABEL.all}</option>
            <option value="open">{FILTER_LABEL.open}</option>
            {ALERT_STATUSES.map((s) => <option key={s} value={s}>{alertStatusLabel(s)}</option>)}
          </select>
        </label>
      </div>
      {error && <p className="err" role="status">{alerts ? "Could not refresh alerts (showing the last loaded list)" : "Could not load alerts"}: {error}</p>}
      {alerts === null && !error && <p className="muted">Loading…</p>}
      {alerts !== null && alerts.length === 0 && <p className="muted">{emptyRiskText("alerts", riskState, filterText)}</p>}
      {alerts !== null && alerts.length > 0 && riskState === "no-screening" && <p className="muted small">{NO_SCREENING_TEXT}.</p>}
      {alerts !== null && alerts.length > 0 && (
        <>
          <a className="skip-inline" href="#findings-h">Skip the alert list to recent findings</a>
          <ul className="alerts scroll-y" aria-label="Alerts, open first, then by priority and last seen">
            {(shown ?? []).map((a) => <AlertItem key={a.id} a={a} onSelect={onSelect} skew={skew} />)}
          </ul>
          {alerts.length > QUEUE_INITIAL && (
            <button type="button" className="linkbtn small" aria-expanded={showAll} onClick={() => setShowAll((v) => !v)}>
              {showAll ? `Show the first ${QUEUE_INITIAL} only` : `Show all ${alerts.length} alerts`}
            </button>
          )}
        </>
      )}
      <p className="muted small">
        Grouped: repeats of the same address, relation or anomaly type update one item. Up to 200
        alerts shown.
      </p>
    </section>
  );
});

// ------------------------------------------------------------------ recent findings
export const RecentFindings = memo(function RecentFindings(props: {
  findings: Finding[] | null;
  error: string | null;
  selected: string | null;
  onSelect: (txid: string) => void;
  skew: number;
  kind: DataKind;
  riskState?: RiskState;
}) {
  const { findings, error, selected, onSelect, skew, kind, riskState = "ok" } = props;
  return (
    <section className="card" aria-labelledby="findings-h">
      <h2 id="findings-h" tabIndex={-1}>Recent findings</h2>
      <p className="muted small">Assessed transactions with a finding (P1–P4), newest first. Select one to see its full assessment.</p>
      <MockNotice kind={kind} />
      {error && <p className="err" role="status">{findings ? "Could not refresh findings (showing the last loaded list)" : "Could not load findings"}: {error}</p>}
      {findings === null && !error && <p className="muted">Loading…</p>}
      {findings !== null && findings.length === 0 && <p className="muted">{emptyRiskText("findings", riskState)}</p>}
      {findings !== null && findings.length > 0 && riskState === "no-screening" && <p className="muted small">{NO_SCREENING_TEXT}.</p>}
      {findings !== null && findings.length > 0 && (
        <ul className="findings scroll-y">
          {findings.map((f) => {
            const isSel = selected === f.txid;
            const layers = findingLayers(f);
            return (
              <li key={f.id} className={isSel ? "finding selected" : "finding"}>
                <div className="alert-head">
                  <PriorityMarker level={f.priority?.level} />
                  <button type="button" className="rowbtn" aria-pressed={isSel}
                          aria-label={`Show details for transaction ${f.txid}`} onClick={() => onSelect(f.txid)}>
                    <code>{shortTxid(f.txid)}</code>
                  </button>
                  {layers.map((l: Layer) => <LayerChip key={l} layer={l} />)}
                  <span className="muted small">seen <Ago ts={f.observed_at} skew={skew} /></span>
                  {f.lifecycle?.state && <span className="chip">{f.lifecycle.state}</span>}
                </div>
                <ul className="lines">
                  {findingLines(f).map((ln, i) => <li key={i}>{ln.text}</li>)}
                </ul>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
});

// ------------------------------------------------------------------ transaction detail
function LifecycleValue({ lc, onSelect }: { lc: Lifecycle | null | undefined; onSelect: (t: string) => void }) {
  if (lc?.state === "replaced" && lc.replaced_by) {
    return (
      <>
        Replaced by{" "}
        <button type="button" className="linkbtn" onClick={() => onSelect(lc.replaced_by!)}
                aria-label={`Show details for replacing transaction ${lc.replaced_by}`}>
          <code>{shortTxid(lc.replaced_by)}</code>
        </button>
        {lc.at ? ` at ${utcStamp(lc.at)} UTC` : ""}
      </>
    );
  }
  return <>{lifecycleText(lc)}{lc?.state && lc.at ? ` at ${utcStamp(lc.at)} UTC` : ""}</>;
}

export function RiskAssessment(props: {
  risk: RiskRecord | null | undefined;
  lifecycle: Lifecycle | null | undefined;
  onSelect: (txid: string) => void;
  kind: DataKind;
}) {
  const { risk, lifecycle, onSelect, kind } = props;
  if (!risk) {
    return (
      <section className="risk-block" aria-labelledby="ra-h">
        <h3 id="ra-h">Risk assessment</h3>
        <p className="muted">
          Not assessed: no risk record for this transaction (fetch failed, assessment disabled, or
          collected before risk intelligence existed). This says nothing either way.
        </p>
        <dl className="kv"><dt>After observation</dt><dd><LifecycleValue lc={lifecycle} onSelect={onSelect} /></dd></dl>
      </section>
    );
  }
  const sc = risk.screening ?? { status: undefined };
  const matches = sc.matches ?? [];
  const cov = sc.coverage;
  const gr = risk.graph ?? { status: undefined };
  const an = risk.anomaly ?? { status: undefined };
  const level = risk.priority?.level ?? null;
  const matchSources = [...new Set(matches.map((m) => sourceName(m.source_id)))];
  return (
    <section className="risk-block" aria-labelledby="ra-h">
      <h3 id="ra-h">Risk assessment</h3>
      <MockNotice kind={kind} />
      <p className="prio-line">
        <PriorityMarker level={level} decorative />{" "}
        {isPriority(level) ? <b><span className="sr-only">Review priority {level}: </span>{PRIORITY_INFO[level].name}</b> : <b>No finding (not a statement of legitimacy)</b>}
      </p>
      <p className="small muted">{PRIORITY_SEMANTICS}</p>
      {risk.insufficient_evidence && (
        <p className="banner small">
          Insufficient evidence: neither screening nor the anomaly detector could assess this
          transaction, so “no finding” carries no information here.
        </p>
      )}
      {risk.errors?.length > 0 && (
        <div className="err small" role="note">
          Layer errors (other layers still reported):
          <ul>{risk.errors.map((e, i) => <li key={i}><b>{e.layer}</b>: <code>{e.error}</code></li>)}</ul>
        </div>
      )}
      <dl className="kv">
        <dt>Observed (first seen)</dt><dd>{utcStamp(risk.observed_at)} UTC</dd>
        <dt>Fetched</dt><dd>{utcStamp(risk.fetched_at)} UTC</dd>
        <dt>Evaluated</dt><dd>{utcStamp(risk.evaluated_at)} UTC · {formatMs(risk.latency_ms)}</dd>
        <dt>After observation</dt><dd><LifecycleValue lc={lifecycle} onSelect={onSelect} /></dd>
        <dt>Engine · features</dt><dd><code>{risk.engine_version}</code> · <code>{risk.feature_schema}</code></dd>
      </dl>

      <h4>Screening (source match)</h4>
      <StatusLine s={screeningStatusText(sc.status, matchSources,
        (sc.sources ?? []).some((s) => s.stale && !s.superseded_by))} />
      {matches.map((m, i) => <MatchEvidence key={`${m.indicator_id}-${m.role}-${m.index}-${i}`} m={m} />)}
      {cov && (
        <p className="small">
          Coverage: {cov.addresses_screened} address{cov.addresses_screened === 1 ? "" : "es"} screened ·{" "}
          {cov.outputs_without_address} output{cov.outputs_without_address === 1 ? "" : "s"} and{" "}
          {cov.inputs_without_address} input{cov.inputs_without_address === 1 ? "" : "s"} without an address (unaddressable scripts) ·{" "}
          {cov.inputs_missing_prevout} input{cov.inputs_missing_prevout === 1 ? "" : "s"} with a missing previous output
          {cov.truncated ? " · address list truncated (very large transaction)" : ""}
        </p>
      )}
      {sc.sources && sc.sources.length > 0 && (
        <p className="small muted">
          Sources at evaluation:{" "}
          {sc.sources.map((s, i) => (
            <span key={s.source_id}>
              {i > 0 && "; "}
              <code>{s.source_id}</code> version <code>{s.version}</code>, checked {formatDuration(s.age_s)} before evaluation
              {s.superseded_by ? <> <span className="chip">superseded by {sourceName(s.superseded_by)}</span></>
                : s.stale && <> <span className="chip stale">STALE</span></>}
            </span>
          ))}
        </p>
      )}

      <h4>Observed-graph relation</h4>
      <StatusLine s={graphStatusText(gr.status, gr.coverage?.parents)} />
      {(gr.relations ?? []).map((r, i) => <RelationEvidence key={i} r={r} onSelect={onSelect} />)}
      {gr.coverage && (
        <p className="small">
          Coverage: {gr.coverage.parents_observed} of {gr.coverage.parents} parent transaction
          {gr.coverage.parents === 1 ? "" : "s"} observed by this collector
          {typeof gr.coverage.parents_checked === "number" && gr.coverage.parents_checked < gr.coverage.parents
            ? ` · ${gr.coverage.parents_checked} looked up (capped)` : ""}
          {typeof gr.relations_total === "number" && gr.relations_total > (gr.relations ?? []).length
            ? ` · ${gr.relations_total} relations found, ${(gr.relations ?? []).length} shown` : ""}
        </p>
      )}
      {gr.limitation && <p className="small muted">Limitation: {gr.limitation}</p>}

      <h4>Behavioural anomaly</h4>
      <StatusLine s={anomalyStatusText(an.status, an.level)} />
      {(an.indicators ?? []).length > 0 && <IndicatorEvidence list={an.indicators ?? []} />}
      {an.detail && <p className="small">{an.detail}</p>}
      {an.detector && (
        <p className="small muted">
          Detector <code>{an.detector.version}</code>: compared with {an.detector.reference_size} earlier sampled
          transactions (window {an.detector.window}, flag at tail probability ≤ {an.detector.alpha}). Unusual is not
          evidence of wrongdoing: batch payouts, consolidations and CoinJoins are unusual and legitimate.
        </p>
      )}

      <h4>Supervised model</h4>
      <StatusLine s={supervisedStatusText(risk.supervised?.status)} />
      {risk.supervised?.detail && <p className="small muted">{risk.supervised.detail}</p>}
      {risk.supervised?.gate && <p className="small muted">Gate: {risk.supervised.gate}</p>}
    </section>
  );
}
