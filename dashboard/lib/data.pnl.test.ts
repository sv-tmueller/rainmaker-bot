import { describe, expect, it } from "vitest";
import { collapseSettled, type SettledCandidateRow } from "./data";
import fixtureRows from "./data.pnl.fixture.json";

// Complements data.collapse.test.ts (which pins the two-step collapse's
// ordering internals: latest-run-per-day, then highest-edge-per-run). This
// file pins the settled P&L business rule itself
// (`pnl: r.won ? 1 - r.ask : -r.ask` in data.ts) and the win-fraction
// aggregation that tracking.py's `compute_pnl` mirrors, neither of which
// data.collapse.test.ts's all-losses fixture exercises.
//
// Fixture (dyadic asks so expectations are exact doubles):
// - Market W: two intraday runs on 2026-06-01 (same UTC day). run-w1 started
//   first (00:00Z, ask 0.375) and must be dropped; run-w2 started later that
//   day (06:00Z, ask 0.25, won: true) and is the one that survives. Hand
//   computation: pnl = 1 - 0.25 = 0.75.
// - Market L: one run on 2026-06-05, ask 0.5, won: false.
//   Hand computation: pnl = -0.5.
// Collapsed output: 2 bets, 1 win (Market W) out of 2 -> win fraction 1/2.
const rows = fixtureRows as SettledCandidateRow[];

describe("collapseSettled: empty input", () => {
  it("returns [] for [] (no settled markets yet)", () => {
    expect(collapseSettled([])).toEqual([]);
  });
});

describe("collapseSettled: settled P&L rule", () => {
  it("collapses market W's two intraday runs to a single winning bet", () => {
    const result = collapseSettled(rows);
    expect(result).toHaveLength(2);
    const marketW = result.find((r) => r.title === "Market W");
    expect(marketW).toBeDefined();
    // Only run-w2's row (ask 0.25) should survive; run-w1's row (ask 0.375)
    // is dropped because it belongs to the day's earlier, non-latest run.
    expect(marketW?.won).toBe(true);
  });

  it("computes a won bet's P&L as 1 - ask", () => {
    const result = collapseSettled(rows);
    const marketW = result.find((r) => r.title === "Market W");
    expect(marketW?.pnl).toBe(1 - 0.25);
    expect(marketW?.pnl).toBe(0.75);
  });

  it("computes a lost bet's P&L as -ask", () => {
    const result = collapseSettled(rows);
    const marketL = result.find((r) => r.title === "Market L");
    expect(marketL?.pnl).toBe(-0.5);
  });

  it("produces a nonzero win fraction over the collapsed output", () => {
    const result = collapseSettled(rows);
    const wins = result.filter((r) => r.won).length;
    expect(wins / result.length).toBe(0.5);
  });
});
