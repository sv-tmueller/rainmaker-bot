// Polymarket taker fee rate. Keep equal to POLYMARKET_TAKER_FEE_RATE in
// src/rainmaker/config.py; fees.test.ts fails if the two drift apart.
export const POLYMARKET_TAKER_FEE_RATE = 0.05;

// Taker fee per share bought at `ask`, same units as edge = p_win - ask. Mirrors
// taker_fee_per_share in the Python ranking code.
export function takerFeePerShare(ask: number, rate: number): number {
  return rate * ask * (1 - ask);
}

export type ShownEdge = { value: number; basis: "net" | "gross" };

// The edge the dashboard displays and ranks on. A Polymarket row with a known
// ask is net of the taker fee, like the advisory report. Kalshi rows, and any
// row whose ask is missing, stay gross so the tag never claims "net" for a
// value the fee was not taken from.
export function shownEdge(edge: number, ask: number | null, venue: string): ShownEdge {
  if (venue === "polymarket" && ask !== null) {
    return { value: edge - takerFeePerShare(ask, POLYMARKET_TAKER_FEE_RATE), basis: "net" };
  }
  return { value: edge, basis: "gross" };
}
