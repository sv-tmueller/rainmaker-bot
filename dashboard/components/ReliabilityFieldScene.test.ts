import { describe, expect, test } from "vitest";
import { barColor } from "./ReliabilityFieldScene";

// The 2D calibration panel's "cov" cells use coverageClass's three bands
// (good / middling / bad) mapped to three visually distinct colors. The 3D
// bars must keep that distinction: a middling ("") band must not collapse
// onto the same color as the bad ("text-warm") band.
const tokens = { pos: "#3fb950", warm: "#e8b05a", fg: "#e8eae6", fg2: "#bdb8af" };

describe("barColor", () => {
  test("good calibration (within 5%) uses the pos color", () => {
    expect(barColor(0.52, 0.5, tokens)).toBe(tokens.pos);
  });

  test("badly off calibration (over 10%) uses the warm color", () => {
    expect(barColor(0.9, 0.5, tokens)).toBe(tokens.warm);
  });

  test("slightly off calibration (5-10%) is distinct from the warm color", () => {
    const middling = barColor(0.58, 0.5, tokens);
    expect(middling).not.toBe(tokens.warm);
    expect(middling).toBe(tokens.fg2);
  });
});
