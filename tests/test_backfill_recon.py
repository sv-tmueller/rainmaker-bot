"""Tests for the #462 reconstruction pair builder (backfill_recon).

Seeded sqlite store in, known pairs out. Pins: the unified mixture sigma
(floored at MIN_SIGMA_F), lead classification via the forecasts table's own
lead_time column with the UTC-day fallback, the TMIN pre-retarget era
exclusion, Kalshi exclusion, and the latest-run-per-(market, UTC day)
collapse.
"""

import json
from datetime import date

import numpy as np
import pytest

from rainmaker.backfill_recon import (
    TMIN_RETROFIT_DATE,
    build_recon_pairs,
    fetch_pools,
    fetch_settled_rows,
    lead_from_columns,
    pair_from_pool,
    routing_table,
)
from rainmaker.config import MIN_CAL_SAMPLES, MIN_SIGMA_F
from rainmaker.store.db import connect, init_schema


def seed_conn():
    conn = connect(":memory:")
    init_schema(conn)
    return conn


def add_run(conn, run_id: str, started_at: str) -> None:
    conn.execute(
        "INSERT INTO runs (id, started_at, finished_at, status) VALUES (?, ?, ?, ?)",
        (run_id, started_at, f"{started_at[:-1]}Z", "ok"),
    )


def add_temp_market(
    conn,
    market_id: str,
    city: str,
    variable: str,
    settlement_date: str,
    venue: str | None = "polymarket",
):
    conn.execute(
        "INSERT INTO markets (id, slug, title, city, variable, settlement_date, venue) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (market_id, market_id, market_id, city, variable, settlement_date, venue),
    )


def add_prediction(
    conn, run_id: str, market_id: str, dist_params: str | None = '{"mu": 70}'
) -> None:
    conn.execute(
        "INSERT INTO predictions (run_id, market_id, p_win, dist_params, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (run_id, market_id, 0.5, dist_params, "2026-10-01T12:00:00Z"),
    )


def add_outcome(conn, market_id: str, actual_value: float) -> None:
    conn.execute(
        "INSERT INTO outcomes (market_id, actual_value, won, settled_at) VALUES (?, ?, ?, ?)",
        (market_id, actual_value, 1, "2026-10-01T12:00:00Z"),
    )


def add_forecast(
    conn, run_id: str, market_id: str, source: str, model: str, values: list[float], lead_time
) -> None:
    conn.execute(
        "INSERT INTO forecasts "
        "(run_id, market_id, source, model, variable, values_json, lead_time, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            market_id,
            source,
            model,
            "TMAX",
            json.dumps(values),
            lead_time,
            "2026-10-01T12:00:00Z",
        ),
    )


def _populated_conn(**kwargs):
    """One settled NYC TMAX market with a two-group pool, plus kwargs overrides.

    Pool: nws singleton at 71.0, open-meteo/gfs singleton at 73.0 ->
    mixture sigma = population std of [71, 73] = 1.0, floored to MIN_SIGMA_F.
    mu = 72.0. Run 2026-10-01, settlement 2026-10-02, lead column 1.
    """
    conn = seed_conn()
    add_run(conn, "r1", "2026-10-01T12:00:00Z")
    add_temp_market(conn, "m1", "NYC", "TMAX", "2026-10-02")
    add_prediction(conn, "r1", "m1")
    add_outcome(conn, "m1", 70.0)
    add_forecast(conn, "r1", "m1", "nws", "nws", [71.0], 1)
    add_forecast(conn, "r1", "m1", "open-meteo", "gfs", [73.0], 1)
    return conn


def test_pair_from_pool_known_mixture_sigma():
    pool = {("nws", "nws"): [71.0], ("open-meteo", "gfs"): [73.0]}
    pair = pair_from_pool(pool, actual=70.0)
    assert pair is not None
    assert pair.mu == pytest.approx(72.0)
    # mixture over singletons = population std (ddof=0) of [71, 73] = 1.0,
    # floored at MIN_SIGMA_F on the fit side, same as the live apply side.
    assert pair.sigma == pytest.approx(max(1.0, MIN_SIGMA_F))
    assert pair.ensemble_var == pytest.approx(pair.sigma**2)
    assert pair.actual == 70.0


def test_pair_from_pool_wide_ensemble_beats_floor():
    # 5-member ensemble 70..74 (within var 2.5) + det singleton at 80:
    # mixture sigma >> floor, exercising the unfloored path.
    pool = {("open-meteo", "gfs_ens"): [70.0, 71.0, 72.0, 73.0, 74.0], ("nws", "nws"): [80.0]}
    pair = pair_from_pool(pool, actual=70.0)
    assert pair is not None
    expected = float(np.sqrt(((2.5 + 16.0) + 16.0) / 2))
    assert pair.sigma == pytest.approx(expected)
    assert pair.sigma > MIN_SIGMA_F


def test_pair_from_pool_empty_is_none():
    assert pair_from_pool({}, actual=70.0) is None


def test_build_recon_pairs_buckets_by_cell_and_uses_lead_column():
    conn = _populated_conn()
    pairs, counts = build_recon_pairs(conn)
    assert counts["rows_read"] == 1
    assert counts["skipped_no_pool"] == 0
    assert counts["null_lead_utc_fallback_used"] == 0
    # lead classified from the forecasts.lead_time column (=1), not derived
    assert list(pairs) == [("KLGA", "TMAX", 1)]
    pair = pairs[("KLGA", "TMAX", 1)][0]
    assert pair.mu == pytest.approx(72.0)
    assert pair.sigma == pytest.approx(MIN_SIGMA_F)
    assert pair.actual == pytest.approx(70.0)


def test_build_recon_pairs_null_lead_falls_back_to_utc_day_arithmetic():
    conn = _populated_conn()
    conn.execute("UPDATE forecasts SET lead_time = NULL")
    pairs, counts = build_recon_pairs(conn)
    # settlement 2026-10-02 minus run day 2026-10-01 -> lead 1
    assert list(pairs) == [("KLGA", "TMAX", 1)]
    assert counts["null_lead_utc_fallback_used"] == 1


def test_build_recon_pairs_column_beats_utc_derivation():
    # Evening run (23:00Z) on settlement-minus-two: UTC arithmetic says lead 2
    # (late-evening runs read one high), the column says 1. Column wins (#460).
    conn = seed_conn()
    add_run(conn, "r1", "2026-09-30T23:00:00Z")
    add_temp_market(conn, "m1", "NYC", "TMAX", "2026-10-02")
    add_prediction(conn, "r1", "m1")
    add_outcome(conn, "m1", 70.0)
    add_forecast(conn, "r1", "m1", "nws", "nws", [71.0], 1)
    add_forecast(conn, "r1", "m1", "open-meteo", "gfs", [73.0], 1)
    pairs, _counts = build_recon_pairs(conn)
    assert list(pairs) == [("KLGA", "TMAX", 1)]


def test_build_recon_pairs_excludes_pre_retrofit_tmin():
    conn = _populated_conn()
    conn.execute("UPDATE markets SET variable = 'TMIN'")
    pairs, counts = build_recon_pairs(conn)
    # run day 2026-10-01 predates the 2026-10-19 retarget: excluded wholesale
    assert pairs == {}
    assert counts["skipped_tmin_pre_retrofit"] == 1


def test_build_recon_pairs_keeps_post_retrofit_tmin():
    conn = seed_conn()
    # A post-retarget run: run day == retrofit date, settlement the next day.
    add_run(conn, "r1", f"{TMIN_RETROFIT_DATE.isoformat()}T12:00:00Z")
    add_temp_market(conn, "m1", "NYC", "TMIN", f"{date(2026, 10, 20).isoformat()}")
    add_prediction(conn, "r1", "m1")
    add_outcome(conn, "m1", 70.0)
    add_forecast(conn, "r1", "m1", "nws", "nws", [71.0], 1)
    add_forecast(conn, "r1", "m1", "open-meteo", "gfs", [73.0], 1)
    pairs, counts = build_recon_pairs(conn)
    assert counts["skipped_tmin_pre_retrofit"] == 0
    assert list(pairs) == [("KLGA", "TMIN", 1)]


def test_build_recon_pairs_excludes_kalshi_venues_and_stations():
    conn = _populated_conn()
    # Kalshi venue on a Polymarket city: excluded by venue
    add_run(conn, "r2", "2026-10-01T12:00:00Z")
    add_temp_market(conn, "mk", "NYC", "TMAX", "2026-10-02", venue="kalshi")
    add_prediction(conn, "r2", "mk")
    add_outcome(conn, "mk", 70.0)
    add_forecast(conn, "r2", "mk", "nws", "nws", [71.0], 1)
    # Polymarket venue on a Kalshi-only city (Midway/Chicago is a Polymarket
    # city too, so use a venue-tagged Kalshi market on NYC's twin instead):
    # the venue tag alone excludes mk; a Kalshi-only CITY is impossible on the
    # Polymarket venue, so the station guard is exercised via venue=NULL rows
    # on a Kalshi city (mc below).
    add_run(conn, "r3", "2026-10-01T12:00:00Z")
    add_temp_market(conn, "mc", "Chicago", "TMAX", "2026-10-02", venue=None)
    add_prediction(conn, "r3", "mc")
    add_outcome(conn, "mc", 70.0)
    add_forecast(conn, "r3", "mc", "nws", "nws", [71.0], 1)
    pairs, counts = build_recon_pairs(conn)
    assert list(pairs.keys()).count(("KLGA", "TMAX", 1)) == 1
    # Chicago resolves to KORD (a Polymarket station), so mc SURVIVES the
    # station guard; only the venue-tagged mk is gone.
    assert ("KORD", "TMAX", 1) in pairs
    assert sum(len(v) for v in pairs.values()) == 2


def test_build_recon_pairs_collapses_latest_run_per_market_day():
    conn = _populated_conn()
    # Earlier run same UTC day on the same market: superseded
    add_run(conn, "r0", "2026-10-01T06:00:00Z")
    add_prediction(conn, "r0", "m1")
    add_forecast(conn, "r0", "m1", "nws", "nws", [50.0], 1)
    add_forecast(conn, "r0", "m1", "open-meteo", "gfs", [52.0], 1)
    pairs, counts = build_recon_pairs(conn)
    assert counts["rows_read"] == 2
    assert len(pairs[("KLGA", "TMAX", 1)]) == 1
    # the surviving pair is from the later run (mu 72, not 51)
    assert pairs[("KLGA", "TMAX", 1)][0].mu == pytest.approx(72.0)


def test_build_recon_pairs_since_filters_runs():
    conn = _populated_conn()
    pairs, _ = build_recon_pairs(conn, since="2026-10-02")
    assert pairs == {}


def test_fetch_pools_decodes_values_json_defensively():
    conn = _populated_conn()
    conn.execute(
        "INSERT INTO forecasts "
        "(run_id, market_id, source, model, variable, values_json, lead_time, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("r1", "m1", "junk", "junk", "TMAX", "{not json", 1, "2026-10-01T12:00:00Z"),
    )
    pools, leads = fetch_pools(conn, [("r1", "m1")])
    # junk row skipped; the two good groups survive
    assert set(pools[("r1", "m1")]) == {("nws", "nws"), ("open-meteo", "gfs")}
    assert leads[("r1", "m1")] == 1


def test_routing_table_threshold_split():
    filler = pair_from_pool({("nws", "nws"): [71.0], ("open-meteo", "gfs"): [73.0]}, 70.0)
    assert filler is not None
    pairs = {
        ("KLGA", "TMAX", 0): [filler] * MIN_CAL_SAMPLES,
        ("KLGA", "TMAX", 1): [filler] * (MIN_CAL_SAMPLES - 1),
    }
    table = routing_table(pairs, min_cal_samples=MIN_CAL_SAMPLES)
    assert table[0]["route"] == "recon"
    assert table[1]["route"] == "archive"


def test_lead_from_columns_prefers_column():
    row = {"settlement_date": "2026-10-02", "started_at": "2026-09-30T23:00:00Z"}
    assert lead_from_columns(1, row) == 1
    assert lead_from_columns(None, row) == 2  # UTC-day fallback
    assert lead_from_columns(None, {"settlement_date": None, "started_at": None}) is None
    assert (
        lead_from_columns(None, {"settlement_date": "2026-09-30", "started_at": "2026-10-01"})
        is None
    )


def test_fetch_settled_rows_requires_dist_params_and_actual():
    conn = _populated_conn()
    # prediction without dist_params: invisible
    add_run(conn, "rx", "2026-10-01T12:00:00Z")
    add_temp_market(conn, "mx", "NYC", "TMAX", "2026-10-02")
    add_prediction(conn, "rx", "mx", dist_params=None)
    add_outcome(conn, "mx", 70.0)
    rows, _stats = fetch_settled_rows(conn)
    assert {r["market_id"] for r in rows} == {"m1"}


class _RecordingConn:
    """Test-local wrapper: records the rows each `FROM forecasts` SELECT returns."""

    def __init__(self, inner):
        self._inner = inner
        self.forecast_rows: list[tuple[str, str]] = []

    def execute(self, sql, params=()):
        cursor = self._inner.execute(sql, params)
        if "FROM forecasts" not in sql:
            return cursor
        outer = self

        class _Cursor:
            def fetchall(self):
                rows = cursor.fetchall()
                outer.forecast_rows.extend((r["run_id"], r["market_id"]) for r in rows)
                return rows

        return _Cursor()


def test_fetch_pools_reads_only_the_requested_run_market_pairs():
    # One run carries forecasts for a settled Polymarket market (m1), an
    # unsettled market (m2) and a settled Kalshi-venue market (m3). Only m1 is
    # asked for, so only m1's rows may leave the database (#466).
    conn = _populated_conn()
    add_temp_market(conn, "m2", "NYC", "TMAX", "2026-10-03")
    add_prediction(conn, "r1", "m2")
    add_temp_market(conn, "m3", "NYC", "TMAX", "2026-10-02", venue="kalshi")
    add_prediction(conn, "r1", "m3")
    add_outcome(conn, "m3", 70.0)
    for mid in ("m2", "m3"):
        add_forecast(conn, "r1", mid, "nws", "nws", [71.0], 1)
        add_forecast(conn, "r1", mid, "open-meteo", "gfs", [73.0], 1)
    recording = _RecordingConn(conn)
    rows, _stats = fetch_settled_rows(conn)
    assert {r["market_id"] for r in rows} == {"m1"}
    pools, _leads = fetch_pools(recording, [(r["run_id"], r["market_id"]) for r in rows])
    assert set(pools) == {("r1", "m1")}
    assert set(recording.forecast_rows) == {("r1", "m1")}


def test_fetch_pools_spans_more_than_one_query_chunk():
    # 260 pairs in one run cross the 250-pair chunk boundary; every pool and
    # its lead must survive the split.
    conn = seed_conn()
    add_run(conn, "r1", "2026-10-01T12:00:00Z")
    pairs = []
    for i in range(260):
        mid = f"m{i}"
        add_temp_market(conn, mid, "NYC", "TMAX", "2026-10-02")
        add_forecast(conn, "r1", mid, "nws", "nws", [70.0 + i], 1)
        pairs.append(("r1", mid))
    pools, leads = fetch_pools(conn, pairs)
    assert set(pools) == set(pairs)
    assert pools[("r1", "m259")] == {("nws", "nws"): [329.0]}
    assert set(leads.values()) == {1}
