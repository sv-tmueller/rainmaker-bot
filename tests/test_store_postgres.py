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
