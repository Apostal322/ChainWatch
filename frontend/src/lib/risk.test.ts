import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ALERT_STATUS_LABEL,
  ALERT_TYPE_LABEL,
  alertsQuery,
  authoritiesOf,
  authorityName,
  FINDING_TYPE_LABEL,
  findingsQuery,
  isFiltered,
  NO_ALERTS_FILTER,
  NO_FINDINGS_FILTER,
  normaliseFindingsFilter,
  PAGE_SIZE,
  pageText,
  TIME_RANGES,
  alertEvidence,
  anomalyStatusText,
  ANOMALY_MIN_REFERENCE,
  baselineProgress,
  emptyRiskText,
  FORBIDDEN_WORDS,
  NO_SCREENING_TEXT,
  RISK_UNAVAILABLE_TEXT,
  riskState,
  sourceName,
  supervisedTile,
  type RiskSummary,
  filterAlerts,
  findingLayers,
  findingLines,
  formatIndicator,
  formatMs,
  formatProbability,
  graphStatusText,
  LAYER_BADGE,
  LAYER_TEXT,
  layerBadge,
  lifecycleText,
  LISTING_CAVEAT,
  partyDesignatedText,
  splitList,
  PRIORITY_INFO,
  priorityByTxid,
  priorityMarker,
  RETROSPECTIVE_TEXT,
  riskApi,
  roleText,
  screeningAvailability,
  screeningStatusText,
  shortSha,
  sortAlerts,
  sourceFreshness,
  supervisedStatusText,
  type Alert,
  type Finding,
  type IndicatorSource,
} from "./risk";

const DAY = 86400;

const alert = (id: number, over: Partial<Alert> = {}): Alert => ({
  id,
  group_key: `g${id}`,
  layer: "screening",
  kind: "source_match",
  priority: 1,
  status: "new",
  title: `alert ${id}`,
  created_at: 1000,
  updated_at: 1000,
  last_seen_at: 1000 + id,
  occurrences: 1,
  suppressed: 0,
  retrospective: false,
  txids: [],
  evidence: {},
  note: null,
  ...over,
});

const source = (over: Partial<IndicatorSource> = {}): IndicatorSource => ({
  source_id: "ofac-sdn",
  version_id: 1,
  sha256: "ab".repeat(32),
  method: "ofac-sdn-advanced-xml",
  url: "https://www.treasury.gov/x.xml",
  licence: "public domain",
  retrieved_at: 1000,
  published_at: null,
  checked_at: 1000,
  n_records: 10,
  n_rejected: 0,
  active_indicators: 10,
  first_retrieved_at: 1000,
  ...over,
});

const emptyFinding = (): Pick<Finding, "screening" | "graph" | "anomaly"> => ({
  screening: { status: "NO_KNOWN_SCREENING_MATCH", matches: [] },
  graph: { status: "NO_KNOWN_RELATION", relations: [] },
  anomaly: { status: "NO_ANOMALY", level: null, indicators: [] },
});

describe("priorities", () => {
  it("labels P1..P4 with an accessible description and no score", () => {
    expect(priorityMarker(1)).toEqual({ short: "P1", aria: PRIORITY_INFO[1].aria });
    expect(priorityMarker(4).short).toBe("P4");
    expect(PRIORITY_INFO[2].aria).toMatch(/^Review priority 2: /);
    for (const v of [null, undefined, 0, 5, 2.5]) {
      expect(priorityMarker(v as number).short).toBe("—");
    }
    expect(priorityMarker(null).aria).toMatch(/not assessed/);
    // feed rows carry risk_assessed: "n/a" (not assessed) is distinct from "—" (no finding)
    expect(priorityMarker(null, false)).toEqual({ short: "n/a", aria: expect.stringMatching(/^Not assessed/) });
    expect(priorityMarker(null, true)).toEqual({ short: "—", aria: "No review priority: assessed, no finding" });
    expect(priorityMarker(2, false).short).toBe("P2");
    // v0.8.1: a row whose screening could not run is never shown as "no finding"
    expect(priorityMarker(null, true, "insufficient_evidence").short).toBe("?");
    expect(priorityMarker(null, true, "insufficient_evidence").aria).toMatch(/not a statement of legitimacy/);
    expect(priorityMarker(null, true, "failed")).toEqual({ short: "err", aria: expect.stringMatching(/^Assessment failed/) });
    expect(priorityMarker(null, true, "no_finding").short).toBe("—");
    expect(priorityMarker(null, true, "screening_unavailable").aria).toMatch(/^Not screened/);
    expect(priorityMarker(1, true, "finding").short).toBe("P1");
  });

  it("fills feed priorities from findings, newest first wins", () => {
    const m = priorityByTxid([
      { txid: "a", priority: { level: 3, label: "", semantics: "" } },
      { txid: "a", priority: { level: 1, label: "", semantics: "" } },
      { txid: "b", priority: { level: null, label: "", semantics: "" } },
    ]);
    expect(m.get("a")).toBe(3);
    expect(m.has("b")).toBe(false);
  });
});

describe("status text", () => {
  it("maps screening outcomes with the required caveats", () => {
    expect(screeningStatusText("SCREENING_MATCH", ["ofac-sdn"]).text).toBe(`Listed by ofac-sdn (${LISTING_CAVEAT})`);
    expect(screeningStatusText("NO_KNOWN_SCREENING_MATCH").text).toBe(LAYER_TEXT.screeningNoMatch);
    expect(screeningStatusText("NO_KNOWN_SCREENING_MATCH").text).toMatch(/not proof of legitimacy/);
    // v0.8.1: a stale source is not the reassuring tone, and an unrun graph is not "no parents"
    const stale = screeningStatusText("NO_KNOWN_SCREENING_MATCH", [], true);
    expect(stale.tone).toBe("warn");
    expect(stale.text).toMatch(/stale at evaluation/);
    expect(graphStatusText("NOT_EVALUATED", 2).text).toMatch(/screening data unavailable/);
    expect(graphStatusText("NOT_EVALUATED", 0).text).toMatch(/no parent transactions/);
    expect(screeningStatusText("SCREENING_DATA_UNAVAILABLE").text).toMatch(/^Screening data unavailable/);
    expect(screeningStatusText("SCREENING_DATA_UNAVAILABLE").tone).toBe("warn");
    expect(screeningStatusText("INSUFFICIENT_INFORMATION").tone).toBe("warn");
    expect(screeningStatusText(undefined).text).toBe("Not assessed");
  });

  it("maps graph, anomaly and supervised outcomes", () => {
    expect(graphStatusText("RELATION_FOUND").text).toBe(LAYER_TEXT.graph);
    expect(graphStatusText("NO_KNOWN_RELATION").text).toMatch(/unobserved parents/);
    expect(anomalyStatusText("ANOMALY", "strong").text).toBe("Strong behavioural anomaly (unusual, not evidence of wrongdoing)");
    expect(anomalyStatusText("ANOMALY", null).text).toBe(LAYER_TEXT.anomaly);
    expect(anomalyStatusText("INSUFFICIENT_BASELINE").tone).toBe("warn");
    expect(supervisedStatusText("NO_VALIDATED_MODEL").text).toBe("No validated model: abstaining");
    expect(supervisedStatusText(undefined).text).toBe("No validated model: abstaining");
  });

  it("never uses forbidden words, except the listing caveat", () => {
    const texts: string[] = [
      ...Object.values(LAYER_TEXT),
      ...Object.values(PRIORITY_INFO).flatMap((p) => [p.name, p.aria]),
      ...Object.values(LAYER_BADGE).flatMap((b) => [b.text, b.caveat]),
      ...Object.values(ALERT_STATUS_LABEL),
      RETROSPECTIVE_TEXT,
      ...["SCREENING_MATCH", "NO_KNOWN_SCREENING_MATCH", "SCREENING_DATA_UNAVAILABLE", "INSUFFICIENT_INFORMATION", "ERROR"]
        .map((s) => screeningStatusText(s, ["x"]).text),
      ...["RELATION_FOUND", "NO_KNOWN_RELATION", "NOT_EVALUATED", "ERROR"].map((s) => graphStatusText(s).text),
      ...["ANOMALY", "NO_ANOMALY", "INSUFFICIENT_BASELINE", "INSUFFICIENT_INFORMATION", "ERROR"].map((s) => anomalyStatusText(s, "strong").text),
    ];
    for (const t of texts) expect(t.replace(LISTING_CAVEAT, "")).not.toMatch(FORBIDDEN_WORDS);
  });
});

describe("layer badges", () => {
  it("uses the investigative names", () => {
    expect(layerBadge("screening").text).toBe("Source match");
    expect(layerBadge("graph").text).toBe("Observed-graph relation");
    expect(layerBadge("anomaly").text).toBe("Behavioural anomaly");
    expect(layerBadge("other").text).toBe("other");
  });

  it("lists the layers with a finding in priority order", () => {
    const f = emptyFinding();
    expect(findingLayers(f)).toEqual([]);
    f.anomaly.status = "ANOMALY";
    f.screening.status = "SCREENING_MATCH";
    expect(findingLayers(f)).toEqual(["screening", "anomaly"]);
  });
});

describe("source freshness", () => {
  const now = 1000 + 3 * DAY;

  it("computes age and stale state from checked_at", () => {
    expect(sourceFreshness(source(), now)).toEqual({ state: "fresh", age_s: 3 * DAY, localFile: false });
    expect(sourceFreshness(source({ checked_at: 1000 - 5 * DAY }), now).state).toBe("stale");
    expect(sourceFreshness(source({ checked_at: null }), now).state).toBe("fresh"); // falls back to retrieved_at
    expect(sourceFreshness(null, now).state).toBe("never");
    expect(sourceFreshness(source({ url: "file:list.txt" }), now).localFile).toBe(true);
    // the server's staleness flag (its configured max age) wins over the local fallback
    expect(sourceFreshness(source({ stale: true }), now).state).toBe("stale");
    expect(sourceFreshness(source({ checked_at: 1000 - 9 * DAY, stale: false }), now).state).toBe("fresh");
  });

  it("reports screening unavailable when no source was loaded", () => {
    expect(screeningAvailability([], now)).toEqual({ available: false, anyStale: false, headline: "Screening data unavailable" });
    expect(screeningAvailability(null, now).available).toBe(false);
    expect(screeningAvailability([source()], now)).toMatchObject({ available: true, anyStale: false });
    const stale = screeningAvailability([source(), source({ source_id: "b", checked_at: 0 })], 1000 + 30 * DAY);
    expect(stale).toMatchObject({ available: true, anyStale: true });
    expect(stale.headline).toMatch(/^STALE/);
  });

  it("does not count a superseded source as a current, independent source", () => {
    const official = source({ source_id: "ofac-sdn", authority: "ofac-sdn", superseded_by: null });
    // superseded: checked long ago, but the server reports stale=false; never "stale" here either
    const mirror = source({ source_id: "ofac-sdn-mirror", authority: "ofac-sdn", superseded_by: "ofac-sdn", checked_at: 0, stale: false });
    expect(sourceFreshness(mirror, 1000 + 30 * DAY).state).toBe("superseded");
    const a = screeningAvailability([official, mirror], now);
    expect(a).toMatchObject({ available: true, anyStale: false });
    expect(a.headline).toBe("1 source loaded (1 superseded)");
    expect(sourceName("ofac-sdn-mirror")).toBe("OFAC SDN (third-party mirror)");
  });

  it("shortens hashes", () => {
    expect(shortSha("0123456789abcdef")).toBe("0123456789ab");
    expect(shortSha(null)).toBe("—");
  });

  it("reports anomaly baseline progress", () => {
    expect(ANOMALY_MIN_REFERENCE).toBe(1000); // DetectorConfig default
    expect(baselineProgress(120)).toEqual({ ready: false, text: "120 of 1000 needed", share: 0.12 });
    expect(baselineProgress(700, 1000)).toEqual({ ready: false, text: "700 of 1000 needed", share: 0.7 });
    expect(baselineProgress(1000).ready).toBe(true);
    expect(baselineProgress(800).ready).toBe(false);
    expect(baselineProgress(null).ready).toBe(false);
    expect(baselineProgress(800, 1000)).toMatchObject({ ready: false, text: "800 of 1000 needed" });
  });
});

describe("risk availability", () => {
  const summary = (over: Partial<RiskSummary> = {}, sources: IndicatorSource[] = [source()]): RiskSummary => ({
    mode: "live",
    enabled: true,
    window_hours: 24,
    summary: {
      since: 0, fetched: 10, assessed: 10, not_assessed: 0, by_priority: {}, screening: {}, graph: {}, anomaly: {},
      supervised: {}, assessments_with_layer_errors: 0, latency_ms: { p50: null, p95: null, max: null },
      lifecycle: {}, alerts_by_status: {}, sources,
    },
    engine: null,
    layers: null,
    ...over,
  });

  it("distinguishes unavailable risk, missing screening data and normal operation", () => {
    expect(riskState(null, 2000)).toBe("loading");
    expect(riskState(summary({ enabled: false }), 2000)).toBe("unavailable");
    expect(riskState(summary({ summary: null }), 2000)).toBe("unavailable");
    expect(riskState(summary({}, []), 2000)).toBe("no-screening");
    expect(riskState(summary(), 2000)).toBe("ok");
  });

  it("never says plain “No alerts” when nothing could raise one", () => {
    expect(emptyRiskText("alerts", "ok")).toBe("No alerts.");
    expect(emptyRiskText("alerts", "ok", " with status “New”")).toBe("No alerts with status “New”.");
    expect(emptyRiskText("alerts", "unavailable")).toMatch(new RegExp(`^${RISK_UNAVAILABLE_TEXT}`));
    expect(emptyRiskText("findings", "unavailable")).not.toMatch(/No findings yet/);
    expect(emptyRiskText("alerts", "no-screening")).toContain(NO_SCREENING_TEXT);
    expect(emptyRiskText("findings", "no-screening")).toContain("(P1/P2) cannot occur");
    for (const st of ["ok", "unavailable", "no-screening"] as const) {
      expect(emptyRiskText("alerts", st)).not.toMatch(FORBIDDEN_WORDS);
      expect(emptyRiskText("findings", st)).not.toMatch(FORBIDDEN_WORDS);
    }
  });

  it("derives the supervised tile from the reported state", () => {
    expect(supervisedTile({ state: "abstaining", reason: null }).v).toBe("No validated model: abstaining");
    expect(supervisedTile({ state: "abstaining", reason: "no gate" }).h).toBe("gate: no gate");
    expect(supervisedTile(null).v).toBe("not reported");
    expect(supervisedTile({ state: "active", reason: null }).v).toBe("State: active");
  });
});

describe("alerts", () => {
  it("sorts open alerts first, then by priority and recency, stably", () => {
    const list = [
      alert(1, { status: "dismissed", priority: 1 }),
      alert(2, { priority: 3, last_seen_at: 9999 }),
      alert(3, { priority: 1, status: "under_review" }),
      alert(4, { priority: 1 }),
    ];
    expect(sortAlerts(list).map((a) => a.id)).toEqual([4, 3, 2, 1]);
  });

  it("filters by status or by open", () => {
    const list = [alert(1, { status: "dismissed" }), alert(2), alert(3, { status: "under_review" })];
    expect(filterAlerts(list, null).length).toBe(3);
    expect(filterAlerts(list, "open").map((a) => a.id)).toEqual([2, 3]);
    expect(filterAlerts(list, "dismissed").map((a) => a.id)).toEqual([1]);
  });

  it("normalises the evidence shapes", () => {
    const m = { address: "bc1q", role: "out", source_id: "ofac-sdn" };
    expect(alertEvidence({ layer: "screening", evidence: { match: m, txid: "t" } })).toMatchObject({ match: m, txid: "t" });
    const retro = { address: "bc1q", role: "in", source_id: "s", retrospective: true };
    expect(alertEvidence({ layer: "screening", evidence: retro }).match).toEqual(retro);
    const rel = { relation: { via_parent: "p", path: ["a", "p", "this transaction"] }, limitation: "only observed", txid: "t" };
    expect(alertEvidence({ layer: "graph", evidence: rel })).toMatchObject({ limitation: "only observed", match: null });
    expect(alertEvidence({ layer: "anomaly", evidence: { indicators: [{ label: "x" }] } }).indicators.length).toBe(1);
    expect(alertEvidence({ layer: "anomaly", evidence: null as unknown as Record<string, unknown> }).indicators).toEqual([]);
  });
});

describe("explanations", () => {
  it("keeps the backend sentence and adds the statistics", () => {
    const f = formatIndicator({
      measurement: "n_outputs",
      label: "Unusual fan-out",
      unit: "outputs",
      value: 120,
      reference_median: 2,
      tail_probability: 0.000998,
      robust_z: 80.1,
      outside_reference_range: true,
      explanation: "n_outputs = 120 outputs is higher than about 99.90% of the 1000 previously sampled transactions (median 2 outputs).",
    });
    expect(f.title).toBe("Unusual fan-out");
    expect(f.sentence).toMatch(/^n_outputs = 120 outputs is higher/);
    expect(f.stats).toBe("tail probability 1.0e-3 · robust z 80.1 · outside the whole reference range");
  });

  it("composes a sentence when the explanation is missing", () => {
    const f = formatIndicator({ measurement: "fee_rate", unit: "sat/vB", value: 1.5, reference_median: 3, robust_z: null });
    expect(f.sentence).toBe("fee_rate = 1.50 sat/vB (median of recent sample 3.00 sat/vB).");
    expect(f.stats).toMatch(/unbounded/);
  });

  it("formats probabilities and latency", () => {
    expect(formatProbability(0.002)).toBe("0.002");
    expect(formatProbability(0.00012)).toBe("1.2e-4");
    expect(formatMs(0.4567)).toBe("0.46 ms");
    expect(formatMs(42.4)).toBe("42 ms");
    expect(formatMs(null)).toBe("—");
  });

  it("summarises findings per layer", () => {
    const f = emptyFinding();
    f.screening = {
      status: "SCREENING_MATCH",
      matches: [
        { role: "in", index: 0, address: "bc1q", source_id: "ofac-sdn", source_version: "x", indicator_id: 1,
          category: "sanctions", label: "PARTY", reference: "SDN 1", listed_on: null, available_at: 0, programs: [] },
        { role: "out", index: 1, address: "bc1r", source_id: "ofac-sdn", source_version: "x", indicator_id: 2,
          category: "sanctions", label: "", reference: "SDN 2", listed_on: null, available_at: 0, programs: [] },
      ],
    };
    expect(findingLines(f)).toEqual([
      { layer: "screening", text: "Spends from an address listed by OFAC SDN (official list) (PARTY) (+1 more)" },
    ]);
  });

  it("splits multi-party labels and references; describes the party designation date", () => {
    expect(splitList("A; B ;; C")).toEqual(["A", "B", "C"]);
    expect(splitList(null)).toEqual([]);
    expect(partyDesignatedText("2020-03-02")).toBe("party designated on 2020-03-02 (the address may have been added later)");
    expect(partyDesignatedText(null)).toBeNull();
    const f = emptyFinding();
    f.screening = { status: "SCREENING_MATCH", matches: [
      { role: "out", index: 0, address: "bc1q", source_id: "ofac-sdn-mirror", source_version: "x", indicator_id: 1,
        category: "sanctions", label: "P1; P2; P3", reference: "OFAC SDN profile 1; OFAC SDN profile 2", listed_on: null,
        party_designated_on: null, available_at: 0, programs: [] },
    ] };
    expect(findingLines(f)[0].text).toBe("Pays to an address listed by OFAC SDN (third-party mirror) (P1 and 2 other parties)");
  });

  it("describes roles and lifecycle", () => {
    expect(roleText("in")).toBe("spends from");
    expect(roleText("out")).toBe("pays to");
    expect(lifecycleText(null)).toMatch(/^No later state/);
    expect(lifecycleText({ state: "mined", at: 1, replaced_by: null })).toBe("Mined");
    expect(lifecycleText({ state: "replaced", at: 1, replaced_by: "abc" })).toBe("Replaced by abc");
    expect(lifecycleText({ state: "removed", at: 1, replaced_by: null })).toMatch(/^Removed/);
  });
});

describe("risk API client", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("builds the query strings", async () => {
    const calls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      calls.push(url);
      return { ok: true, status: 200, json: async () => ({ alerts: [], findings: [] }) } as Response;
    }));
    await riskApi.alerts(null);
    await riskApi.alerts("dismissed", 20);
    await riskApi.findings();
    await riskApi.summary(24);
    expect(calls).toEqual([
      "/v1/live/alerts?limit=50",
      "/v1/live/alerts?limit=20&status=dismissed",
      "/v1/live/findings?max_priority=4&limit=50",
      "/v1/live/risk/summary?hours=24",
    ]);
  });
});

describe("paged risk lists", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("sends only the active filters and the cursor", () => {
    expect(findingsQuery(NO_FINDINGS_FILTER, 25, null)).toBe("limit=25");
    expect(findingsQuery({ type: "anomaly", authority: null, priority: 4, hours: 24 }, 25, "812"))
      .toBe("limit=25&type=anomaly&priority=4&hours=24&cursor=812");
    expect(alertsQuery(NO_ALERTS_FILTER, 25, null)).toBe("limit=25"); // triage is the default
    expect(alertsQuery({ ...NO_ALERTS_FILTER, status: "open", type: "retrospective", authority: "ofac-sdn", sort: "newest" }, 10, "a_b-c"))
      .toBe("limit=10&status=open&type=retrospective&authority=ofac-sdn&sort=newest&cursor=a_b-c");
  });

  it("keeps insufficient evidence apart from priorities and sources", () => {
    const f = normaliseFindingsFilter({ type: "insufficient_evidence", authority: "ofac-sdn", priority: 1, hours: null });
    expect(f).toEqual({ type: "insufficient_evidence", authority: null, priority: null, hours: null });
    expect(normaliseFindingsFilter({ type: "source_match", authority: "ofac-sdn", priority: 1, hours: null }).priority).toBe(1);
    expect(FINDING_TYPE_LABEL.insufficient_evidence).toMatch(/not findings/);
    expect(ALERT_TYPE_LABEL.retrospective).not.toBe(ALERT_TYPE_LABEL.source_match);
  });

  it("knows when a list is filtered", () => {
    expect(isFiltered(NO_FINDINGS_FILTER)).toBe(false);
    expect(isFiltered(NO_ALERTS_FILTER)).toBe(false);
    expect(isFiltered({ ...NO_ALERTS_FILTER, sort: "newest" })).toBe(true);
    expect(isFiltered({ ...NO_FINDINGS_FILTER, hours: 1 })).toBe(true);
  });

  it("states the page and total, or that the total is unknown", () => {
    expect(pageText(0, 312, 25, ["alert", "alerts"])).toBe("Page 1 of 13 · 312 alerts");
    expect(pageText(0, 1, 25, ["alert", "alerts"])).toBe("Page 1 of 1 · 1 alert");
    expect(pageText(0, 0, 25, ["finding", "findings"])).toBe("Page 1 of 1 · 0 findings");
    // rows removed while paging: never "page 3 of 2"
    expect(pageText(2, 40, 25, ["alert", "alerts"])).toBe("Page 3 of 3 · 40 alerts");
    expect(pageText(1, null, 25, ["alert", "alerts"])).toMatch(/^Page 2 · total not reported/);
  });

  it("offers each authority once", () => {
    const sources = [{ source_id: "ofac-sdn", authority: "ofac-sdn" }, { source_id: "ofac-sdn-mirror", authority: "ofac-sdn" },
      { source_id: "local-list" }];
    expect(authoritiesOf(sources)).toEqual(["local-list", "ofac-sdn"]);
    expect(authoritiesOf(null)).toEqual([]);
    expect(authorityName("ofac-sdn")).toMatch(/official list or its mirror/);
  });

  it("says when filters, not the data, emptied a list", () => {
    expect(emptyRiskText("alerts", "ok", " matching these filters")).toBe("No alerts matching these filters.");
    expect(emptyRiskText("findings", "ok", " matching these filters")).toMatch(/^No assessments matching these filters\. Absence/);
    for (const t of [...Object.values(FINDING_TYPE_LABEL), ...Object.values(ALERT_TYPE_LABEL), ...TIME_RANGES.map((r) => r.label)]) {
      expect(t).not.toMatch(FORBIDDEN_WORDS);
      expect(t).not.toMatch(/probab|score/i);
    }
  });

  it("requests pages through the API client", async () => {
    const calls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      calls.push(url);
      return { ok: true, status: 200, json: async () => ({ alerts: [], findings: [], page: { total: 0 } }) } as Response;
    }));
    await riskApi.findingsPage({ ...NO_FINDINGS_FILTER, type: "source_match" }, PAGE_SIZE, null);
    await riskApi.alertsPage({ ...NO_ALERTS_FILTER, status: "dismissed" }, PAGE_SIZE, "xyz");
    expect(calls).toEqual(["/v1/live/findings?limit=25&type=source_match", "/v1/live/alerts?limit=25&status=dismissed&cursor=xyz"]);
  });
});
