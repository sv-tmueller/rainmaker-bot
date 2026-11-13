import { describe, expect, it } from "vitest";
import { rankBets, type Bet } from "./data";
import { shownEdge } from "./fees";

function bet(venue: string, ask: number | null, edge: number, title: string): Bet {
  const s = shownEdge(edge, ask, venue);
  return {
    title,
    city: "New York",
    venue,
    variable: "TMAX",
    slug: null,
    bucket: "80-81",
    side: "YES",
    mu: null,
    sigma: null,
    nSources: null,
    pWin: 0.6,
    ask,
    edge,
    shownEdge: s.value,
    edgeBasis: s.basis,
    yesTokenId: null,
    station: null,
  };
}

describe("rankBets", () => {
  it("ranks on the displayed edge, not the gross edge", () => {
    // Polymarket: gross 0.130 at ask 0.50 is net 0.1175. Kalshi: gross 0.125.
    // Gross order puts Polymarket first; displayed order puts Kalshi first.
    const poly = bet("polymarket", 0.5, 0.13, "poly");
    const kalshi = bet("kalshi", 0.5, 0.125, "kalshi");
    expect(poly.edge).toBeGreaterThan(kalshi.edge);
    expect(rankBets([poly, kalshi]).map((b) => b.title)).toEqual(["kalshi", "poly"]);
    expect(rankBets([kalshi, poly]).map((b) => b.title)).toEqual(["kalshi", "poly"]);
  });

  it("does not mutate its input", () => {
    const a = bet("polymarket", 0.5, 0.13, "a");
    const b = bet("kalshi", 0.5, 0.125, "b");
    const input = [a, b];
    rankBets(input);
    expect(input.map((x) => x.title)).toEqual(["a", "b"]);
  });
});
