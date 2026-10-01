import type { CalibrationData } from "./data";

// Tailwind text-* classes from the CSS color tokens. The chart sets stroke and
// fill to currentColor, so one class colors a whole series. No pos/neg: green
// and red mean win and loss elsewhere on the page.
export const LEAD_COLORS = ["text-cool", "text-warm", "text-fg", "text-fg-2"];

export type ReliabilityPoint = {
  lo: number;
  hi: number;
  predictedMean: number;
  observedFreq: number;
  count: number;
  // count / largest count across every chart, 0-1, so a dot means the same n
  // in each chart.
  weight: number;
};

export type ReliabilitySeries = {
  lead: number;
  colorClass: string;
  points: ReliabilityPoint[];
};

export type ReliabilityChartData = {
  variable: string;
  series: ReliabilitySeries[];
};

/**
 * Shape the pooled calibration object into one chart per variable and one
 * series per lead. Pure: no DOM, no rendering.
 *
 * Color is keyed on the lead's index in calibration.leads, not on which series
 * survived, so a lead keeps its color when another lead has no data. A missing
 * (variable, lead) row or an empty reliabilityBins list yields no series.
 */
export function buildReliabilitySeries(calibration: CalibrationData): ReliabilityChartData[] {
  let maxCount = 0;
  for (const row of calibration.rows) {
    for (const bin of row.cell.reliabilityBins) maxCount = Math.max(maxCount, bin.count);
  }

  return calibration.variables.map((variable) => {
    const series: ReliabilitySeries[] = [];
    calibration.leads.forEach((lead, leadIndex) => {
      const row = calibration.rows.find((r) => r.variable === variable && r.lead === lead);
      if (!row || row.cell.reliabilityBins.length === 0) return;
      const points = row.cell.reliabilityBins
        .map((bin) => ({
          lo: bin.lo,
          hi: bin.hi,
          predictedMean: bin.predicted_mean,
          observedFreq: bin.observed_freq,
          count: bin.count,
          weight: maxCount > 0 ? bin.count / maxCount : 0,
        }))
        .sort((a, b) => a.lo - b.lo);
      series.push({ lead, colorClass: LEAD_COLORS[leadIndex % LEAD_COLORS.length], points });
    });
    return { variable, series };
  });
}
