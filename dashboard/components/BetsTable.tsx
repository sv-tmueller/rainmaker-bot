import { Fragment } from "react";

import type { Bet } from "../lib/data";
import { degC, pct, signed, withCelsius } from "../lib/format";

function groupByCity(bets: Bet[]): { city: string; bets: Bet[] }[] {
  // bets arrive edge-sorted; preserving first-seen order keeps cities ordered by
  // their best edge, and each city's bets stay edge-sorted.
  const order: string[] = [];
  const groups = new Map<string, Bet[]>();
  for (const b of bets) {
    const city = b.city || "Other";
    if (!groups.has(city)) {
      groups.set(city, []);
      order.push(city);
    }
    groups.get(city)!.push(b);
  }
  return order.map((city) => ({ city, bets: groups.get(city)! }));
}

function marketLabel(b: Bet): string {
  // Drop the city from the title; it is redundant under the city header.
  return b.city ? b.title.replace(` in ${b.city}`, "") : b.title;
}

export function BetsTable({ bets }: { bets: Bet[] }) {
  const groups = groupByCity(bets);
  return (
    <section className="rounded border border-line bg-panel px-4 py-4">
      <div className="text-[10px] uppercase tracking-[0.1em] text-muted">
        Recommended bets{" "}
        <span className="normal-case tracking-normal text-faint">
          · grouped by city, best edge first
        </span>
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
            {groups.map((g) => (
              <Fragment key={g.city}>
                <tr>
                  <td
                    colSpan={9}
                    className="border-t border-line pt-3 pb-1 font-sans text-[11px] uppercase tracking-[0.08em] text-muted"
                  >
                    {g.city}
                  </td>
                </tr>
                {g.bets.map((b, i) => (
                  <tr key={`${g.city}-${i}`} className="border-t border-line-soft">
                    <td className="py-1.5 font-sans text-[13px]">
                      {b.venue === "polymarket" && b.slug ? (
                        <a
                          href={`https://polymarket.com/event/${b.slug}`}
                          target="_blank"
                          rel="noreferrer"
                          className="hover:underline"
                        >
                          {marketLabel(b)} <span className="text-[10px] text-faint">↗</span>
                        </a>
                      ) : (
                        marketLabel(b)
                      )}
                      <span className="ml-1.5 text-[9px] uppercase tracking-wide text-faint">
                        {b.venue}
                      </span>
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
              </Fragment>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
