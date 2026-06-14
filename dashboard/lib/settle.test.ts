import { describe, it, expect } from "vitest";

import { settledIn, type BucketSpec } from "./settle";

const below = (threshold: number): BucketSpec => ({
  label: "below",
  kind: "below",
  lo: null,
  hi: null,
  threshold,
});
const above = (threshold: number): BucketSpec => ({
  label: "above",
  kind: "above",
  lo: null,
  hi: null,
  threshold,
});
const range = (lo: number, hi: number): BucketSpec => ({
  label: "range",
  kind: "range",
  lo,
  hi,
  threshold: null,
});

// Temperature: whole-degree rounding (half-to-even), closed intervals.
// Mirrors probability/outcomes.py:settles.
describe("settledIn temperature (TMAX/TMIN)", () => {
  it("closed range includes both ends", () => {
    const r = range(70, 72);
    expect(settledIn(r, "TMAX", 69)).toBe(false);
    expect(settledIn(r, "TMAX", 70)).toBe(true);
    expect(settledIn(r, "TMAX", 71)).toBe(true);
    expect(settledIn(r, "TMAX", 72)).toBe(true);
    expect(settledIn(r, "TMAX", 73)).toBe(false);
  });

  it("rounds half-to-even (banker's), not half-up", () => {
    const r = range(70, 72);
    expect(settledIn(r, "TMAX", 72.5)).toBe(true); // -> 72 (even), in range
    expect(settledIn(r, "TMAX", 73.5)).toBe(false); // -> 74 (73 is odd), out
  });

  it("below is inclusive, above is inclusive", () => {
    expect(settledIn(below(59), "TMIN", 59)).toBe(true);
    expect(settledIn(below(59), "TMIN", 60)).toBe(false);
    expect(settledIn(above(80), "TMAX", 80)).toBe(true);
    expect(settledIn(above(80), "TMAX", 79)).toBe(false);
  });
});

// Precipitation: 2-decimal rounding, half-open intervals, strict below.
// Mirrors probability/precip_outcomes.py:precip_settles. A boundary value
// resolves UP to the next bracket.
describe("settledIn precipitation (PRCP)", () => {
  it("half-open range excludes the high end (resolves up)", () => {
    const r = range(2, 3);
    expect(settledIn(r, "PRCP", 1.99)).toBe(false);
    expect(settledIn(r, "PRCP", 2.0)).toBe(true); // closed low
    expect(settledIn(r, "PRCP", 2.99)).toBe(true);
    expect(settledIn(r, "PRCP", 3.0)).toBe(false); // half-open high
  });

  it("handles decimal brackets (the regression: these were dropped before)", () => {
    const r = range(0.5, 1);
    expect(settledIn(r, "PRCP", 0.49)).toBe(false);
    expect(settledIn(r, "PRCP", 0.5)).toBe(true);
    expect(settledIn(r, "PRCP", 0.99)).toBe(true);
    expect(settledIn(r, "PRCP", 1.0)).toBe(false);
  });

  it("below is strict (< threshold), above is inclusive (>= threshold)", () => {
    expect(settledIn(below(2), "PRCP", 1.99)).toBe(true);
    expect(settledIn(below(2), "PRCP", 2.0)).toBe(false); // strict, unlike temp
    expect(settledIn(above(6), "PRCP", 6.0)).toBe(true);
    expect(settledIn(above(6), "PRCP", 5.99)).toBe(false);
  });
});

// A malformed spec yields null so the caller skips the row rather than miscounting.
describe("settledIn skips on a malformed spec", () => {
  it("returns null when the needed bound is missing or the kind is unknown", () => {
    expect(settledIn({ label: "r", kind: "range", lo: 2, hi: null, threshold: null }, "PRCP", 2.5)).toBeNull();
    expect(settledIn({ label: "b", kind: "below", lo: null, hi: null, threshold: null }, "TMAX", 50)).toBeNull();
    expect(
      settledIn({ label: "x", kind: "bogus" as unknown as "range", lo: 1, hi: 2, threshold: null }, "PRCP", 1.5),
    ).toBeNull();
  });
});
