import { describe, expect, it } from "vitest";
import { STALE_RUN_HOURS, runFreshness } from "./freshness";

const HOUR = 3_600_000;
const MIN = 60_000;
const NOW = Date.parse("2026-11-13T16:00:00Z");
const ago = (ms: number) => new Date(NOW - ms).toISOString();

describe("runFreshness", () => {
  it("uses a 4 hour stale threshold", () => {
    expect(STALE_RUN_HOURS).toBe(4);
  });

  it("returns null when there is no run", () => {
    expect(runFreshness(null, NOW)).toBeNull();
  });

  it("labels a 2h old run as fresh", () => {
    expect(runFreshness(ago(2 * HOUR), NOW)).toEqual({ ageLabel: "2h ago", stale: false });
  });

  it("labels a 7h old run as stale", () => {
    expect(runFreshness(ago(7 * HOUR), NOW)).toEqual({ ageLabel: "7h ago", stale: true });
  });

  it("shows minutes under one hour", () => {
    expect(runFreshness(ago(25 * MIN), NOW)).toEqual({ ageLabel: "25m ago", stale: false });
  });

  it("floors hours", () => {
    expect(runFreshness(ago(2 * HOUR + 59 * MIN), NOW)?.ageLabel).toBe("2h ago");
  });

  it("treats exactly 4h as fresh and 4h plus a minute as stale", () => {
    expect(runFreshness(ago(4 * HOUR), NOW)?.stale).toBe(false);
    expect(runFreshness(ago(4 * HOUR + MIN), NOW)?.stale).toBe(true);
  });

  it("clamps a future timestamp (clock skew) to zero age", () => {
    expect(runFreshness(ago(-10 * MIN), NOW)).toEqual({ ageLabel: "0m ago", stale: false });
  });

  it("parses the Python isoformat form with microseconds and +00:00", () => {
    expect(runFreshness("2026-11-13T09:00:00.123456+00:00", NOW)).toEqual({
      ageLabel: "6h ago",
      stale: true,
    });
  });

  it("parses the Z form", () => {
    expect(runFreshness("2026-11-13T14:00:00Z", NOW)).toEqual({
      ageLabel: "2h ago",
      stale: false,
    });
  });

  it("fails closed on an unparseable timestamp", () => {
    expect(runFreshness("not a date", NOW)).toEqual({ ageLabel: "age unknown", stale: true });
  });
});
