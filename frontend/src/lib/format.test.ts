import { describe, expect, it } from "vitest";
import { describeVersion, fmtHits, fmtPct, fmtScore, shortCommit } from "./format";

describe("format helpers", () => {
  it("formats missing values as an em dash, never as zero", () => {
    expect(fmtScore(null)).toBe("—");
    expect(fmtScore(Number.NaN)).toBe("—");
    expect(fmtPct(undefined)).toBe("—");
  });
  it("formats numbers", () => {
    expect(fmtScore(0.12345)).toBe("0.123");
    expect(fmtPct(0.0253)).toBe("2.5%");
    expect(fmtPct(1 / 5121)).toBe("0.02%");
    expect(fmtHits(12)).toBe("12");
    expect(fmtHits(12.333)).toBe("12.3");
  });
  it("describes registry versions", () => {
    expect(describeVersion("snap-expanding-L1-t034")).toBe("snapshot @ end of step 34 (L=1)");
    expect(describeVersion("phase1-xgboost-all")).toContain("Phase 1");
    expect(describeVersion("other")).toBe("other");
  });
  it("shortens a commit and keeps the -dirty suffix", () => {
    expect(shortCommit("0123456789abcdef")).toBe("0123456789");
    expect(shortCommit("0123456789abcdef-dirty")).toBe("0123456789-dirty");
    expect(shortCommit(null)).toBe("n/a");
  });
});
