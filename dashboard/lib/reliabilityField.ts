import type { CalibrationData, ReliabilityBin } from "./data";

export type ReliabilityBar = {
  lead: number;
  leadIndex: number;
  binIndex: number;
  lo: number;
  hi: number;
  predictedMean: number;
  observedFreq: number;
  count: number;
  // Count relative to the largest bin count in the field, 0-1. Drives bar
  // opacity/width so thin bins look thin instead of full-size and empty.
  weight: number;
};

export type ReliabilityField = {
  variable: string;
  leads: number[];
  bins: Array<{ lo: number; hi: number }>;
  bars: ReliabilityBar[];
};

/**
 * Shape the pooled calibration object into a lead x predicted-probability-bin
 * grid of bars for the selected variable. Pure: no DOM, no rendering.
 *
 * The bin axis is the union of (lo, hi) ranges seen across every lead for the
 * variable, sorted by lo, so a lead missing a particular bin still lines up
 * with the other leads' bars on that axis. A missing (variable, lead) row or
 * an empty reliabilityBins list simply contributes no bars for that lead
 * (rendered as a gap by the caller), never a thrown error.
 */
export function buildReliabilityField(
  calibration: CalibrationData,
  variable: string,
): ReliabilityField {
  const rows = calibration.rows.filter((r) => r.variable === variable);
  const leads = [...calibration.leads];

  const binByKey = new Map<string, { lo: number; hi: number }>();
  for (const row of rows) {
    for (const bin of row.cell.reliabilityBins) {
      const key = binKey(bin);
      if (!binByKey.has(key)) binByKey.set(key, { lo: bin.lo, hi: bin.hi });
    }
  }
  const bins = [...binByKey.values()].sort((a, b) => a.lo - b.lo);
  const binIndexByKey = new Map(bins.map((b, i) => [binKey(b), i]));

  const rowByLead = new Map(rows.map((r) => [r.lead, r]));

  let maxCount = 0;
  const bars: ReliabilityBar[] = [];
  leads.forEach((lead, leadIndex) => {
    const row = rowByLead.get(lead);
    if (!row) return;
    for (const bin of row.cell.reliabilityBins) {
      const binIndex = binIndexByKey.get(binKey(bin));
      if (binIndex === undefined) continue;
      maxCount = Math.max(maxCount, bin.count);
      bars.push({
        lead,
        leadIndex,
        binIndex,
        lo: bin.lo,
        hi: bin.hi,
        predictedMean: bin.predicted_mean,
        observedFreq: bin.observed_freq,
        count: bin.count,
        weight: 0,
      });
    }
  });

  for (const bar of bars) {
    bar.weight = maxCount > 0 ? bar.count / maxCount : 0;
  }

  return { variable, leads, bins, bars };
}

function binKey(bin: Pick<ReliabilityBin, "lo" | "hi">): string {
  return `${bin.lo}|${bin.hi}`;
}
