import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ALERT_STATUS_LABEL,
  alertEvidence,
  anomalyStatusText,
  baselineProgress,
  FORBIDDEN_WORDS,
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

  it("shortens hashes", () => {
    expect(shortSha("0123456789abcdef")).toBe("0123456789ab");
    expect(shortSha(null)).toBe("—");
  });

  it("reports anomaly baseline progress", () => {
    expect(baselineProgress(120)).toEqual({ ready: false, text: "120 of 500 needed", share: 0.24 });
    expect(baselineProgress(800).ready).toBe(true);
    expect(baselineProgress(null).ready).toBe(false);
    expect(baselineProgress(800, 1000)).toMatchObject({ ready: false, text: "800 of 1000 needed" });
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
    expect(findingLines(f)[0].text).toBe("Pays to an address listed by OFAC SDN (third-party extraction) (P1 and 2 other parties)");
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
