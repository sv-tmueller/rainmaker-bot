import type { RunInfo } from "../lib/data";

// The source names the pipeline writes (forecasts/nws.py, forecasts/openmeteo.py).
const EXPECTED_SOURCES = ["nws", "open-meteo"];

export function RunHealth({ run }: { run: RunInfo | null }) {
  if (!run) {
    return <span className="font-mono text-[11px] text-faint">no runs yet</span>;
  }
  const ok = run.okSources ?? [];
  const missing = run.okSources === null ? [] : EXPECTED_SOURCES.filter((s) => !ok.includes(s));
  return (
    <span className="font-mono text-[11px] text-muted">
      run {run.startedAt.slice(0, 16).replace("T", " ")} UTC
      {(ok.length > 0 || missing.length > 0) && <span className="mx-2 text-faint">·</span>}
      {ok.map((s) => (
        <span key={s} className="mr-2">
          {s} <span className="text-pos">✓</span>
        </span>
      ))}
      {missing.map((s) => (
        <span key={s} className="mr-2 text-warn">
          {s} missing
        </span>
      ))}
      {run.nMarkets !== null && (
        <>
          <span className="mx-2 text-faint">·</span>
          {run.nMarkets} markets
        </>
      )}
    </span>
  );
}
