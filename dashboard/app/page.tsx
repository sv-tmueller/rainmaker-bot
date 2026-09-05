import { AccuracyGrid } from "../components/AccuracyGrid";
import { BetsTable } from "../components/BetsTable";
import { CalibrationPanel } from "../components/CalibrationPanel";
import { KpiStrip } from "../components/KpiStrip";
import { PnlChart } from "../components/PnlChart";
import { RunHealth } from "../components/RunHealth";
import { SettledList } from "../components/SettledList";
import { getDashboardData } from "../lib/data";
import { visibleBreaks } from "../lib/structuralBreaks";

export const dynamic = "force-dynamic"; // always read live data, never prerender

export default async function Page() {
  const { run, bets, snapshots, venueSnapshots, accuracy, calibration, settled } =
    await getDashboardData();
  const snap = snapshots.length > 0 ? snapshots[snapshots.length - 1] : null;
  const snapshotDates = snapshots.map((s) => s.date).sort();
  const breaksShown = visibleBreaks(snapshotDates);
  return (
    <main className="mx-auto w-full max-w-[1200px] px-9 py-7">
      <header className="flex items-baseline justify-between border-b border-line pb-3.5">
        <h1 className="text-[15px] font-semibold tracking-tight">Rainmaker</h1>
        <RunHealth run={run} />
      </header>

      <KpiStrip snap={snap} venue={venueSnapshots} />

      <BetsTable bets={bets} />

      <div className="mt-3.5 grid grid-cols-5 gap-3.5">
        <div className="col-span-3 flex flex-col gap-3.5">
          <AccuracyGrid accuracy={accuracy} />
          <CalibrationPanel calibration={calibration} />
        </div>
        <section className="col-span-2 rounded border border-line bg-panel px-4 py-4">
          <div className="text-[10px] uppercase tracking-[0.1em] text-muted">Track record</div>
          {snapshots.length === 0 ? (
            <p className="mt-3 text-sm text-muted">No settled results yet.</p>
          ) : (
            <>
              <div className="mt-2.5">
                <PnlChart snapshots={snapshots} venue={venueSnapshots} />
              </div>
              {breaksShown.length > 0 && (
                <ul className="mt-2 space-y-1">
                  {breaksShown.map((br) => (
                    <li
                      key={br.date}
                      className="font-mono text-[10px] leading-snug text-warn"
                    >
                      <span className="text-faint">{br.label}:</span> {br.detail}
                    </li>
                  ))}
                </ul>
              )}
              {settled.length > 0 && (
                <>
                  <div className="mt-3 text-[10px] uppercase tracking-[0.1em] text-muted">
                    Recent settled
                  </div>
                  <div className="mt-1.5">
                    <SettledList settled={settled} />
                  </div>
                </>
              )}
            </>
          )}
        </section>
      </div>
    </main>
  );
}
