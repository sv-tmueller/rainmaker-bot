import type { AccSlot, Accuracy } from "../lib/data";
import { signed } from "../lib/format";

function Cell({ slot }: { slot: AccSlot | undefined }) {
  if (!slot || (!slot.live && !slot.backtest)) {
    return <td className="text-right text-faint">–</td>;
  }
  const { live, backtest } = slot;
  if (!live) {
    return (
      <td className="text-right text-faint">
        bt {backtest!.mae.toFixed(1)}° n{backtest!.n}
      </td>
    );
  }
  return (
    <td className="text-right">
      {live.mae.toFixed(1)}°{" "}
      <span className={live.bias >= 0 ? "text-warm" : "text-cool"}>{signed(live.bias, 1)}</span>{" "}
      <span className="text-faint">
        n{live.n}
        {backtest ? ` · bt ${backtest.mae.toFixed(1)}°` : ""}
      </span>
    </td>
  );
}

export function AccuracyGrid({ accuracy }: { accuracy: Accuracy }) {
  return (
    <section className="rounded border border-line bg-panel px-4 py-4">
      <div className="text-[10px] uppercase tracking-[0.1em] text-muted">
        Forecast accuracy{" "}
        <span className="normal-case tracking-normal text-faint">
          · live MAE, bias, n · bt = backtest
        </span>
      </div>
      {accuracy.rows.length === 0 ? (
        <p className="mt-3 text-sm text-muted">No accuracy data yet.</p>
      ) : (
        <table className="mt-2.5 w-full border-collapse font-mono text-xs">
          <thead>
            <tr className="text-left font-sans text-[10px] uppercase tracking-[0.08em] text-faint">
              <th className="py-1 font-medium">City</th>
              {accuracy.leads.map((l) => (
                <th key={l} className="text-right font-medium">
                  {l}d
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {accuracy.rows.map((row) => (
              <tr key={row.city} className="border-t border-line-soft">
                <td className="py-1.5 font-sans text-[13px]">{row.city}</td>
                {accuracy.leads.map((l) => (
                  <Cell key={l} slot={row.cells[l]} />
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
