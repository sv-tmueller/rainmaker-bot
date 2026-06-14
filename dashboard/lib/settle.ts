// Settlement grading for the dashboard's "Recent Settled" list.
//
// Mirrors the Python single-source-of-truth grading:
//   - probability/outcomes.py:settles        (temperature: TMAX/TMIN)
//   - probability/precip_outcomes.py:precip_settles  (PRCP)
//
// The bounds (kind, lo, hi, threshold) are NOT parsed from the bucket label here;
// they are read from the market's structured `outcome_spec`, which the recorder
// writes for every bucket on both venues. That avoids re-deriving bounds from a
// free-form display label (Kalshi precip labels in particular are not parseable
// back into bounds), which is what previously dropped every settled precip bet.

export type BucketKind = "below" | "above" | "range";

export type BucketSpec = {
  label: string;
  kind: BucketKind;
  lo: number | null;
  hi: number | null;
  threshold: number | null;
};

// Python round() is half-to-even; Math.round is half-up. Temperature settles on
// whole degrees F and NOAA actuals can land on .5 (45.5F == 7.5C), so mirror it.
export function roundHalfEven(x: number): number {
  const f = Math.floor(x);
  if (x - f === 0.5) return f % 2 === 0 ? f : f + 1;
  return Math.round(x);
}

// PRCP settles on the NOAA monthly total, reported to 2 decimals; this mirrors
// Python round(x, 2). NOAA values are already 2-dp, so half-even vs half-up never
// bites here - this only clears float noise before the comparison.
function round2(x: number): number {
  return Math.round(x * 100) / 100;
}

// Whether the settled actual lands in this bucket. Returns null when the spec is
// malformed (missing the bound it needs, or an unknown kind), so the caller skips
// the row rather than scoring it wrong. Temperature uses whole-degree rounding and
// closed intervals; PRCP uses 2-dp rounding and half-open intervals (a boundary
// value resolves up to the next bracket).
export function settledIn(
  spec: BucketSpec,
  variable: string,
  actual: number,
): boolean | null {
  const precip = variable === "PRCP";
  const v = precip ? round2(actual) : roundHalfEven(actual);
  if (spec.kind === "below") {
    if (spec.threshold === null) return null;
    return precip ? v < spec.threshold : v <= spec.threshold;
  }
  if (spec.kind === "above") {
    if (spec.threshold === null) return null;
    return v >= spec.threshold;
  }
  if (spec.kind === "range") {
    if (spec.lo === null || spec.hi === null) return null;
    return precip ? spec.lo <= v && v < spec.hi : spec.lo <= v && v <= spec.hi;
  }
  return null;
}
