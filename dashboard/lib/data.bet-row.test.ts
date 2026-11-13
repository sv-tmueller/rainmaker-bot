import { describe, expect, it } from "vitest";
import { mapBetRow, parseMarketRaw, type BetEnrichment, type RawBetPredictionRow } from "./data";
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
    station: "KNYC",
    yesTokens: { "80-81": "tok-80-81", "82-83": "tok-82-83" },
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

describe("mapBetRow: live-price fields", () => {
  it("carries the station and the YES token for the row's bucket", () => {
    const bet = mapBetRow(pred, enrich(), 0.5);
    expect(bet.station).toBe("KNYC");
    expect(bet.yesTokenId).toBe("tok-80-81");
  });

  it("uses the YES token for a NO row too", () => {
    const bet = mapBetRow({ ...pred, side: "NO" }, enrich(), 0.5);
    expect(bet.yesTokenId).toBe("tok-80-81");
  });

  it("has a null token when the bucket is not in the stored market", () => {
    const bet = mapBetRow({ ...pred, bucket: "99-100" }, enrich(), 0.5);
    expect(bet.yesTokenId).toBeNull();
  });

  it("has a null token and station for a Kalshi row", () => {
    const bet = mapBetRow(pred, enrich({ venue: "kalshi" }), 0.5);
    expect(bet.yesTokenId).toBeNull();
    expect(bet.station).toBeNull();
  });
});

describe("parseMarketRaw", () => {
  it("reads the station ICAO and each bucket's YES token", () => {
    const raw = JSON.stringify({
      target: { station: { icao: "KSFO", city: "San Francisco" } },
      buckets: [
        { label: "60-61", yes_token_id: "a", no_token_id: "x" },
        { label: "62-63", yes_token_id: "b" },
      ],
    });
    expect(parseMarketRaw(raw)).toEqual({ station: "KSFO", yesTokens: { "60-61": "a", "62-63": "b" } });
  });

  it("reads tokens for a monthly precip market, which has no ICAO", () => {
    const raw = JSON.stringify({
      target: { station: { resolution_name: "NYC climate" } },
      buckets: [{ label: "2-3 in", yes_token_id: "p1" }],
    });
    expect(parseMarketRaw(raw)).toEqual({ station: null, yesTokens: { "2-3 in": "p1" } });
  });

  it("skips buckets with a missing or non-string label or token", () => {
    const raw = JSON.stringify({
      buckets: [{ label: "a" }, { yes_token_id: "b" }, { label: "c", yes_token_id: 5 }, { label: "d", yes_token_id: "" }],
    });
    expect(parseMarketRaw(raw)).toEqual({ station: null, yesTokens: {} });
  });

  it("returns empty for unparsable, null or non-object raw", () => {
    const empty = { station: null, yesTokens: {} };
    expect(parseMarketRaw("not json")).toEqual(empty);
    expect(parseMarketRaw(null)).toEqual(empty);
    expect(parseMarketRaw("42")).toEqual(empty);
    expect(parseMarketRaw(JSON.stringify({ buckets: "nope" }))).toEqual(empty);
  });
});
