import type { Snapshot } from "../lib/data";
import { signed } from "../lib/format";

export function PnlChart({ snapshots }: { snapshots: Snapshot[] }) {
  if (snapshots.length < 2) return null;
  const W = 600;
  const H = 150;
  const PAD = 8;
  const BOTTOM = 18;
  const pnls = snapshots.map((s) => s.totalPnl);
  const hi = Math.max(0, ...pnls);
  const lo = Math.min(0, ...pnls);
  const span = hi - lo || 1;
  const x = (i: number) => PAD + (i * (W - 2 * PAD)) / (snapshots.length - 1);
  const y = (v: number) => PAD + ((hi - v) * (H - PAD - BOTTOM)) / span;
  const points = snapshots.map((s, i) => `${x(i).toFixed(1)},${y(s.totalPnl).toFixed(1)}`).join(" ");
  const last = snapshots[snapshots.length - 1];
  const toneClass = last.totalPnl >= 0 ? "text-pos" : "text-neg";
  const fillClass = last.totalPnl >= 0 ? "fill-pos" : "fill-neg";
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
      <polyline
        points={points}
        fill="none"
        className={toneClass}
        stroke="currentColor"
        strokeWidth="1.5"
      />
      <circle
        cx={x(snapshots.length - 1)}
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
      <text x={PAD} y={H - 4} className="fill-faint font-mono text-[10px]">
        {snapshots[0].date}
      </text>
      <text x={W - PAD} y={H - 4} textAnchor="end" className="fill-faint font-mono text-[10px]">
        {last.date}
      </text>
    </svg>
  );
}
