import sqlite3

import psycopg
import pytest

from rainmaker.store.db import (
    PG_CONNECT_ATTEMPTS,
    _backend_for,
    _schema_for,
    _translate,
    connect,
    init_schema,
)

FAKE_DSN = "postgresql://fake:fake@localhost/fake"

EXPECTED_TABLES = {
    "runs",
    "markets",
    "prices",
    "forecasts",
    "predictions",
    "outcomes",
    "calibration",
    "forecast_accuracy",
    "tracking_snapshot",
}


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {r[0] for r in rows}


def test_init_schema_creates_all_tables():
    conn = connect(":memory:")
    init_schema(conn)
    # exact equality (not a subset) so a future table omitted from the schema or
    # from EXPECTED_TABLES is caught. schema_migrations is created by the migrator.
    assert _table_names(conn) == EXPECTED_TABLES | {"schema_migrations"}
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
    pg = _schema_for("postgres")
    sl = _schema_for("sqlite")
    # all three surrogate-key tables (prices, forecasts, predictions) must get an
    # identity column on Postgres; a partial .replace would break inserts at runtime
    assert pg.count("GENERATED ALWAYS AS IDENTITY") == 3
    assert "GENERATED ALWAYS AS IDENTITY" not in sl
    assert sl.count("INTEGER PRIMARY KEY") == 3


def test_postgres_schema_uses_double_precision_not_real():
    # Postgres REAL is float4 and underflows on tiny tail probabilities; float8 matches SQLite
    pg = _schema_for("postgres")
    sl = _schema_for("sqlite")
    assert " REAL," not in pg
    assert "DOUBLE PRECISION" in pg
    assert " REAL," in sl  # SQLite REAL is already 8-byte, left as-is


def test_connect_retries_transient_operational_error_then_succeeds(monkeypatch):
    stub = object()
    calls = []

    def fake_connect(dsn, row_factory=None):
        calls.append(dsn)
        if len(calls) == 1:
            # the actual subclass from the 2026-07-26 incident (run 30198856442),
            # not a generic stand-in, so this pins the exact exception being retried
            raise psycopg.errors.ConnectionTimeout("connection timeout expired")
        return stub

    monkeypatch.setattr(psycopg, "connect", fake_connect)
    sleeps = []
    conn = connect(FAKE_DSN, sleep=sleeps.append)
    assert conn.backend == "postgres"
    assert conn._raw is stub
    assert len(calls) == 2
    assert sleeps == [1.0]


def test_connect_exhausts_retries_and_reraises_original_error(monkeypatch):
    calls = []
    orig = psycopg.OperationalError("connection refused")

    def fake_connect(dsn, row_factory=None):
        calls.append(dsn)
        raise orig

    monkeypatch.setattr(psycopg, "connect", fake_connect)
    sleeps = []
    with pytest.raises(psycopg.OperationalError, match="connection refused") as exc_info:
        connect(FAKE_DSN, sleep=sleeps.append)
    # identity, not just type/message: pins the "re-raises the original error
    # unchanged" contract against a future refactor that reconstructs a
    # same-typed, same-message exception instead of a bare re-raise
    assert exc_info.value is orig
    assert len(calls) == PG_CONNECT_ATTEMPTS
    assert sleeps == [1.0, 2.0, 4.0]


def test_connect_succeeds_first_try_is_not_retried(monkeypatch):
    stub = object()
    calls = []

    def fake_connect(dsn, row_factory=None):
        calls.append(dsn)
        return stub

    monkeypatch.setattr(psycopg, "connect", fake_connect)
    sleeps = []
    conn = connect(FAKE_DSN, sleep=sleeps.append)
    assert conn._raw is stub
    assert len(calls) == 1
    assert sleeps == []


def test_connect_non_operational_error_propagates_without_retry(monkeypatch):
    calls = []

    def fake_connect(dsn, row_factory=None):
        calls.append(dsn)
        raise ValueError("not an OperationalError")

    monkeypatch.setattr(psycopg, "connect", fake_connect)
    sleeps = []
    with pytest.raises(ValueError, match="not an OperationalError"):
        connect(FAKE_DSN, sleep=sleeps.append)
    assert len(calls) == 1
    assert sleeps == []


def test_connect_sqlite_path_needs_no_sleep_argument():
    # the sqlite branch takes no retry parameter at all; this pins that it
    # keeps working with the default call shape every other caller uses.
    conn = connect(":memory:")
    assert conn.backend == "sqlite"
    conn.close()
