import sqlite3

from rainmaker.store.db import connect, init_schema

EXPECTED_TABLES = {
    "runs",
    "markets",
    "prices",
    "forecasts",
    "predictions",
    "outcomes",
    "calibration",
}


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {r[0] for r in rows}


def test_init_schema_creates_all_tables():
    conn = connect(":memory:")
    init_schema(conn)
    assert EXPECTED_TABLES <= _table_names(conn)
    conn.close()


def test_init_schema_is_idempotent():
    conn = connect(":memory:")
    init_schema(conn)
    init_schema(conn)  # re-running must not raise
    assert EXPECTED_TABLES <= _table_names(conn)
    conn.close()


def test_connect_enables_foreign_keys():
    conn = connect(":memory:")
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    conn.close()


def test_can_insert_and_read_a_run():
    conn = connect(":memory:")
    init_schema(conn)
    conn.execute(
        "INSERT INTO runs (id, started_at, finished_at, status, coverage) VALUES (?, ?, ?, ?, ?)",
        ("run-1", "2026-05-31T10:00:00Z", "2026-05-31T10:00:05Z", "ok", '{"nws": true}'),
    )
    conn.commit()
    row = conn.execute("SELECT id, status FROM runs WHERE id = ?", ("run-1",)).fetchone()
    assert row["id"] == "run-1"
    assert row["status"] == "ok"
    conn.close()


def test_foreign_key_violation_is_enforced():
    conn = connect(":memory:")
    init_schema(conn)
    # prices.market_id references markets(id); inserting an orphan must fail.
    try:
        conn.execute(
            "INSERT INTO prices (run_id, market_id, outcome, price, implied_prob, captured_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (None, "missing-market", "Yes", 0.4, 0.4, "2026-05-31T10:00:00Z"),
        )
        conn.commit()
        raised = False
    except sqlite3.IntegrityError:
        raised = True
    assert raised
    conn.close()
