import sqlite3

import pytest

from rainmaker.store.db import (
    _backend_for,
    _schema_for,
    _translate,
    connect,
    init_schema,
)

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


def test_backend_for_detects_postgres_and_sqlite():
    assert _backend_for("postgresql://u:p@host:5432/db") == "postgres"
    assert _backend_for("postgres://u:p@host/db") == "postgres"
    assert _backend_for("rainmaker.db") == "sqlite"
    assert _backend_for(":memory:") == "sqlite"


def test_translate_rewrites_placeholders():
    got = _translate("INSERT INTO t (a, b) VALUES (?, ?)", 2)
    assert got == "INSERT INTO t (a, b) VALUES (%s, %s)"
    assert _translate("SELECT 1", 0) == "SELECT 1"


def test_translate_guards_placeholder_count():
    with pytest.raises(ValueError):
        _translate("VALUES (?, ?)", 1)


def test_schema_for_uses_identity_only_on_postgres():
    # all three surrogate-key tables (prices, forecasts, predictions) must get an
    # identity column on Postgres; a partial .replace would break inserts at runtime
    assert _schema_for("postgres").count("GENERATED ALWAYS AS IDENTITY") == 3
    assert "GENERATED ALWAYS AS IDENTITY" not in _schema_for("sqlite")
    assert _schema_for("sqlite").count("INTEGER PRIMARY KEY") == 3
