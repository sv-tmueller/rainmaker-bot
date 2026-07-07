import { describe, expect, test } from "vitest";
import { coverageClass, selectReliabilityBins } from "./CalibrationPanel";
import type { ReliabilityBin } from "../lib/data";

describe("coverageClass", () => {
  test("exact match on 50% nominal is good", () => {
    expect(coverageClass(0.5, 0.5)).toBe("text-pos");
  });

  test("over-wide 50% column is bad, not good", () => {
    expect(coverageClass(0.9, 0.5)).toBe("text-warm");
  });

  test("close to 80% nominal is good", () => {
    expect(coverageClass(0.83, 0.8)).toBe("text-pos");
  });
});

describe("selectReliabilityBins", () => {
  test("always includes the top predicted bin even with a low count", () => {
    const bins: ReliabilityBin[] = [
      { lo: 0.0, hi: 0.1, predicted_mean: 0.05, observed_freq: 0.04, count: 100 },
      { lo: 0.2, hi: 0.3, predicted_mean: 0.25, observed_freq: 0.28, count: 90 },
      { lo: 0.5, hi: 0.6, predicted_mean: 0.55, observed_freq: 0.5, count: 80 },
      { lo: 0.9, hi: 1.0, predicted_mean: 0.95, observed_freq: 0.6, count: 3 },
    ];

    const selected = selectReliabilityBins(bins);

    expect(selected).toHaveLength(3);
    expect(selected.some((b) => b.lo === 0.9)).toBe(true);
    const rest = selected.filter((b) => b.lo !== 0.9);
    expect(rest.map((b) => b.lo).sort()).toEqual([0.0, 0.2]);
  });
});
