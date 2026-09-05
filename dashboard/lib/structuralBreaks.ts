// Known discontinuities in the tracking_snapshot time series. These are
// one-time measurement-base changes, not organic performance shifts, so the
// P&L chart annotates them instead of letting a reader mistake the jump for
// alpha. Append a new entry here whenever a regrade, settlement-source change,
// or gate-policy change retroactively rewrites historical grades.
//
// `date` is the first snapshot_date that reflects the change (ISO yyyy-mm-dd).
// `label` is the short tag drawn on the chart; `detail` is the footnote text.
export type StructuralBreak = {
  date: string;
  label: string;
  detail: string;
};

export const STRUCTURAL_BREAKS: readonly StructuralBreak[] = [
  {
    // The 2026-09-04 nightly snapshot was the first computed after the
    // regrade_polymarket_settlements run regraded 1106 US Polymarket TMAX/TMIN
    // markets from the ASOS hourly-METAR proxy to the weather.gov wrh source
    // (5-minute observations, local-day bucketing) that Polymarket actually
    // resolves on. ~195 historical bet grades flipped, swinging Polymarket ROI
    // from +0.88% to +17.25% in one snapshot. The jump is a corrected
    // measurement, not earned alpha; forward-going wrh-graded settlements are
    // the out-of-sample confirmation.
    date: "2026-09-04",
    label: "regrade",
    detail:
      "Sep 4: US Polymarket temp settlements regraded to the wrh source Polymarket resolves on (~195 historical grades flipped). Jump is a corrected measurement, not new alpha.",
  },
];

/**
 * Return the breaks that fall strictly inside the plotted date range, in date
 * order. A break dated on or before the first snapshot, or on/after the last,
 * sits at a chart edge and adds no visible discontinuity, so it is dropped.
 *
 * Pure and side-effect-free so it can be unit-tested without rendering.
 */
export function visibleBreaks(
  dates: string[],
  breaks: readonly StructuralBreak[] = STRUCTURAL_BREAKS,
): StructuralBreak[] {
  if (dates.length < 2) return [];
  const first = dates[0];
  const last = dates[dates.length - 1];
  return breaks
    .filter((b) => b.date > first && b.date < last)
    .sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : 0));
}
