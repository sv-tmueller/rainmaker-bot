import type { Snapshot } from "../lib/data";
import { pct, signed } from "../lib/format";

function Kpi({
  label,
  value,
  tone,
  sub,
}: {
  label: string;
  value: string;
  tone?: "pos" | "neg";
  sub?: string;
}) {
  const toneClass = tone === "pos" ? "text-pos" : tone === "neg" ? "text-neg" : "";
  return (
    <div>
      <div className="text-[10px] uppercase tracking-[0.1em] text-muted">{label}</div>
      <div className={`font-mono text-[22px] font-semibold ${toneClass}`}>
        {value}
        {sub && <span className="ml-1 text-xs font-normal text-faint">{sub}</span>}
      </div>
    </div>
  );
}

// Only the last row per venue matters here (it already carries the
// cumulative record/ROI); an empty array means no venue rows exist yet
// (aggregate-only data), and a zero-bet last row (the sibling package writes
// one unconditionally) means the venue has no settled bets yet. Either case
// omits the card rather than showing a blank or falsely-toned "0-0".
export function activeVenueSnapshot(rows: Snapshot[]): Snapshot | null {
  if (rows.length === 0) return null;
  const last = rows[rows.length - 1];
  return last.nBets > 0 ? last : null;
}

export function KpiStrip({
  snap,
  venue,
}: {
  snap: Snapshot | null;
  venue: { polymarket: Snapshot[]; kalshi: Snapshot[] };
}) {
  const has = snap !== null && snap.nBets > 0;
  const tone = (x: number): "pos" | "neg" => (x >= 0 ? "pos" : "neg");
  const polySnap = activeVenueSnapshot(venue.polymarket);
  const kalshiSnap = activeVenueSnapshot(venue.kalshi);
  return (
    <div className="flex items-baseline gap-12 py-5">
      <Kpi
        label="P&L"
        value={has ? `${signed(snap.totalPnl)}u` : "–"}
        tone={has ? tone(snap.totalPnl) : undefined}
      />
      <Kpi
        label="ROI"
        value={has ? `${signed(snap.roi * 100, 1)}%` : "–"}
        tone={has ? tone(snap.roi) : undefined}
      />
      <Kpi label="Record" value={has ? `${snap.wins}-${snap.losses}` : "–"} />
      <Kpi label="Brier" value={has && snap.brier !== null ? snap.brier.toFixed(3) : "–"} />
      <Kpi
        label="Hit rate"
        value={has && snap.hitRate !== null ? pct(snap.hitRate) : "–"}
        sub={has ? `n${snap.nScored}` : undefined}
      />
      {polySnap && (
        <Kpi
          label="Polymarket"
          value={`${polySnap.wins}-${polySnap.losses}`}
          tone={tone(polySnap.roi)}
          sub={`${signed(polySnap.roi * 100, 1)}%`}
        />
      )}
      {kalshiSnap && (
        <Kpi
          label="Kalshi"
          value={`${kalshiSnap.wins}-${kalshiSnap.losses}`}
          tone={tone(kalshiSnap.roi)}
          sub={`${signed(kalshiSnap.roi * 100, 1)}%`}
        />
      )}
      {snap && (
        <div className="ml-auto font-mono text-[11px] text-faint">snapshot {snap.date}</div>
      )}
    </div>
  );
}
