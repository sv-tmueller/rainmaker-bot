import json
import os

import pytest

from rainmaker.backtest import crps_gaussian
from rainmaker.probability.calibration import numeric_crps, std_cdf_for
from rainmaker.store.db import connect, init_schema
from rainmaker.store.query import count_rows, get_run
from rainmaker.store.record import record_run
from rainmaker.tracking import compute_live_calibration

DSN = os.environ.get("DATABASE_URL")


@pytest.mark.skipif(not DSN, reason="DATABASE_URL not set; Postgres integration skipped")
def test_postgres_round_trip():
    conn = connect(DSN)
    try:
        init_schema(conn)
        conn.execute("DELETE FROM runs WHERE id = ?", ("it-roundtrip",))
        conn.commit()
        record_run(
            conn,
            run_id="it-roundtrip",
            started_at="2026-06-03T00:00:00Z",
            finished_at="2026-06-03T00:01:00Z",
            status="ok",
            evaluated=[],
        )
        run = get_run(conn, "it-roundtrip")
        assert run is not None and run["status"] == "ok"
        assert count_rows(conn, "runs") >= 1
        conn.execute("DELETE FROM runs WHERE id = ?", ("it-roundtrip",))
        conn.commit()
    finally:
        conn.close()


@pytest.mark.skipif(not DSN, reason="DATABASE_URL not set; Postgres integration skipped")
def test_postgres_float_columns_are_double_precision():
    """The six columns widened by migration 0010 must be float8, not float4.

    Postgres REAL is 4-byte and underflows on tiny tail-bucket probabilities;
    a fresh install and an upgraded install must both end up as float8.
    """
    conn = connect(DSN)
    try:
        init_schema(conn)
        rows = conn.execute(
            "SELECT table_name, column_name, data_type FROM information_schema.columns"
            " WHERE table_schema = 'public' AND ("
            "(table_name = ? AND column_name IN (?, ?))"
            " OR (table_name = ? AND column_name IN (?, ?, ?, ?))"
            ")",
            (
                "calibration",
                "var_a",
                "var_b",
                "forecast_accuracy",
                "crps",
                "coverage_50",
                "coverage_80",
                "coverage_90",
            ),
        ).fetchall()
        data_types = {(r["table_name"], r["column_name"]): r["data_type"] for r in rows}
        assert len(data_types) == 6
        assert all(dt == "double precision" for dt in data_types.values())
    finally:
        conn.close()


@pytest.mark.skipif(not DSN, reason="DATABASE_URL not set; Postgres integration skipped")
def test_postgres_mixed_regime_tracking_routes_by_family():
    """A no-df-key row, an explicit "df": null row, and a Student-t row in the
    same (variable, lead) group each score with their own family on Postgres,
    matching the SQLite-tested behaviour byte for byte (#292)."""
    market_ids = ["it-mix-1", "it-mix-2", "it-mix-3"]
    run_ids = ["it-mix-r1", "it-mix-r2", "it-mix-r3"]
    conn = connect(DSN)
    try:
        init_schema(conn)
        for mid in market_ids:
            conn.execute("DELETE FROM outcomes WHERE market_id = ?", (mid,))
            conn.execute("DELETE FROM predictions WHERE market_id = ?", (mid,))
            conn.execute("DELETE FROM markets WHERE id = ?", (mid,))
        for rid in run_ids:
            conn.execute("DELETE FROM runs WHERE id = ?", (rid,))
        conn.commit()

        started_at = "2026-07-01T12:00:00+00:00"
        settlement_date = "2026-07-02"
        for rid in run_ids:
            conn.execute(
                "INSERT INTO runs (id, started_at, status) VALUES (?, ?, ?)",
                (rid, started_at, "ok"),
            )
        for mid in market_ids:
            conn.execute(
                "INSERT INTO markets (id, city, variable, settlement_date) VALUES (?, ?, ?, ?)",
                (mid, "NYC", "TMIN", settlement_date),
            )

        # it-mix-1: legacy shape, no "df" key.
        conn.execute(
            "INSERT INTO predictions "
            "(run_id, market_id, bucket, side, p_win, dist_params, edge, recommended, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "it-mix-r1",
                "it-mix-1",
                "70-71°F",
                "YES",
                0.70,
                json.dumps({"mu": 70.0, "sigma": 2.0, "n_sources": 2}),
                0.1,
                1,
                started_at,
            ),
        )
        conn.execute(
            "INSERT INTO outcomes (market_id, actual_value, settled_at) VALUES (?, ?, ?)",
            ("it-mix-1", 70.5, started_at),
        )

        # it-mix-2: explicit "df": null (already-migrated Gaussian row).
        conn.execute(
            "INSERT INTO predictions "
            "(run_id, market_id, bucket, side, p_win, dist_params, edge, recommended, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "it-mix-r2",
                "it-mix-2",
                "60-61°F",
                "YES",
                0.60,
                json.dumps({"mu": 60.0, "sigma": 2.0, "n_sources": 2, "df": None}),
                0.1,
                1,
                started_at,
            ),
        )
        conn.execute(
            "INSERT INTO outcomes (market_id, actual_value, settled_at) VALUES (?, ?, ?)",
            ("it-mix-2", 60.5, started_at),
        )

        # it-mix-3: Student-t, df=5.0.
        conn.execute(
            "INSERT INTO predictions "
            "(run_id, market_id, bucket, side, p_win, dist_params, edge, recommended, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "it-mix-r3",
                "it-mix-3",
                "50-51°F",
                "YES",
                0.55,
                json.dumps({"mu": 50.0, "sigma": 2.0, "n_sources": 2, "df": 5.0}),
                0.1,
                1,
                started_at,
            ),
        )
        conn.execute(
            "INSERT INTO outcomes (market_id, actual_value, settled_at) VALUES (?, ?, ?)",
            ("it-mix-3", 53.0, started_at),
        )
        conn.commit()

        rows = [r for r in compute_live_calibration(conn) if r["variable"] == "TMIN"]
        # started_at (2026-07-01) vs settlement_date (2026-07-02) is lead 1.
        row = next(r for r in rows if r["lead_time"] == 1)
        assert row["n_samples"] == 3
        expected = (
            crps_gaussian(70.0, 2.0, 70.5)
            + crps_gaussian(60.0, 2.0, 60.5)
            + float(numeric_crps(std_cdf_for(5.0), 50.0, 2.0, 53.0)[0])
        ) / 3
        assert row["crps"] == pytest.approx(expected, abs=1e-9)

        for mid in market_ids:
            conn.execute("DELETE FROM outcomes WHERE market_id = ?", (mid,))
            conn.execute("DELETE FROM predictions WHERE market_id = ?", (mid,))
            conn.execute("DELETE FROM markets WHERE id = ?", (mid,))
        for rid in run_ids:
            conn.execute("DELETE FROM runs WHERE id = ?", (rid,))
        conn.commit()
    finally:
        conn.close()
