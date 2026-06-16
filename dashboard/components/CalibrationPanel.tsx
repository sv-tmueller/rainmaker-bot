import type { CalibrationData, CalibrationRow, ReliabilityBin } from "../lib/data";
import { pct } from "../lib/format";

function coverageBar(frac: number, label: string) {
  return (
    <span title={`${label}: ${pct(frac)}`}>
      <span className={frac >= 0.85 ? "text-pos" : frac >= 0.7 ? "" : "text-warm"}>
        {pct(frac)}
      </span>
    </span>
  );
}

function ReliabilityMini({ bins }: { bins: ReliabilityBin[] }) {
  if (bins.length === 0) return <span className="text-faint">–</span>;
  // Compact inline display: each bin as "predicted%:observed%" for the largest bins
  const top = [...bins].sort((a, b) => b.count - a.count).slice(0, 3);
  return (
    <span className="text-faint text-[10px]">
      {top
        .sort((a, b) => a.lo - b.lo)
        .map((b) => `${pct(b.lo)}-${pct(b.hi)}: ${pct(b.observed_freq)}`)
        .join(" · ")}
    </span>
  );
}

function CalibrationCellDisplay({ row }: { row: CalibrationRow }) {
  const { cell } = row;
  return (
    <td className="py-1.5 text-right align-top">
      <div className="font-mono text-xs">
        <span className="text-faint">CRPS </span>
        {cell.crps.toFixed(3)}
      </div>
      <div className="font-mono text-xs">
        <span className="text-faint">cov </span>
        {coverageBar(cell.coverage50, "50%")} {coverageBar(cell.coverage80, "80%")}{" "}
        {coverageBar(cell.coverage90, "90%")}
      </div>
      <div className="mt-0.5">
        <ReliabilityMini bins={cell.reliabilityBins} />
      </div>
      <div className="text-[10px] text-faint">n{cell.n}</div>
    </td>
  );
}

export function CalibrationPanel({ calibration }: { calibration: CalibrationData }) {
  if (calibration.rows.length === 0) {
    return (
      <section className="rounded border border-line bg-panel px-4 py-4">
        <div className="text-[10px] uppercase tracking-[0.1em] text-muted">
          Forecast calibration
        </div>
        <p className="mt-3 text-sm text-muted">No calibration data yet.</p>
      </section>
    );
  }

  // Build a lookup: (variable, lead) -> CalibrationRow
  const byKey = new Map<string, CalibrationRow>(
    calibration.rows.map((r) => [`${r.variable}|${r.lead}`, r]),
  );

  return (
    <section className="rounded border border-line bg-panel px-4 py-4">
      <div className="text-[10px] uppercase tracking-[0.1em] text-muted">
        Forecast calibration{" "}
        <span className="normal-case tracking-normal text-faint">
          · CRPS, coverage, reliability by variable + lead
        </span>
      </div>
      <table className="mt-2.5 w-full border-collapse">
        <thead>
          <tr className="text-left font-sans text-[10px] uppercase tracking-[0.08em] text-faint">
            <th className="py-1 font-medium">Variable</th>
            {calibration.leads.map((l) => (
              <th key={l} className="text-right font-medium">
                {l}d
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {calibration.variables.map((variable) => (
            <tr key={variable} className="border-t border-line-soft">
              <td className="py-1.5 font-sans text-[13px] align-top">{variable}</td>
              {calibration.leads.map((lead) => {
                const row = byKey.get(`${variable}|${lead}`);
                if (!row) {
                  return (
                    <td key={lead} className="py-1.5 text-right text-faint">
                      –
                    </td>
                  );
                }
                return <CalibrationCellDisplay key={lead} row={row} />;
              })}
            </tr>
          ))}
        </tbody>
      </table>
      <p className="mt-3 text-[11px] leading-relaxed text-faint">
        Pooled across all cities. CRPS = Continuous Ranked Probability Score (lower is better). cov
        = central-interval coverage at 50/80/90%: fraction of actuals landing inside the central
        predictive band. Reliability shows observed frequency per predicted-probability bin (top 3
        by count). Columns = lead time in days.
      </p>
    </section>
  );
}
