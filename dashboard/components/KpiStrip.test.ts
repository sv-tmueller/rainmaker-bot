import { describe, expect, it } from "vitest";
import { activeVenueSnapshot } from "./KpiStrip";
import type { Snapshot } from "../lib/data";

// activeVenueSnapshot backs each venue card's render gate: the sibling
// package #344 writes zero-bet venue rows unconditionally (roi 0.0), so
// "last row exists" is not enough to show a card; it must also have bets.

function snap(overrides: Partial<Snapshot>): Snapshot {
  return {
    date: "2026-08-01",
    nBets: 1,
    wins: 1,
    losses: 0,
    totalPnl: 0.5,
    roi: 0.5,
    brier: 0.1,
    hitRate: 1,
    nScored: 1,
    ...overrides,
  };
}

describe("activeVenueSnapshot", () => {
  it("returns null for an empty venue series", () => {
    expect(activeVenueSnapshot([])).toBeNull();
  });

  it("returns null when the last row has zero bets", () => {
    const rows = [snap({ date: "2026-08-01", nBets: 3 }), snap({ date: "2026-08-02", nBets: 0 })];
    expect(activeVenueSnapshot(rows)).toBeNull();
  });

  it("returns the last row when it has bets", () => {
    const rows = [snap({ date: "2026-08-01", nBets: 3 }), snap({ date: "2026-08-02", nBets: 2 })];
    expect(activeVenueSnapshot(rows)).toEqual(rows[1]);
  });
});
