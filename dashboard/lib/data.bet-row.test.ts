import { describe, expect, it } from "vitest";
import { mapBetRow, type BetEnrichment, type RawBetPredictionRow } from "./data";
import { POLYMARKET_TAKER_FEE_RATE, takerFeePerShare } from "./fees";

// mapBetRow is the one place a prediction row becomes a Bet, so these cases
// pin the visible-edge contract that used to live inline in getDashboardData:
// the ask nullness and the venue decide whether the row is net or gross.

const pred: RawBetPredictionRow = {
  market_id: "market-1",
  bucket: "80-81",
  side: "YES",
  p_win: 0.6,
  edge: 0.13,
  dist_params: JSON.stringify({ mu: 80.5, sigma: 2, n_sources: 3 }),
};

function enrich(overrides: Partial<BetEnrichment> = {}): BetEnrichment {
  return {
    title: "New York high",
    city: "New York",
    venue: "polymarket",
    variable: "TMAX",
    slug: "ny-high",
    ...overrides,
  };
}

describe("mapBetRow: shown edge by venue and ask", () => {
  it("labels a Polymarket row with a null ask gross, with a blank ask", () => {
    const bet = mapBetRow(pred, enrich(), null);
    expect(bet.ask).toBeNull();
    expect(bet.edgeBasis).toBe("gross");
    expect(bet.shownEdge).toBe(bet.edge);
    expect(bet.shownEdge).toBe(0.13);
  });

  it("treats the raw Map.get miss (undefined) the same as a null ask", () => {
    // The caller passes askOf.get(...) straight through, so a missing price
    // arrives as undefined, not null; both must stay gross with a blank ask.
    const bet = mapBetRow(pred, enrich(), undefined);
    expect(bet.ask).toBeNull();
    expect(bet.edgeBasis).toBe("gross");
    expect(bet.shownEdge).toBe(bet.edge);
  });

  it("treats a null venue as Polymarket and nets the taker fee", () => {
    const ask = 0.5;
    const bet = mapBetRow(pred, enrich({ venue: null }), ask);
    expect(bet.venue).toBe("polymarket");
    expect(bet.edgeBasis).toBe("net");
    expect(bet.shownEdge).toBe(bet.edge - takerFeePerShare(ask, POLYMARKET_TAKER_FEE_RATE));
    expect(bet.shownEdge).toBe(0.13 - 0.05 * 0.5 * 0.5);
  });

  it("keeps a Kalshi row gross even with a known ask", () => {
    const bet = mapBetRow(pred, enrich({ venue: "kalshi" }), 0.5);
    expect(bet.edgeBasis).toBe("gross");
    expect(bet.shownEdge).toBe(bet.edge);
  });

  it("blanks mu/sigma/nSources when dist_params is unparsable", () => {
    const bet = mapBetRow({ ...pred, dist_params: "not json" }, enrich(), 0.5);
    expect(bet.mu).toBeNull();
    expect(bet.sigma).toBeNull();
    expect(bet.nSources).toBeNull();
  });
});
