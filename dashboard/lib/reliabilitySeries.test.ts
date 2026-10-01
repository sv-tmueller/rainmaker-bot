import { describe, expect, test } from "vitest";
import { LEAD_COLORS, buildReliabilitySeries } from "./reliabilitySeries";
import type { CalibrationCell, CalibrationData } from "./data";

function cell(bins: CalibrationCell["reliabilityBins"]): CalibrationCell {
  return { n: 0, crps: 1, coverage50: 0.5, coverage80: 0.8, coverage90: 0.9, reliabilityBins: bins };
}

// 2 variables x 2 leads. The largest bin count across everything is 100 (TMAX, 1d).
function fixture(): CalibrationData {
  return {
    leads: [1, 3],
    variables: ["TMAX", "TMIN"],
    rows: [
      {
        variable: "TMAX",
        lead: 1,
        cell: cell([
          { lo: 0.5, hi: 1.0, predicted_mean: 0.7, observed_freq: 0.75, count: 25 },
          { lo: 0.0, hi: 0.5, predicted_mean: 0.3, observed_freq: 0.28, count: 100 },
        ]),
      },
      {
        variable: "TMAX",
        lead: 3,
        cell: cell([{ lo: 0.0, hi: 0.5, predicted_mean: 0.32, observed_freq: 0.4, count: 49 }]),
      },
      {
        variable: "TMIN",
        lead: 1,
        cell: cell([{ lo: 0.0, hi: 1.0, predicted_mean: 0.5, observed_freq: 0.5, count: 4 }]),
      },
      {
        variable: "TMIN",
        lead: 3,
        cell: cell([{ lo: 0.0, hi: 1.0, predicted_mean: 0.6, observed_freq: 0.55, count: 16 }]),
      },
    ],
  };
}

describe("buildReliabilitySeries", () => {
  test("normal data: one chart per variable, one series per lead, points sorted by lo", () => {
    const charts = buildReliabilitySeries(fixture());

    expect(charts.map((c) => c.variable)).toEqual(["TMAX", "TMIN"]);
    const tmax = charts[0];
    expect(tmax.series.map((s) => s.lead)).toEqual([1, 3]);

    expect(tmax.series[0].points).toEqual([
      { lo: 0.0, hi: 0.5, predictedMean: 0.3, observedFreq: 0.28, count: 100, weight: 1 },
      { lo: 0.5, hi: 1.0, predictedMean: 0.7, observedFreq: 0.75, count: 25, weight: 0.25 },
    ]);
  });

  test("weight is count over the largest count across all charts", () => {
    const charts = buildReliabilitySeries(fixture());

    // One scale for every chart, so a dot means the same n everywhere.
    expect(charts[1].series[1].points[0].weight).toBeCloseTo(0.16);
    expect(charts[0].series[1].points[0].weight).toBeCloseTo(0.49);
  });

  test("palette avoids pos/neg, which mean win and loss elsewhere on the page", () => {
    expect(LEAD_COLORS).toEqual(["text-cool", "text-warm", "text-fg", "text-fg-2"]);
  });

  test("colors follow the lead's index in calibration.leads and cycle past the palette", () => {
    const charts = buildReliabilitySeries(fixture());
    expect(charts[0].series[0].colorClass).toBe(LEAD_COLORS[0]);
    expect(charts[0].series[1].colorClass).toBe(LEAD_COLORS[1]);

    const leads = [0, 1, 2, 3, 4, 5, 6];
    const many: CalibrationData = {
      leads,
      variables: ["TMAX"],
      rows: leads.map((lead) => ({
        variable: "TMAX",
        lead,
        cell: cell([{ lo: 0, hi: 1, predicted_mean: 0.5, observed_freq: 0.5, count: 1 }]),
      })),
    };
    const series = buildReliabilitySeries(many)[0].series;
    expect(series).toHaveLength(7);
    expect(series[LEAD_COLORS.length].colorClass).toBe(LEAD_COLORS[0]);
    expect(series[6].colorClass).toBe(LEAD_COLORS[6 % LEAD_COLORS.length]);
  });

  test("a missing (variable, lead) cell draws no series for that lead, others keep their color", () => {
    const data = fixture();
    data.rows = data.rows.filter((r) => !(r.variable === "TMAX" && r.lead === 1));

    const tmax = buildReliabilitySeries(data)[0];

    expect(tmax.series.map((s) => s.lead)).toEqual([3]);
    expect(tmax.series[0].colorClass).toBe(LEAD_COLORS[1]);
  });

  test("a row with empty buckets draws no series for that lead", () => {
    const data = fixture();
    const row = data.rows.find((r) => r.variable === "TMAX" && r.lead === 3);
    if (row) row.cell.reliabilityBins = [];

    const tmax = buildReliabilitySeries(data)[0];

    expect(tmax.series.map((s) => s.lead)).toEqual([1]);
  });

  test("a single lead builds one series per chart", () => {
    const data = fixture();
    data.leads = [1];
    data.rows = data.rows.filter((r) => r.lead === 1);

    const charts = buildReliabilitySeries(data);

    expect(charts.map((c) => c.series.map((s) => s.lead))).toEqual([[1], [1]]);
  });

  test("a single variable builds a single chart", () => {
    const data = fixture();
    data.variables = ["TMAX"];
    data.rows = data.rows.filter((r) => r.variable === "TMAX");

    const charts = buildReliabilitySeries(data);

    expect(charts).toHaveLength(1);
    expect(charts[0].variable).toBe("TMAX");
    expect(charts[0].series).toHaveLength(2);
  });

  test("no calibration rows yields no charts", () => {
    expect(buildReliabilitySeries({ leads: [], variables: [], rows: [] })).toEqual([]);
  });

  test("a variable whose cells are all empty still gets a chart with no series", () => {
    const data = fixture();
    for (const r of data.rows) if (r.variable === "TMIN") r.cell.reliabilityBins = [];

    const charts = buildReliabilitySeries(data);

    expect(charts[1].variable).toBe("TMIN");
    expect(charts[1].series).toEqual([]);
  });
});
