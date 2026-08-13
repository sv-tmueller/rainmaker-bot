import type { Snapshot } from "../lib/data";
import { signed } from "../lib/format";

type NamedSeries = { label: string; data: Snapshot[]; colorClass: string };

export function PnlChart({
  snapshots,
  venue,
}: {
  snapshots: Snapshot[];
  venue: { polymarket: Snapshot[]; kalshi: Snapshot[] };
}) {
  if (snapshots.length < 2) return null;
  const W = 600;
  const H = 150;
  const PAD = 8;
  const BOTTOM = 18;

  // Each venue series plots only the dates it has a row for, so pre-#345
  // history (no venue rows at all) draws no venue line.
  const venueSeries: NamedSeries[] = [
    { label: "Polymarket", data: venue.polymarket, colorClass: "text-cool" },
    { label: "Kalshi", data: venue.kalshi, colorClass: "text-warm" },
  ].filter((s) => s.data.length > 0);

  // x-axis is the union of dates across the total and venue series, so a
  // venue series that started later still lines up with the total's points.
  const dateSet = new Set<string>();
  for (const s of snapshots) dateSet.add(s.date);
  for (const series of venueSeries) for (const s of series.data) dateSet.add(s.date);
  const dates = [...dateSet].sort();
  const xIndexOf = new Map(dates.map((d, i) => [d, i]));

  const allValues = [
    ...snapshots.map((s) => s.totalPnl),
    ...venueSeries.flatMap((s) => s.data.map((d) => d.totalPnl)),
  ];
  const hi = Math.max(0, ...allValues);
  const lo = Math.min(0, ...allValues);
  const span = hi - lo || 1;
  const x = (i: number) => PAD + (i * (W - 2 * PAD)) / Math.max(1, dates.length - 1);
  const y = (v: number) => PAD + ((hi - v) * (H - PAD - BOTTOM)) / span;
  const pointsOf = (rows: Snapshot[]) =>
    rows
      .map((s) => `${x(xIndexOf.get(s.date)!).toFixed(1)},${y(s.totalPnl).toFixed(1)}`)
      .join(" ");

  const totalPoints = pointsOf(snapshots);
  const last = snapshots[snapshots.length - 1];
  const toneClass = last.totalPnl >= 0 ? "text-pos" : "text-neg";
  const fillClass = last.totalPnl >= 0 ? "fill-pos" : "fill-neg";

  // Legend x advances cumulatively by the previous label's rendered width, not
  // a fixed slot: "polymarket" in 9px monospace runs wider than a fixed
  // 34-unit slot and would overlap the next label otherwise.
  const CHAR_WIDTH = 5.5;
  const LEGEND_GAP = 6;
  const legendLabels: { label: string; colorClass: string }[] = [
    { label: "total", colorClass: toneClass },
    ...venueSeries.map((s) => ({ label: s.label.toLowerCase(), colorClass: s.colorClass })),
  ];
  let cursorX = PAD;
  const legendItems = legendLabels.map(({ label, colorClass }) => {
    const item = { label, colorClass, x: cursorX };
    cursorX += label.length * CHAR_WIDTH + LEGEND_GAP;
    return item;
  });

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="block w-full" role="img" aria-label="P&L over time">
      <line
        x1={PAD}
        y1={y(0)}
        x2={W - PAD}
        y2={y(0)}
        className="text-line"
        stroke="currentColor"
        strokeDasharray="3 4"
      />
      {venueSeries.map((series) => (
        <polyline
          key={series.label}
          points={pointsOf(series.data)}
          fill="none"
          className={series.colorClass}
          stroke="currentColor"
          strokeWidth="1"
          opacity={0.65}
        />
      ))}
      <polyline
        points={totalPoints}
        fill="none"
        className={toneClass}
        stroke="currentColor"
        strokeWidth="1.5"
      />
      <circle
        cx={x(dates.length - 1)}
        cy={y(last.totalPnl)}
        r="2.5"
        className={toneClass}
        fill="currentColor"
      />
      <text
        x={W - PAD}
        y={Math.max(10, y(last.totalPnl) - 7)}
        textAnchor="end"
        className={`font-mono text-[11px] ${fillClass}`}
      >
        {signed(last.totalPnl, 1)}u
      </text>
      {venueSeries.length > 0 && (
        <g className="font-mono text-[9px]">
          {legendItems.map((item) => (
            <text
              key={item.label}
              x={item.x}
              y={12}
              className={`fill-current ${item.colorClass}`}
            >
              {item.label}
            </text>
          ))}
        </g>
      )}
      <text x={PAD} y={H - 4} className="fill-faint font-mono text-[10px]">
        {dates[0]}
      </text>
      <text x={W - PAD} y={H - 4} textAnchor="end" className="fill-faint font-mono text-[10px]">
        {dates[dates.length - 1]}
      </text>
    </svg>
  );
}
