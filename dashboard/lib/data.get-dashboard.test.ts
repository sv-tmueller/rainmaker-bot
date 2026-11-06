import type { SupabaseClient } from "@supabase/supabase-js";
import { describe, expect, it } from "vitest";
import { getDashboardData } from "./data";

// End-to-end cover for the pass-through between the Supabase reads and
// mapBetRow: the price lookup by side, the null-venue default, and the ranking.
// mapBetRow's own cases call it directly, so a reintroduced `?? 0` at the
// askOf.get(...) call site in getDashboardData would be invisible there.

type Row = Record<string, unknown>;

// A hand-rolled stand-in for the Postgrest builder: every chain method returns
// the same builder and awaiting it resolves the canned rows for that table.
// Filters are accepted and ignored, so the canned data is shaped to be what the
// unfiltered reads would return (empty outcomes makes every settled query
// short-circuit before it runs).
function fakeDb(tables: Record<string, Row[]>): SupabaseClient {
  const builder = (table: string) => {
    const rows = tables[table] ?? [];
    const b = {
      select: () => b,
      order: () => b,
      limit: () => b,
      eq: () => b,
      not: () => b,
      in: () => b,
      then: (resolve: (value: { data: Row[]; error: null }) => unknown) =>
        resolve({ data: rows, error: null }),
    };
    return b;
  };
  return { from: builder } as unknown as SupabaseClient;
}

const dist = JSON.stringify({ mu: 80.5, sigma: 2, n_sources: 3 });

const tables: Record<string, Row[]> = {
  runs: [
    {
      id: "run-1",
      started_at: "2026-11-06T00:00:00Z",
      coverage: JSON.stringify({ ok_sources: ["nws"], n_markets: 3 }),
    },
  ],
  predictions: [
    {
      market_id: "m-poly",
      bucket: "80-81",
      side: "YES",
      p_win: 0.6,
      edge: 0.1,
      dist_params: dist,
    },
    {
      market_id: "m-null",
      bucket: "70-71",
      side: "YES",
      p_win: 0.55,
      edge: 0.13,
      dist_params: dist,
    },
    {
      market_id: "m-kalshi",
      bucket: "50-51",
      side: "NO",
      p_win: 0.5,
      edge: 0.125,
      dist_params: dist,
    },
  ],
  prices: [
    { market_id: "m-null", outcome: "70-71", side: "YES", price: 0.5 },
    { market_id: "m-kalshi", outcome: "50-51", side: "NO", price: 0.5 },
    // Same market and bucket, opposite side: the NO lookup must not land here.
    { market_id: "m-kalshi", outcome: "50-51", side: "YES", price: 0.9 },
  ],
  markets: [
    {
      id: "m-poly",
      title: "New York high",
      slug: "ny-high",
      settlement_date: "2026-11-07",
      city: "New York",
      venue: "polymarket",
      variable: "TMAX",
    },
    {
      id: "m-null",
      title: "Chicago high",
      slug: "chi-high",
      settlement_date: "2026-11-07",
      city: "Chicago",
      venue: null,
      variable: "TMAX",
    },
    {
      id: "m-kalshi",
      title: "Denver low",
      slug: "den-low",
      settlement_date: "2026-11-07",
      city: "Denver",
      venue: "kalshi",
      variable: "TMIN",
    },
  ],
  // m-poly has no price row, so its ask stays null and its edge stays gross.
  // Empty outcomes keeps the settled path out of this test.
  outcomes: [],
  tracking_snapshot: [],
  forecast_accuracy: [],
};

describe("getDashboardData", () => {
  it("assembles and ranks the recommended bets from the stored rows", async () => {
    const data = await getDashboardData(fakeDb(tables));

    // Kalshi 0.125 gross beats the netted Polymarket row: 0.13 at ask 0.5 is
    // 0.1175 after the taker fee. Raw-edge order would put Chicago first.
    expect(data.bets.map((b) => b.title)).toEqual([
      "Denver low",
      "Chicago high",
      "New York high",
    ]);

    const [kalshi, chicago, ny] = data.bets;
    expect(kalshi.venue).toBe("kalshi");
    expect(kalshi.side).toBe("NO");
    expect(kalshi.ask).toBe(0.5);
    expect(kalshi.edgeBasis).toBe("gross");
    expect(kalshi.shownEdge).toBe(0.125);

    // A null venue defaults to Polymarket, so the taker fee applies.
    expect(chicago.venue).toBe("polymarket");
    expect(chicago.ask).toBe(0.5);
    expect(chicago.edgeBasis).toBe("net");
    expect(chicago.shownEdge).toBe(0.13 - 0.05 * 0.5 * 0.5);

    // No stored price for this outcome: blank ask, gross edge.
    expect(ny.ask).toBeNull();
    expect(ny.edgeBasis).toBe("gross");
    expect(ny.shownEdge).toBe(ny.edge);
    expect(ny.shownEdge).toBe(0.1);
    expect(ny.mu).toBe(80.5);

    expect(data.run?.startedAt).toBe("2026-11-06T00:00:00Z");
    expect(data.run?.okSources).toEqual(["nws"]);
  });
});
