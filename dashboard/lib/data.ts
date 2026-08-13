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

// A tracking_snapshot row as read back by select("*"). venue is optional
// because a DB predating the sibling package's migration has no such column
// at all, not merely a null value.
export type RawSnapshotRow = {
  snapshot_date: string;
  n_bets: number;
  wins: number;
  losses: number;
  total_pnl: number;
  roi: number;
  brier: number | null;
  hit_rate: number | null;
  n_scored: number;
  venue?: string | null;
};

export type VenueSnapshots = {
  total: Snapshot[];
  polymarket: Snapshot[];
  kalshi: Snapshot[];
};

function toSnapshot(r: RawSnapshotRow): Snapshot {
  return {
    date: r.snapshot_date,
    nBets: r.n_bets,
    wins: r.wins,
    losses: r.losses,
    totalPnl: r.total_pnl,
    roi: r.roi,
    brier: r.brier,
    hitRate: r.hit_rate,
    nScored: r.n_scored,
  };
}

// The snapshot query orders descending with a row cap so history growing past
// Supabase's default row cap truncates the oldest rows, never the newest
// (#345's row-cap fix). This restores ascending order for the chart/KPI
// consumers without touching the query's cap semantics.
export function orderSnapshotRows(rows: RawSnapshotRow[]): RawSnapshotRow[] {
  return [...rows].reverse();
}

// Aggregate = venue 'all', null, or absent (a DB predating the sibling's
// migration, since the read is select("*")); this one filter is the whole
// graceful-degradation contract. Venue arrays only collect
// venue === 'polymarket' | 'kalshi' rows, so aggregate-only data yields empty
// venue arrays and every downstream consumer renders as it does today.
export function splitSnapshotsByVenue(rows: RawSnapshotRow[]): VenueSnapshots {
  const total: Snapshot[] = [];
  const polymarket: Snapshot[] = [];
  const kalshi: Snapshot[] = [];
  for (const r of rows) {
    const venue = r.venue ?? "all";
    if (venue === "polymarket") polymarket.push(toSnapshot(r));
    else if (venue === "kalshi") kalshi.push(toSnapshot(r));
    else total.push(toSnapshot(r));
  }
  return { total, polymarket, kalshi };
}

export type AccCell = { n: number; mae: number; bias: number };
export type AccSlot = { live: AccCell | null; backtest: AccCell | null };
export type AccRow = { city: string; cells: Record<number, AccSlot> };
export type Accuracy = { leads: number[]; rows: AccRow[] };

export type ReliabilityBin = {
  lo: number;
  hi: number;
  predicted_mean: number;
  observed_freq: number;
  count: number;
};

export type CalibrationCell = {
  n: number;
  crps: number;
  coverage50: number;
  coverage80: number;
  coverage90: number;
  reliabilityBins: ReliabilityBin[];
};

export type CalibrationRow = {
  variable: string;
  lead: number;
  cell: CalibrationCell;
};

export type CalibrationData = {
  leads: number[];
  variables: string[];
  rows: CalibrationRow[];
};

export type SettledBet = {
  date: string;
  title: string;
  side: "YES" | "NO";
  pWin: number;
  won: boolean;
  pnl: number;
};

// One priced, graded, recommended prediction row: the candidate population
// collapseSettled collapses to one bet per (market, UTC day).
export type SettledCandidateRow = {
  marketId: string;
  runId: string;
  startedAt: string;
  bucket: string;
  side: "YES" | "NO";
  pWin: number;
  edge: number | null;
  won: boolean;
  ask: number;
  title: string;
  date: string;
  settledAt: string;
};

type SettledCollapsedRow = {
  settledAt: string;
  date: string;
  title: string;
  side: "YES" | "NO";
  pWin: number;
  won: boolean;
  pnl: number;
};

// Mirrors tracking.py's two stacked collapses (_latest_run_per_market_day,
// _best_per_market_run) so the dashboard's win rate agrees with `rainmaker
// track`. Step 1 collapses the intraday reruns (#77) that price a market
// several times a day to the day's latest run; step 2 collapses the buckets
// of one market/run (correlated: they describe the same temperature) to the
// single best-edge bet. Order matters: a market's highest-edge row overall
// can belong to a run that is not that day's latest, and must be dropped
// before the edge tie-break runs.
export function collapseSettled(rows: SettledCandidateRow[]): SettledCollapsedRow[] {
  // Step 1: keep only the latest run's rows per (market, UTC day). Marker
  // (startedAt, runId) breaks an exact-timestamp tie deterministically,
  // matching tracking.py's _latest_run_per_market_day.
  const latest = new Map<string, { marketId: string; runId: string; startedAt: string }>();
  for (const r of rows) {
    const key = `${r.marketId}|${r.startedAt.slice(0, 10)}`;
    const cur = latest.get(key);
    if (!cur || r.startedAt > cur.startedAt || (r.startedAt === cur.startedAt && r.runId > cur.runId)) {
      latest.set(key, { marketId: r.marketId, runId: r.runId, startedAt: r.startedAt });
    }
  }
  const keep = new Set([...latest.values()].map((v) => `${v.marketId}|${v.runId}`));
  const latestRows = rows.filter((r) => keep.has(`${r.marketId}|${r.runId}`));

  // Step 2: keep the highest-edge row per (market, run). Tie-break on
  // (edge, pWin, bucket, side) to match tracking.py's _edge_key exactly.
  const best = new Map<string, SettledCandidateRow>();
  for (const r of latestRows) {
    const key = `${r.marketId}|${r.runId}`;
    const cur = best.get(key);
    if (!cur || edgeKeyGreater(r, cur)) {
      best.set(key, r);
    }
  }

  return [...best.values()].map((r) => ({
    settledAt: r.settledAt,
    date: r.date,
    title: r.title,
    side: r.side,
    pWin: r.pWin,
    won: r.won,
    pnl: r.won ? 1 - r.ask : -r.ask,
  }));
}

// (edge ?? -Infinity, pWin, bucket, side) compared left to right, matching
// tracking.py's _edge_key. Numeric compare for edge/pWin, lexicographic for
// bucket/side ("YES" > "NO", so YES wins an exact tie).
function edgeKeyGreater(a: SettledCandidateRow, b: SettledCandidateRow): boolean {
  const aEdge = a.edge ?? -Infinity;
  const bEdge = b.edge ?? -Infinity;
  if (aEdge !== bEdge) return aEdge > bEdge;
  if (a.pWin !== b.pWin) return a.pWin > b.pWin;
  if (a.bucket !== b.bucket) return a.bucket > b.bucket;
  return a.side > b.side;
}

export async function getDashboardData() {
  const db = serverClient();

  // Wave 1: bounded independent reads.
  // outcomes: newest-first, capped at 30. The settled list shows 10 bets; 30
  // recently-settled markets is plenty and keeps reads bounded as history grows.
  const [runsQ, snapsQ, accQ, outcomesQ] = await Promise.all([
    db.from("runs").select("id, started_at, coverage").order("started_at", { ascending: false }).limit(1),
    // Descending + capped, then reversed client-side (#345): an unbounded
    // ascending read gets silently truncated by Supabase's row cap once
    // history exceeds it, dropping the newest rows and freezing the KPI
    // strip. Descending + .limit(999) guarantees the newest rows survive.
    db
      .from("tracking_snapshot")
      .select("*")
      .order("snapshot_date", { ascending: false })
      .limit(999),
    db.from("forecast_accuracy").select("city, variable, lead_time, kind, n, mae_f, bias_f, crps, coverage_50, coverage_80, coverage_90, reliability").order("city").order("lead_time"),
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
          .select("market_id, run_id, bucket, side, p_win, edge, won")
          .eq("recommended", 1)
          .not("bucket", "is", null)
          .not("won", "is", null)
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
  const [marketsQ, settledPrices, settledRunsQ] = await Promise.all([
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
    settledRunIds.length > 0
      ? db.from("runs").select("id, started_at").in("id", settledRunIds)
      : Promise.resolve({ data: null }),
  ]);
  const startedAtOf = new Map(
    (settledRunsQ.data ?? []).map((r) => [r.id, r.started_at as string]),
  );

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

  // Assemble snapshots, split by venue. The query above reads newest-first
  // (capped); orderSnapshotRows restores ascending order before grouping.
  const orderedSnapRows = orderSnapshotRows((snapsQ.data ?? []) as RawSnapshotRow[]);
  const { total: snapshots, polymarket: polymarketSnapshots, kalshi: kalshiSnapshots } =
    splitSnapshotsByVenue(orderedSnapRows);

  // Assemble accuracy pivot (MAE/bias rows only; kind='calibration' rows feed CalibrationPanel).
  const accMap = new Map<string, AccRow>();
  const leadSet = new Set<number>();
  const calRows: CalibrationRow[] = [];
  const calLeadSet = new Set<number>();
  const calVarSet = new Set<string>();
  for (const r of accQ.data ?? []) {
    const lead = r.lead_time as number;
    if (lead < 0) continue; // a run after settlement is a catch-up, not a forecast
    const kind = r.kind as string;

    if (kind === "calibration") {
      // Calibration rows: station='ALL', variable+lead are the keys.
      const variable = (r.variable as string | null) ?? "";
      if (!variable) continue;
      let bins: ReliabilityBin[] = [];
      try {
        const parsed = JSON.parse((r.reliability as string | null) ?? "[]");
        if (Array.isArray(parsed)) bins = parsed as ReliabilityBin[];
      } catch {
        // unparsable reliability JSON: leave bins empty
      }
      calLeadSet.add(lead);
      calVarSet.add(variable);
      calRows.push({
        variable,
        lead,
        cell: {
          n: r.n as number,
          crps: (r.crps as number | null) ?? 0,
          coverage50: (r.coverage_50 as number | null) ?? 0,
          coverage80: (r.coverage_80 as number | null) ?? 0,
          coverage90: (r.coverage_90 as number | null) ?? 0,
          reliabilityBins: bins,
        },
      });
      continue;
    }

    // kind 'live' or 'backtest': feed the per-city MAE/bias AccuracyGrid.
    if (kind !== "live" && kind !== "backtest") continue;
    const city = (r.city as string | null) ?? "";
    if (!city) continue;
    leadSet.add(lead);
    const row = accMap.get(city) ?? { city, cells: {} };
    const cell = { n: r.n as number, mae: r.mae_f as number, bias: r.bias_f as number };
    const slot = row.cells[lead] ?? { live: null, backtest: null };
    if (kind === "backtest") slot.backtest = cell;
    else slot.live = cell;
    row.cells[lead] = slot;
    accMap.set(city, row);
  }
  const accuracy: Accuracy = {
    leads: [...leadSet].sort((a, b) => a - b),
    rows: [...accMap.values()].sort((a, b) => a.city.localeCompare(b.city)),
  };
  const calibration: CalibrationData = {
    leads: [...calLeadSet].sort((a, b) => a - b),
    variables: [...calVarSet].sort(),
    rows: calRows,
  };

  // Assemble settled bets.
  // Mirrors tracking.py's population and its two stacked collapses
  // (_latest_run_per_market_day, _best_per_market_run) via collapseSettled, so
  // the win rate shown here agrees with `rainmaker track`.
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
    const candidates: SettledCandidateRow[] = (settledPreds.data ?? []).flatMap((p) => {
      const o = outcomeOf.get(p.market_id);
      const side = sideOf(p.side);
      const ask = priceOf.get(`${p.run_id}|${p.market_id}|${p.bucket}|${side}`);
      const startedAt = startedAtOf.get(p.run_id);
      // won is pre-graded by settle.py using the canonical Python grading; skip
      // rows where it is NULL (not yet graded, or non-recommended).
      if (!o || ask === undefined || p.won === null || startedAt === undefined) return [];
      return [
        {
          marketId: p.market_id as string,
          runId: p.run_id as string,
          startedAt,
          bucket: p.bucket as string,
          side,
          pWin: p.p_win as number,
          edge: (p.edge as number | null) ?? null,
          won: p.won === 1,
          ask,
          title: titleOf.get(p.market_id) ?? (p.market_id as string),
          date: (settleDateOf.get(p.market_id) ?? (o.settled_at as string)).slice(0, 10),
          settledAt: o.settled_at as string,
        },
      ];
    });
    settled = collapseSettled(candidates)
      // Stable sort: newest settled first; within one date, alphabetical by title.
      .sort((a, b) => {
        if (a.settledAt !== b.settledAt) return a.settledAt < b.settledAt ? 1 : -1;
        return a.title.localeCompare(b.title);
      })
      .slice(0, 10)
      .map(({ settledAt: _settledAt, ...rest }) => rest);
  }

  return {
    run,
    bets,
    snapshots,
    venueSnapshots: { polymarket: polymarketSnapshots, kalshi: kalshiSnapshots },
    accuracy,
    calibration,
    settled,
  };
}
