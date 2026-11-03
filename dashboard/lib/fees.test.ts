import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { POLYMARKET_TAKER_FEE_RATE, shownEdge, takerFeePerShare } from "./fees";

describe("takerFeePerShare", () => {
  it("is rate * ask * (1 - ask)", () => {
    expect(takerFeePerShare(0.7, 0.05)).toBeCloseTo(0.0105, 10);
    expect(takerFeePerShare(0.5, 0.05)).toBeCloseTo(0.0125, 10);
  });

  it("is zero at the price extremes", () => {
    expect(takerFeePerShare(0, 0.05)).toBe(0);
    expect(takerFeePerShare(1, 0.05)).toBe(0);
  });
});

describe("shownEdge", () => {
  it("nets the fee off a Polymarket row with a known ask", () => {
    const s = shownEdge(0.15, 0.7, "polymarket");
    expect(s.basis).toBe("net");
    expect(s.value).toBeCloseTo(0.1395, 10);
  });

  it("keeps a Kalshi row gross", () => {
    expect(shownEdge(0.15, 0.7, "kalshi")).toEqual({ value: 0.15, basis: "gross" });
  });

  it("falls back to gross for a Polymarket row with no ask", () => {
    expect(shownEdge(0.15, null, "polymarket")).toEqual({ value: 0.15, basis: "gross" });
  });
});

describe("POLYMARKET_TAKER_FEE_RATE", () => {
  it("matches the Python constant in src/rainmaker/config.py", () => {
    const src = readFileSync(new URL("../../src/rainmaker/config.py", import.meta.url), "utf8");
    const m = src.match(/^POLYMARKET_TAKER_FEE_RATE = (\S+)$/m);
    expect(m).not.toBeNull();
    expect(POLYMARKET_TAKER_FEE_RATE).toBe(Number(m![1]));
  });
});
