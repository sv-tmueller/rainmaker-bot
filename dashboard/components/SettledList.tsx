import type { SettledBet } from "../lib/data";
import { pct, signed } from "../lib/format";

export function SettledList({ settled }: { settled: SettledBet[] }) {
  if (settled.length === 0) return null;
  return (
    <table className="w-full border-collapse font-mono text-xs">
      <tbody>
        {settled.map((s, i) => (
          <tr key={i} className="border-t border-line-soft">
            <td className="py-1 pr-2 text-faint">{s.date.slice(5)}</td>
            <td className="font-sans text-[13px]">{s.title}</td>
            <td className={`pr-2 text-[11px] ${s.side === "NO" ? "text-warm" : "text-faint"}`}>
              {s.side}
            </td>
            <td className="text-right text-muted">{pct(s.pWin)}</td>
            <td className={`text-right ${s.won ? "text-pos" : "text-neg"}`}>
              {s.won ? "W" : "L"}
            </td>
            <td className={`text-right ${s.won ? "text-pos" : "text-neg"}`}>{signed(s.pnl)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
