import os

import pytest

from rainmaker.store.db import connect, init_schema
from rainmaker.store.query import count_rows, get_run
from rainmaker.store.record import record_run

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
