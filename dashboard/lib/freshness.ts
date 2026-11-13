// The dashboard's own stale threshold. It is not tied to the Python trader's
// 60 minute run-age cap or the 24 hour forecast-sample limit.
export const STALE_RUN_HOURS = 4;

export type Freshness = { ageLabel: string; stale: boolean };

const MINUTE_MS = 60_000;
const HOUR_MS = 3_600_000;

// null in means no run, so null out. An unparseable timestamp fails closed
// (stale) so an old forecast is never shown as fresh.
export function runFreshness(startedAt: string | null, nowMs: number): Freshness | null {
  if (startedAt === null) return null;
  const startedMs = Date.parse(startedAt);
  if (Number.isNaN(startedMs)) return { ageLabel: "age unknown", stale: true };
  const ageMs = Math.max(0, nowMs - startedMs);
  const ageLabel =
    ageMs < HOUR_MS
      ? `${Math.floor(ageMs / MINUTE_MS)}m ago`
      : `${Math.floor(ageMs / HOUR_MS)}h ago`;
  return { ageLabel, stale: ageMs > STALE_RUN_HOURS * HOUR_MS };
}
