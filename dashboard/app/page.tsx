import { serverClient } from "../lib/supabase";
import { degC, pct, withCelsius } from "../lib/format";

export const dynamic = "force-dynamic"; // always read live data, never prerender

type Bet = { title: string; bucket: string; pWin: number; ask: number; edge: number; forecastF: number | null };

async function getData() {
  const db = serverClient();

  const { data: runs } = await db
    .from("runs")
    .select("id")
    .order("started_at", { ascending: false })
    .limit(1);
  const runId = runs?.[0]?.id as string | undefined;

  let bets: Bet[] = [];
  if (runId) {
    const [{ data: preds }, { data: prices }, { data: markets }] = await Promise.all([
      db
        .from("predictions")
        .select("market_id, bucket, p_win, edge, dist_params")
        .eq("run_id", runId)
        .eq("recommended", 1),
      db.from("prices").select("market_id, outcome, price").eq("run_id", runId),
      db.from("markets").select("id, title"),
    ]);
    const askOf = new Map(
      (prices ?? []).map((p) => [`${p.market_id}|${p.outcome}`, p.price as number]),
    );
    const titleOf = new Map((markets ?? []).map((m) => [m.id, m.title as string]));
    bets = (preds ?? [])
      .map((p) => {
        let forecastF: number | null = null;
        try {
          const mu = JSON.parse(p.dist_params as string)?.mu;
          if (typeof mu === "number") forecastF = mu;
        } catch {
          // no parsable mu -> blank forecast cell
        }
        return {
          title: titleOf.get(p.market_id) ?? p.market_id,
          bucket: p.bucket as string,
          pWin: p.p_win as number,
          ask: askOf.get(`${p.market_id}|${p.bucket}`) ?? 0,
          edge: p.edge as number,
          forecastF,
        };
      })
      .sort((a, b) => b.edge - a.edge);
  }

  const { data: snaps } = await db
    .from("tracking_snapshot")
    .select("*")
    .order("snapshot_date", { ascending: false })
    .limit(1);
  const snap = snaps?.[0] ?? null;

  const { data: accRows } = await db
    .from("forecast_accuracy")
    .select("city, lead_time, kind, n, mae_f, bias_f")
    .order("city")
    .order("lead_time");

  type AccCell = { n: number; mae: number; bias: number } | null;
  type AccLine = { city: string; lead: number; backtest: AccCell; live: AccCell };
  const accMap = new Map<string, AccLine>();
  for (const r of accRows ?? []) {
    // TMAX-only today; include r.variable in this key when TMIN accuracy lands.
    const key = `${r.city}|${r.lead_time}`;
    const line = accMap.get(key) ?? {
      city: r.city as string,
      lead: r.lead_time as number,
      backtest: null,
      live: null,
    };
    const cell = { n: r.n as number, mae: r.mae_f as number, bias: r.bias_f as number };
    if (r.kind === "backtest") line.backtest = cell;
    else line.live = cell;
    accMap.set(key, line);
  }
  const accuracy = [...accMap.values()];

  return { bets, snap, accuracy };
}

function degDelta(f: number) {
  return `${f.toFixed(1)}°F (${((f * 5) / 9).toFixed(1)}°C)`;
}

function accCell(c: { n: number; mae: number; bias: number } | null) {
  if (!c) return "–";
  const sign = c.bias >= 0 ? "+" : "";
  return `${degDelta(c.mae)}, bias ${sign}${degDelta(c.bias)}, n=${c.n}`;
}

export default async function Page() {
  const { bets, snap, accuracy } = await getData();
  return (
    <main className="mx-auto max-w-3xl p-6 font-sans">
      <h1 className="text-2xl font-bold">Rainmaker</h1>

      <h2 className="mt-6 text-lg font-semibold">Recommended bets</h2>
      {bets.length === 0 ? (
        <p className="text-gray-500">No bets pass the gates right now.</p>
      ) : (
        <table className="mt-2 w-full text-sm">
          <thead>
            <tr className="text-left text-gray-500">
              <th>Market</th>
              <th>Bucket</th>
              <th>Forecast</th>
              <th>P(win)</th>
              <th>Ask</th>
              <th>Edge</th>
            </tr>
          </thead>
          <tbody>
            {bets.map((b, i) => (
              <tr key={i} className="border-t">
                <td>{b.title}</td>
                <td>{withCelsius(b.bucket)}</td>
                <td>{b.forecastF === null ? "" : `${b.forecastF.toFixed(1)}°F / ${degC(b.forecastF)}°C`}</td>
                <td>{pct(b.pWin)}</td>
                <td>{b.ask.toFixed(2)}</td>
                <td>
                  {b.edge >= 0 ? "+" : ""}
                  {b.edge.toFixed(2)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h2 className="mt-6 text-lg font-semibold">Performance</h2>
      {!snap || snap.n_bets === 0 ? (
        <p className="text-gray-500">No settled results yet.</p>
      ) : (
        <ul className="mt-2 text-sm">
          <li>
            P&amp;L: {snap.total_pnl >= 0 ? "+" : ""}
            {snap.total_pnl.toFixed(2)}u over {snap.n_bets} bets ({snap.wins}-{snap.losses}), ROI{" "}
            {snap.roi >= 0 ? "+" : ""}
            {pct(snap.roi)}
          </li>
          <li>
            Calibration: Brier {snap.brier === null ? "n/a" : snap.brier.toFixed(3)}, hit rate{" "}
            {snap.hit_rate === null ? "n/a" : pct(snap.hit_rate)} (n={snap.n_scored})
          </li>
        </ul>
      )}
      <h2 className="mt-6 text-lg font-semibold">Forecast accuracy</h2>
      {accuracy.length === 0 ? (
        <p className="text-gray-500">No accuracy data yet.</p>
      ) : (
        <table className="mt-2 w-full text-sm">
          <thead>
            <tr className="text-left text-gray-500">
              <th>City</th>
              <th>Lead</th>
              <th>Backtest MAE</th>
              <th>Live MAE</th>
            </tr>
          </thead>
          <tbody>
            {accuracy.map((a, i) => (
              <tr key={i} className="border-t">
                <td>{a.city}</td>
                <td>{a.lead}d</td>
                <td>{accCell(a.backtest)}</td>
                <td>{accCell(a.live)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="mt-6 text-xs text-gray-400">
        {snap ? `snapshot ${snap.snapshot_date}` : "no snapshot yet"}
      </p>
    </main>
  );
}
