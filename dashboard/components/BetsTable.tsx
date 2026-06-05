import type { Bet } from "../lib/data";
import { degC, pct, signed, withCelsius } from "../lib/format";

export function BetsTable({ bets }: { bets: Bet[] }) {
  return (
    <section className="rounded border border-line bg-panel px-4 py-4">
      <div className="text-[10px] uppercase tracking-[0.1em] text-muted">
        Recommended bets{" "}
        <span className="normal-case tracking-normal text-faint">· edge-sorted</span>
      </div>
      {bets.length === 0 ? (
        <p className="mt-3 text-sm text-muted">No bets pass the gates today.</p>
      ) : (
        <table className="mt-2.5 w-full border-collapse text-sm">
          <thead>
            <tr className="text-left text-[10px] uppercase tracking-[0.08em] text-faint">
              <th className="py-1 font-medium">Market</th>
              <th className="font-medium">Bucket</th>
              <th className="font-medium">Side</th>
              <th className="text-right font-medium">Forecast</th>
              <th className="text-right font-medium">P(win)</th>
              <th className="text-right font-medium">Ask</th>
              <th className="text-right font-medium">Edge</th>
              <th className="pl-7 text-right font-medium">σ</th>
              <th className="text-right font-medium">Src</th>
            </tr>
          </thead>
          <tbody className="font-mono text-xs">
            {bets.map((b, i) => (
              <tr key={i} className="border-t border-line-soft">
                <td className="py-1.5 font-sans text-[13px]">
                  {b.slug ? (
                    <a
                      href={`https://polymarket.com/event/${b.slug}`}
                      target="_blank"
                      rel="noreferrer"
                      className="hover:underline"
                    >
                      {b.title} <span className="text-[10px] text-faint">↗</span>
                    </a>
                  ) : (
                    b.title
                  )}
                </td>
                <td className="text-foreground/80">{withCelsius(b.bucket)}</td>
                <td className={b.side === "NO" ? "font-semibold text-warm" : "text-faint"}>
                  {b.side}
                </td>
                <td className="text-right">
                  {b.mu === null ? (
                    ""
                  ) : (
                    <>
                      {b.mu.toFixed(1)}° <span className="text-faint">{degC(b.mu)}C</span>
                    </>
                  )}
                </td>
                <td className="text-right">{pct(b.pWin)}</td>
                <td className="text-right">{b.ask.toFixed(2)}</td>
                <td className="text-right font-semibold text-pos">{signed(b.edge)}</td>
                <td className="pl-7 text-right text-faint">
                  {b.sigma === null ? "" : b.sigma.toFixed(1)}
                </td>
                <td className="text-right text-faint">{b.nSources ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
