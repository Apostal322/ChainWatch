import { describe, expect, it } from "vitest";
import { ago, connectionLabel, filterSort, formatSat, mergeFeed, shortTxid, type LiveRow } from "./live";

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
    expect(connectionLabel(null)).toBe("Disconnected");
    expect(connectionLabel("reconnecting")).toBe("Reconnecting");
  });
});
