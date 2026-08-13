import { describe, expect, it } from "vitest";
import { orderSnapshotRows, splitSnapshotsByVenue, type RawSnapshotRow } from "./data";

// splitSnapshotsByVenue is the whole graceful-degradation contract described
// in the sub-plan: aggregate = venue 'all' | null | absent (the last case
// covers a DB where the sibling's column does not exist yet, since the read
// is select("*")); venue arrays only get 'polymarket' | 'kalshi' rows.

function row(overrides: Partial<RawSnapshotRow> & { snapshot_date: string }): RawSnapshotRow {
  return {
    snapshot_date: overrides.snapshot_date,
    n_bets: 1,
    wins: 1,
    losses: 0,
    total_pnl: 0.5,
    roi: 0.5,
    brier: 0.1,
    hit_rate: 1,
    n_scored: 1,
    ...overrides,
  } as RawSnapshotRow;
}

describe("splitSnapshotsByVenue: mixed rows", () => {
  const rows = [
    row({ snapshot_date: "2026-08-01", venue: "all", n_bets: 10 }),
    row({ snapshot_date: "2026-08-01", venue: "polymarket", n_bets: 6 }),
    row({ snapshot_date: "2026-08-01", venue: "kalshi", n_bets: 4 }),
  ];

  it("splits into total, polymarket, and kalshi arrays", () => {
    const result = splitSnapshotsByVenue(rows);
    expect(result.total).toHaveLength(1);
    expect(result.polymarket).toHaveLength(1);
    expect(result.kalshi).toHaveLength(1);
  });

  it("routes each row to the array matching its venue", () => {
    const result = splitSnapshotsByVenue(rows);
    expect(result.total[0].nBets).toBe(10);
    expect(result.polymarket[0].nBets).toBe(6);
    expect(result.kalshi[0].nBets).toBe(4);
  });
});

describe("splitSnapshotsByVenue: no venue key at all", () => {
  it("yields total-only, without merging in a sibling schema", () => {
    const rows = [row({ snapshot_date: "2026-08-01" })];
    // Simulate select("*") against a DB predating the sibling's migration:
    // the column does not exist, so the row object has no "venue" property.
    delete (rows[0] as { venue?: string }).venue;

    const result = splitSnapshotsByVenue(rows);
    expect(result.total).toHaveLength(1);
    expect(result.polymarket).toHaveLength(0);
    expect(result.kalshi).toHaveLength(0);
  });
});

describe("splitSnapshotsByVenue: venue='all'-only rows", () => {
  it("yields total-only", () => {
    const rows = [
      row({ snapshot_date: "2026-08-01", venue: "all" }),
      row({ snapshot_date: "2026-08-02", venue: "all" }),
    ];

    const result = splitSnapshotsByVenue(rows);
    expect(result.total).toHaveLength(2);
    expect(result.polymarket).toHaveLength(0);
    expect(result.kalshi).toHaveLength(0);
  });
});

describe("splitSnapshotsByVenue: venue series tolerate missing dates", () => {
  it("does not backfill a venue series for a date with no row for that venue", () => {
    const rows = [
      row({ snapshot_date: "2026-08-01", venue: "all" }),
      row({ snapshot_date: "2026-08-01", venue: "polymarket" }),
      row({ snapshot_date: "2026-08-02", venue: "all" }),
      // 2026-08-02 has no polymarket row (e.g. sibling deployed mid-history).
    ];

    const result = splitSnapshotsByVenue(rows);
    expect(result.total.map((s) => s.date)).toEqual(["2026-08-01", "2026-08-02"]);
    expect(result.polymarket.map((s) => s.date)).toEqual(["2026-08-01"]);
    expect(result.kalshi).toHaveLength(0);
  });
});

describe("orderSnapshotRows: descending-input-reversed-to-ascending ordering", () => {
  it("reverses a descending-by-date input to ascending order", () => {
    const rows = [
      row({ snapshot_date: "2026-08-03" }),
      row({ snapshot_date: "2026-08-02" }),
      row({ snapshot_date: "2026-08-01" }),
    ];

    const result = orderSnapshotRows(rows);
    expect(result.map((r) => r.snapshot_date)).toEqual(["2026-08-01", "2026-08-02", "2026-08-03"]);
  });

  it("does not mutate the input array", () => {
    const rows = [row({ snapshot_date: "2026-08-02" }), row({ snapshot_date: "2026-08-01" })];
    const before = rows.map((r) => r.snapshot_date);

    orderSnapshotRows(rows);

    expect(rows.map((r) => r.snapshot_date)).toEqual(before);
  });
});
