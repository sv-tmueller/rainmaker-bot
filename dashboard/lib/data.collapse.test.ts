import { describe, expect, it } from "vitest";
import { collapseSettled, type SettledCandidateRow } from "./data";

// Fixture mirrors tracking.py's two stacked collapses:
// 1. _latest_run_per_market_day: per (market, UTC day), keep only the latest run's rows.
// 2. _best_per_market_run: among the rows that survive, keep the highest-edge bet
//    per (market, run).
//
// Market A / 2026-07-01 has two runs on the same UTC day. run1 started earlier and
// carries the single highest-edge row overall (edge 0.30), but it must be dropped
// entirely because run2 started later that day. Within the surviving run2, two
// buckets compete on edge (0.10 vs 0.25); the higher-edge one must win the
// per-run tie-break even though it is the losing bet. A naive single collapse
// (best edge across all rows for the market, ignoring the day-level run
// collapse) would wrongly keep both run1's and run2's best bets as two separate
// rows; the correct two-step collapse keeps exactly one.
//
// Market B / 2026-07-02 is a single run on a different UTC day and must stay a
// separate bet from Market A.
const rows: SettledCandidateRow[] = [
  // Market A, run1 (2026-07-01T00:00:00Z): dropped entirely, run2 started later same day.
  {
    marketId: "market-a",
    runId: "run1",
    startedAt: "2026-07-01T00:00:00Z",
    bucket: "70-72",
    side: "YES",
    pWin: 0.8,
    edge: 0.3,
    won: true,
    ask: 0.6,
    title: "Market A",
    date: "2026-07-01",
    settledAt: "2026-07-02T12:00:00Z",
  },
  {
    marketId: "market-a",
    runId: "run1",
    startedAt: "2026-07-01T00:00:00Z",
    bucket: "72-74",
    side: "YES",
    pWin: 0.7,
    edge: 0.05,
    won: false,
    ask: 0.5,
    title: "Market A",
    date: "2026-07-01",
    settledAt: "2026-07-02T12:00:00Z",
  },
  // Market A, run2 (2026-07-01T06:00:00Z): the latest run that UTC day, so it wins.
  {
    marketId: "market-a",
    runId: "run2",
    startedAt: "2026-07-01T06:00:00Z",
    bucket: "74-76",
    side: "YES",
    pWin: 0.85,
    edge: 0.1,
    won: true,
    ask: 0.55,
    title: "Market A",
    date: "2026-07-01",
    settledAt: "2026-07-02T12:00:00Z",
  },
  {
    marketId: "market-a",
    runId: "run2",
    startedAt: "2026-07-01T06:00:00Z",
    bucket: "76-78",
    side: "YES",
    pWin: 0.65,
    edge: 0.25,
    won: false,
    ask: 0.45,
    title: "Market A",
    date: "2026-07-01",
    settledAt: "2026-07-02T12:00:00Z",
  },
  // Market B, run3, a different UTC day: stays a separate bet.
  {
    marketId: "market-b",
    runId: "run3",
    startedAt: "2026-07-02T00:00:00Z",
    bucket: "60-62",
    side: "NO",
    pWin: 0.75,
    edge: 0.15,
    won: false,
    ask: 0.4,
    title: "Market B",
    date: "2026-07-02",
    settledAt: "2026-07-03T12:00:00Z",
  },
];

describe("collapseSettled", () => {
  it("collapses to exactly one bet per (market, UTC day)", () => {
    const result = collapseSettled(rows);
    expect(result).toHaveLength(2);
  });

  it("keeps the latest run per day, then the highest-edge bucket within it", () => {
    const result = collapseSettled(rows);
    const marketA = result.find((r) => r.title === "Market A");
    expect(marketA).toEqual({
      settledAt: "2026-07-02T12:00:00Z",
      date: "2026-07-01",
      title: "Market A",
      side: "YES",
      pWin: 0.65,
      won: false,
      pnl: -0.45,
    });
  });

  it("keeps a different UTC day as a separate bet", () => {
    const result = collapseSettled(rows);
    const marketB = result.find((r) => r.title === "Market B");
    expect(marketB).toEqual({
      settledAt: "2026-07-03T12:00:00Z",
      date: "2026-07-02",
      title: "Market B",
      side: "NO",
      pWin: 0.75,
      won: false,
      pnl: -0.4,
    });
  });

  it("matches the win fraction tracking.py's rule would produce on these rows", () => {
    // tracking.py's rule on this fixture: 2 bets total, 0 wins (Market A's kept
    // bet lost via the higher-edge bucket, Market B's only bet lost).
    const result = collapseSettled(rows);
    const wins = result.filter((r) => r.won).length;
    expect(wins / result.length).toBe(0);
  });
});
