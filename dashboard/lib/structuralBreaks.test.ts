import { describe, expect, it } from "vitest";
import { STRUCTURAL_BREAKS, visibleBreaks } from "./structuralBreaks";

describe("visibleBreaks", () => {
  it("returns nothing for fewer than two dates", () => {
    expect(visibleBreaks(["2026-09-04"])).toEqual([]);
    expect(visibleBreaks([])).toEqual([]);
  });

  it("includes a break strictly inside the range", () => {
    const out = visibleBreaks(["2026-08-01", "2026-09-04", "2026-09-10"]);
    expect(out).toHaveLength(1);
    expect(out[0]).toBe(STRUCTURAL_BREAKS[0]);
  });

  it("drops a break on the first date (edge, no visible discontinuity)", () => {
    expect(visibleBreaks(["2026-09-04", "2026-09-05"])).toEqual([]);
  });

  it("drops a break on the last date (edge)", () => {
    expect(visibleBreaks(["2026-09-01", "2026-09-04"])).toEqual([]);
  });

  it("drops a break outside the range entirely", () => {
    expect(visibleBreaks(["2026-09-10", "2026-09-20"])).toEqual([]);
  });
});
