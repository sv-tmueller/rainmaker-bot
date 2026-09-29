import { describe, expect, test } from "vitest";
import { buildReliabilityField } from "./reliabilityField";
import type { CalibrationData } from "./data";

// Known-answer fixtures. Bin identity is (lo, hi); the bin axis is the union
// of bin ranges seen across all leads for the selected variable, sorted by lo.
function cube(): CalibrationData {
  return {
    leads: [1, 3],
    variables: ["TMAX", "TMIN"],
    rows: [
      {
        variable: "TMAX",
        lead: 1,
        cell: {
          n: 190,
          crps: 1.2,
          coverage50: 0.5,
          coverage80: 0.8,
          coverage90: 0.9,
          reliabilityBins: [
            { lo: 0.0, hi: 0.5, predicted_mean: 0.3, observed_freq: 0.28, count: 100 },
            { lo: 0.5, hi: 1.0, predicted_mean: 0.7, observed_freq: 0.75, count: 90 },
          ],
        },
      },
      {
        variable: "TMAX",
        lead: 3,
        cell: {
          n: 150,
          crps: 1.5,
          coverage50: 0.48,
          coverage80: 0.77,
          coverage90: 0.88,
          reliabilityBins: [
            { lo: 0.0, hi: 0.5, predicted_mean: 0.32, observed_freq: 0.4, count: 80 },
            { lo: 0.5, hi: 1.0, predicted_mean: 0.68, observed_freq: 0.6, count: 70 },
          ],
        },
      },
    ],
  };
}

describe("buildReliabilityField", () => {
  test("normal cube: every (lead, bin) pair present produces one bar each", () => {
    const field = buildReliabilityField(cube(), "TMAX");

    expect(field.variable).toBe("TMAX");
    expect(field.leads).toEqual([1, 3]);
    expect(field.bins).toEqual([
      { lo: 0.0, hi: 0.5 },
      { lo: 0.5, hi: 1.0 },
    ]);
    expect(field.bars).toHaveLength(4);

    const bar = field.bars.find((b) => b.lead === 1 && b.lo === 0.0);
    expect(bar).toBeDefined();
    expect(bar?.leadIndex).toBe(0);
    expect(bar?.binIndex).toBe(0);
    expect(bar?.predictedMean).toBeCloseTo(0.3);
    expect(bar?.observedFreq).toBeCloseTo(0.28);
    expect(bar?.count).toBe(100);
    // Highest count in the field (100) gets full weight.
    expect(bar?.weight).toBeCloseTo(1);

    const smaller = field.bars.find((b) => b.lead === 3 && b.lo === 0.5);
    expect(smaller?.count).toBe(70);
    expect(smaller?.weight).toBeCloseTo(70 / 100);
  });

  test("a missing (variable, lead) row renders as a gap, not a crash", () => {
    const data = cube();
    // Drop the lead-3 row entirely for TMAX.
    data.rows = data.rows.filter((r) => !(r.variable === "TMAX" && r.lead === 3));

    const field = buildReliabilityField(data, "TMAX");

    expect(field.leads).toEqual([1, 3]);
    expect(field.bars.every((b) => b.lead !== 3)).toBe(true);
    expect(field.bars).toHaveLength(2);
  });

  test("an empty reliabilityBins list on a row contributes no bars for that lead", () => {
    const data = cube();
    const row = data.rows.find((r) => r.variable === "TMAX" && r.lead === 3);
    if (row) row.cell.reliabilityBins = [];

    const field = buildReliabilityField(data, "TMAX");

    // Bin axis is still driven by the other lead's bins.
    expect(field.bins).toHaveLength(2);
    expect(field.bars.every((b) => b.lead !== 3)).toBe(true);
    expect(field.bars).toHaveLength(2);
  });

  test("a single-lead calibration set still builds a field", () => {
    const data: CalibrationData = {
      leads: [1],
      variables: ["TMAX"],
      rows: [
        {
          variable: "TMAX",
          lead: 1,
          cell: {
            n: 10,
            crps: 1,
            coverage50: 0.5,
            coverage80: 0.8,
            coverage90: 0.9,
            reliabilityBins: [
              { lo: 0.0, hi: 1.0, predicted_mean: 0.5, observed_freq: 0.5, count: 10 },
            ],
          },
        },
      ],
    };

    const field = buildReliabilityField(data, "TMAX");

    expect(field.leads).toEqual([1]);
    expect(field.bins).toEqual([{ lo: 0.0, hi: 1.0 }]);
    expect(field.bars).toHaveLength(1);
    expect(field.bars[0].weight).toBeCloseTo(1);
  });

  test("selecting a variable with no rows at all yields an empty, non-crashing field", () => {
    const field = buildReliabilityField(cube(), "PRECIP");

    expect(field.variable).toBe("PRECIP");
    expect(field.bins).toEqual([]);
    expect(field.bars).toEqual([]);
  });

  test("a calibration set with a single variable behaves the same as the multi-variable case", () => {
    const data = cube();
    data.variables = ["TMAX"];
    data.rows = data.rows.filter((r) => r.variable === "TMAX");

    const field = buildReliabilityField(data, "TMAX");

    expect(field.bars).toHaveLength(4);
  });
});
