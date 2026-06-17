import { AccuracyGrid } from "../components/AccuracyGrid";
import { BetsTable } from "../components/BetsTable";
import { CalibrationPanel } from "../components/CalibrationPanel";
import { KpiStrip } from "../components/KpiStrip";
import { PnlChart } from "../components/PnlChart";
import { RunHealth } from "../components/RunHealth";
import { SettledList } from "../components/SettledList";
import { getDashboardData } from "../lib/data";

export const dynamic = "force-dynamic"; // always read live data, never prerender

export default async function Page() {
  const { run, bets, snapshots, accuracy, calibration, settled } = await getDashboardData();
  const snap = snapshots.length > 0 ? snapshots[snapshots.length - 1] : null;
  return (
    <main className="mx-auto w-full max-w-[1200px] px-9 py-7">
      <header className="flex items-baseline justify-between border-b border-line pb-3.5">
        <h1 className="text-[15px] font-semibold tracking-tight">Rainmaker</h1>
        <RunHealth run={run} />
      </header>

      <KpiStrip snap={snap} />

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
                <PnlChart snapshots={snapshots} />
              </div>
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
