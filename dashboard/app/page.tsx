import { serverClient } from "../lib/supabase";

export const dynamic = "force-dynamic"; // always read live data, never prerender

type Bet = { title: string; bucket: string; pWin: number; ask: number; edge: number };

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
        .select("market_id, bucket, p_win, edge")
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
      .map((p) => ({
        title: titleOf.get(p.market_id) ?? p.market_id,
        bucket: p.bucket as string,
        pWin: p.p_win as number,
        ask: askOf.get(`${p.market_id}|${p.bucket}`) ?? 0,
        edge: p.edge as number,
      }))
      .sort((a, b) => b.edge - a.edge);
  }

  const { data: snaps } = await db
    .from("tracking_snapshot")
    .select("*")
    .order("snapshot_date", { ascending: false })
    .limit(1);
  const snap = snaps?.[0] ?? null;

  return { bets, snap };
}

function pct(x: number) {
  return `${(x * 100).toFixed(0)}%`;
}

export default async function Page() {
  const { bets, snap } = await getData();
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
              <th>P(win)</th>
              <th>Ask</th>
              <th>Edge</th>
            </tr>
          </thead>
          <tbody>
            {bets.map((b, i) => (
              <tr key={i} className="border-t">
                <td>{b.title}</td>
                <td>{b.bucket}</td>
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
      <p className="mt-6 text-xs text-gray-400">
        {snap ? `snapshot ${snap.snapshot_date}` : "no snapshot yet"}
      </p>
    </main>
  );
}
