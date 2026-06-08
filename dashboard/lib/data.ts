import { serverClient } from "./supabase";

export type Bet = {
  title: string;
  city: string;
  venue: string;
  variable: string;
  slug: string | null;
  bucket: string;
  side: "YES" | "NO";
  mu: number | null;
  sigma: number | null;
  nSources: number | null;
  pWin: number;
  ask: number;
  edge: number;
};

export type RunInfo = {
  startedAt: string;
  okSources: string[] | null;
  nMarkets: number | null;
};

export type Snapshot = {
  date: string;
  nBets: number;
  wins: number;
  losses: number;
  totalPnl: number;
  roi: number;
  brier: number | null;
  hitRate: number | null;
  nScored: number;
};

export type AccCell = { n: number; mae: number; bias: number };
export type AccSlot = { live: AccCell | null; backtest: AccCell | null };
export type AccRow = { city: string; cells: Record<number, AccSlot> };
export type Accuracy = { leads: number[]; rows: AccRow[] };

export type SettledBet = {
  date: string;
  title: string;
  side: "YES" | "NO";
  pWin: number;
  won: boolean;
  pnl: number;
};

// Python round() is half-to-even, Math.round is half-up. NOAA actuals can land
// exactly on .5°F (7.5°C is 45.5°F), so mirror the settlement math exactly.
function roundHalfEven(x: number): number {
  const f = Math.floor(x);
  if (x - f === 0.5) return f % 2 === 0 ? f : f + 1;
  return Math.round(x);
}

// Mirrors _won in src/rainmaker/tracking.py (parse_bucket_label + comparison).
// Returns null for an unparsable label so the row is skipped, not miscounted.
function wonBucket(label: string, actual: number): boolean | null {
  const lowered = label.toLowerCase();
  const v = roundHalfEven(actual);
  const t = label.match(/-?\d+/);
  if (lowered.includes("below")) return t ? v <= +t[0] : null;
  if (lowered.includes("higher") || lowered.includes("above")) return t ? v >= +t[0] : null;
  const m = label.match(/(-?\d+)\s*-\s*(-?\d+)/);
  if (!m || +m[1] > +m[2]) return null;
  return +m[1] <= v && v <= +m[2];
}

export async function getDashboardData() {
  const db = serverClient();

  // Wave 1: bounded independent reads.
  // outcomes: newest-first, capped at 30. The settled list shows 10 bets; 30
  // recently-settled markets is plenty and keeps reads bounded as history grows.
  const [runsQ, snapsQ, accQ, outcomesQ] = await Promise.all([
    db.from("runs").select("id, started_at, coverage").order("started_at", { ascending: false }).limit(1),
    db.from("tracking_snapshot").select("*").order("snapshot_date", { ascending: true }),
    db.from("forecast_accuracy").select("city, lead_time, kind, n, mae_f, bias_f").order("city").order("lead_time"),
    db.from("outcomes").select("market_id, actual_value, settled_at").order("settled_at", { ascending: false }).limit(30),
  ]);

  const runRow = runsQ.data?.[0];
  // settledIds from the bounded outcomes result, already sorted desc by settled_at.
  const settledIds = (outcomesQ.data ?? []).map((o) => o.market_id);

  // Wave 2: predictions and the latest run's prices, scoped by runRow and
  // settledIds. Settled-market prices move to wave 3 so they can be bounded by
  // the run ids that settledPreds actually references (#64).
  const [latestPreds, latestPrices, settledPreds] = await Promise.all([
    runRow
      ? db
          .from("predictions")
          .select("market_id, bucket, side, p_win, edge, dist_params")
          .eq("run_id", runRow.id)
          .eq("recommended", 1)
      : Promise.resolve({ data: null }),
    runRow
      ? db.from("prices").select("market_id, outcome, side, price").eq("run_id", runRow.id)
      : Promise.resolve({ data: null }),
    settledIds.length > 0
      ? db
          .from("predictions")
          .select("market_id, run_id, bucket, side, p_win")
          .eq("recommended", 1)
          .not("bucket", "is", null)
          .in("market_id", settledIds)
      : Promise.resolve({ data: null }),
  ]);

  // Wave 3: markets and settled-market prices, both bounded to only what this
  // page needs. settledPrices is scoped to the run ids present in settledPreds
  // (not every run that ever priced these markets), so the read cannot silently
  // truncate at Supabase's 1000-row cap and drop a settled bet (#64).
  const latestPredIds = (latestPreds.data ?? []).map((p) => p.market_id);
  const neededIds = [...new Set([...latestPredIds, ...settledIds])];
  const settledRunIds = [...new Set((settledPreds.data ?? []).map((p) => p.run_id))];
  const [marketsQ, settledPrices] = await Promise.all([
    neededIds.length > 0
      ? db
          .from("markets")
          .select("id, title, slug, settlement_date, city, venue, variable")
          .in("id", neededIds)
      : Promise.resolve({ data: [] }),
    settledRunIds.length > 0
      ? db
          .from("prices")
          .select("run_id, market_id, outcome, side, price")
          .in("market_id", settledIds)
          .in("run_id", settledRunIds)
      : Promise.resolve({ data: null }),
  ]);

  const titleOf = new Map((marketsQ.data ?? []).map((m) => [m.id, m.title as string]));
  const cityOf = new Map((marketsQ.data ?? []).map((m) => [m.id, (m.city as string | null) ?? ""]));
  const venueOf = new Map(
    (marketsQ.data ?? []).map((m) => [m.id, (m.venue as string | null) ?? "polymarket"]),
  );
  const variableOf = new Map(
    (marketsQ.data ?? []).map((m) => [m.id, (m.variable as string | null) ?? ""]),
  );
  const slugOf = new Map((marketsQ.data ?? []).map((m) => [m.id, (m.slug as string | null) ?? null]));
  const settleDateOf = new Map(
    (marketsQ.data ?? []).map((m) => [m.id, (m.settlement_date as string | null) ?? null]),
  );

  // Assemble run health.
  let run: RunInfo | null = null;
  if (runRow) {
    let okSources: string[] | null = null;
    let nMarkets: number | null = null;
    try {
      const cov = JSON.parse(runRow.coverage as string);
      if (Array.isArray(cov?.ok_sources)) okSources = cov.ok_sources;
      if (typeof cov?.n_markets === "number") nMarkets = cov.n_markets;
    } catch {
      // unparsable coverage -> timestamp-only health line
    }
    run = { startedAt: runRow.started_at as string, okSources, nMarkets };
  }

  // Assemble bets for the latest run. Price is keyed by side; legacy rows are YES.
  const sideOf = (s: unknown): "YES" | "NO" => (s === "NO" ? "NO" : "YES");
  const askOf = new Map(
    (latestPrices.data ?? []).map((p) => [
      `${p.market_id}|${p.outcome}|${sideOf(p.side)}`,
      p.price as number,
    ]),
  );
  const bets: Bet[] = (latestPreds.data ?? [])
    .map((p) => {
      let mu: number | null = null;
      let sigma: number | null = null;
      let nSources: number | null = null;
      try {
        const d = JSON.parse(p.dist_params as string);
        if (typeof d?.mu === "number") mu = d.mu;
        if (typeof d?.sigma === "number") sigma = d.sigma;
        if (typeof d?.n_sources === "number") nSources = d.n_sources;
      } catch {
        // no parsable dist_params -> blank forecast cells
      }
      const side = sideOf(p.side);
      return {
        title: titleOf.get(p.market_id) ?? (p.market_id as string),
        city: cityOf.get(p.market_id) ?? "",
        venue: venueOf.get(p.market_id) ?? "polymarket",
        variable: variableOf.get(p.market_id) ?? "",
        slug: slugOf.get(p.market_id) ?? null,
        bucket: p.bucket as string,
        side,
        mu,
        sigma,
        nSources,
        pWin: p.p_win as number,
        ask: askOf.get(`${p.market_id}|${p.bucket}|${side}`) ?? 0,
        edge: p.edge as number,
      };
    })
    .sort((a, b) => b.edge - a.edge);

  // Assemble snapshots.
  const snapshots: Snapshot[] = (snapsQ.data ?? []).map((s) => ({
    date: s.snapshot_date as string,
    nBets: s.n_bets as number,
    wins: s.wins as number,
    losses: s.losses as number,
    totalPnl: s.total_pnl as number,
    roi: s.roi as number,
    brier: s.brier as number | null,
    hitRate: s.hit_rate as number | null,
    nScored: s.n_scored as number,
  }));

  // Assemble accuracy pivot.
  // TMAX-only today; add variable to the key when TMIN accuracy lands.
  const accMap = new Map<string, AccRow>();
  const leadSet = new Set<number>();
  for (const r of accQ.data ?? []) {
    const city = r.city as string;
    const lead = r.lead_time as number;
    if (lead < 0) continue; // a run after settlement is a catch-up, not a forecast
    leadSet.add(lead);
    const row = accMap.get(city) ?? { city, cells: {} };
    const cell = { n: r.n as number, mae: r.mae_f as number, bias: r.bias_f as number };
    const slot = row.cells[lead] ?? { live: null, backtest: null };
    if (r.kind === "backtest") slot.backtest = cell;
    else slot.live = cell;
    row.cells[lead] = slot;
    accMap.set(city, row);
  }
  const accuracy: Accuracy = {
    leads: [...leadSet].sort((a, b) => a - b),
    rows: [...accMap.values()].sort((a, b) => a.city.localeCompare(b.city)),
  };

  // Assemble settled bets.
  // Mirrors tracking.compute_pnl: each recommended prediction with a price on a
  // settled market is one one-unit bet; re-recommendations are separate bets.
  let settled: SettledBet[] = [];
  const outcomes = outcomesQ.data ?? [];
  if (outcomes.length > 0) {
    const priceOf = new Map(
      (settledPrices.data ?? []).map((p) => [
        `${p.run_id}|${p.market_id}|${p.outcome}|${sideOf(p.side)}`,
        p.price as number,
      ]),
    );
    const outcomeOf = new Map(outcomes.map((o) => [o.market_id, o]));
    settled = (settledPreds.data ?? [])
      .flatMap((p) => {
        const o = outcomeOf.get(p.market_id);
        const side = sideOf(p.side);
        const ask = priceOf.get(`${p.run_id}|${p.market_id}|${p.bucket}|${side}`);
        if (!o || ask === undefined) return [];
        const settledIn = wonBucket(p.bucket as string, o.actual_value as number);
        if (settledIn === null) return [];
        // A NO bet wins when the bucket does not settle.
        const won = side === "NO" ? !settledIn : settledIn;
        return [
          {
            settledAt: o.settled_at as string,
            date: (settleDateOf.get(p.market_id) ?? (o.settled_at as string)).slice(0, 10),
            title: titleOf.get(p.market_id) ?? (p.market_id as string),
            side,
            pWin: p.p_win as number,
            won,
            pnl: won ? 1 - ask : -ask,
          },
        ];
      })
      // Stable sort: newest settled first; within one date, alphabetical by title.
      .sort((a, b) => {
        if (a.settledAt !== b.settledAt) return a.settledAt < b.settledAt ? 1 : -1;
        return a.title.localeCompare(b.title);
      })
      .slice(0, 10)
      .map(({ settledAt: _settledAt, ...rest }) => rest);
  }

  return { run, bets, snapshots, accuracy, settled };
}
