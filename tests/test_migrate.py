import re
import sqlite3

import pytest

from rainmaker.store.db import _SQLITE_SCHEMA, connect, init_schema
from rainmaker.store.migrate import (
    _MIGRATIONS,
    _RLS_TABLES,
    _WIDEN_FLOAT4_COLUMNS,
    _backfill_venue,
    _enable_rls_statements,
    _for_backend,
    _is_duplicate_column,
    apply_migrations,
)


def test_migration_adds_predictions_bucket_column():
    conn = connect(":memory:")
    init_schema(conn)
    conn.execute(
        "INSERT INTO predictions (run_id, market_id, bucket, p_win, edge, recommended, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (None, None, "70-71°F", 0.9, 0.1, 1, "2026-06-04T00:00:00Z"),
    )
    conn.commit()
    row = conn.execute("SELECT bucket FROM predictions").fetchone()
    conn.close()
    assert row["bucket"] == "70-71°F"


def test_migration_adds_side_columns():
    conn = connect(":memory:")
    init_schema(conn)
    conn.execute(
        "INSERT INTO predictions (run_id, bucket, side, p_win, edge, recommended, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (None, "70-71°F", "NO", 0.9, 0.1, 1, "t"),
    )
    conn.execute(
        "INSERT INTO prices (run_id, market_id, outcome, side, price, captured_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (None, None, "70-71°F", "NO", 0.7, "t"),
    )
    conn.commit()
    assert conn.execute("SELECT side FROM predictions").fetchone()["side"] == "NO"
    assert conn.execute("SELECT side FROM prices").fetchone()["side"] == "NO"
    conn.close()


def test_migration_adds_markets_settlement_ghcnd_column():
    conn = connect(":memory:")
    init_schema(conn)
    conn.execute("INSERT INTO markets (id, settlement_ghcnd) VALUES (?, ?)", ("m", "USW00094728"))
    conn.commit()
    row = conn.execute("SELECT settlement_ghcnd FROM markets").fetchone()
    conn.close()
    assert row["settlement_ghcnd"] == "USW00094728"


def test_migration_adds_markets_venue_column():
    conn = connect(":memory:")
    init_schema(conn)
    conn.execute("INSERT INTO markets (id, venue) VALUES (?, ?)", ("m", "kalshi"))
    conn.commit()
    row = conn.execute("SELECT venue FROM markets").fetchone()
    conn.close()
    assert row["venue"] == "kalshi"


def test_apply_migrations_is_idempotent():
    conn = connect(":memory:")
    init_schema(conn)
    apply_migrations(conn)  # second pass must not error
    n = conn.execute("SELECT count(*) AS n FROM schema_migrations").fetchone()["n"]
    conn.close()
    # _MIGRATIONS holds DDL steps; 0007_backfill_venue, 0010_widen_float4_columns,
    # and 0013_enable_rls are recorded as separate Python steps outside that list,
    # so the total count is len(_MIGRATIONS) + 3.
    assert n == len(_MIGRATIONS) + 3


def test_migration_statements_render_real_as_double_precision_for_postgres():
    for _migration_id, statements in _MIGRATIONS:
        for statement in statements:
            rendered = _for_backend(statement, "postgres")
            assert "REAL" not in rendered
            assert not re.search(r"\breal\b", rendered, re.IGNORECASE)
            if "REAL" in statement:
                assert "DOUBLE PRECISION" in rendered


def test_migration_statements_unchanged_for_sqlite():
    for _migration_id, statements in _MIGRATIONS:
        for statement in statements:
            assert _for_backend(statement, "sqlite") == statement


def test_widen_float4_columns_statements_are_exact():
    # Corrective ALTERs for the six columns that landed as float4 on Postgres
    # before the REAL -> DOUBLE PRECISION substitution existed (0008, 0009).
    assert _WIDEN_FLOAT4_COLUMNS == [
        "ALTER TABLE calibration ALTER COLUMN var_a TYPE double precision",
        "ALTER TABLE calibration ALTER COLUMN var_b TYPE double precision",
        "ALTER TABLE forecast_accuracy ALTER COLUMN crps TYPE double precision",
        "ALTER TABLE forecast_accuracy ALTER COLUMN coverage_50 TYPE double precision",
        "ALTER TABLE forecast_accuracy ALTER COLUMN coverage_80 TYPE double precision",
        "ALTER TABLE forecast_accuracy ALTER COLUMN coverage_90 TYPE double precision",
    ]


def test_apply_migrations_records_0010_once_on_sqlite():
    conn = connect(":memory:")
    init_schema(conn)  # already applies migrations once
    ids_before = {r["id"] for r in conn.execute("SELECT id FROM schema_migrations").fetchall()}
    assert "0010_widen_float4_columns" in ids_before

    apply_migrations(conn)  # second pass must be a no-op, not re-insert

    ids_after = {r["id"] for r in conn.execute("SELECT id FROM schema_migrations").fetchall()}
    conn.close()
    assert ids_after == ids_before


def test_is_duplicate_column_only_matches_the_two_exact_signals():
    # SQLite's duplicate-column message and Postgres SQLSTATE 42701 are the only
    # signals; both cover every ADD COLUMN migration.
    assert _is_duplicate_column(sqlite3.OperationalError("duplicate column name: venue")) is True

    class _PgDuplicateColumn(Exception):
        sqlstate = "42701"

    assert _is_duplicate_column(_PgDuplicateColumn()) is True

    # A future CREATE TABLE/INDEX "already exists" error must NOT be swallowed and
    # falsely recorded as applied: only the two exact signals count.
    assert _is_duplicate_column(sqlite3.OperationalError("table foo already exists")) is False
    assert _is_duplicate_column(sqlite3.OperationalError("index ix already exists")) is False

    class _PgDuplicateTable(Exception):
        sqlstate = "42P07"  # duplicate_table, not duplicate_column

    assert _is_duplicate_column(_PgDuplicateTable()) is False


def test_migration_adds_predictions_won_column():
    conn = connect(":memory:")
    init_schema(conn)
    cols = "run_id, market_id, bucket, side, p_win, edge, recommended, won, created_at"
    conn.execute(
        f"INSERT INTO predictions ({cols}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (None, None, "70-71°F", "YES", 0.8, 0.1, 1, 1, "t"),
    )
    conn.commit()
    row = conn.execute("SELECT won FROM predictions").fetchone()
    conn.close()
    assert row["won"] == 1


def test_migration_adds_calibration_emos_columns():
    conn = connect(":memory:")
    init_schema(conn)
    conn.execute(
        "INSERT INTO calibration (station, variable, lead_time, bias, var_a, var_b, n_samples)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("KLGA", "TMAX", 1, -0.5, 1.2, 0.8, 30),
    )
    conn.commit()
    row = conn.execute("SELECT var_a, var_b FROM calibration").fetchone()
    conn.close()
    assert row["var_a"] == pytest.approx(1.2)
    assert row["var_b"] == pytest.approx(0.8)


def test_load_calibration_returns_none_for_null_emos_columns():
    """load_calibration returns None when var_a/var_b are NULL (pre-EMOS rows after migration).

    Migration 0008 adds var_a/var_b as nullable columns; rows written by the old
    code retain NULL in those fields. Passing NULLs into Calibration() would fail
    Pydantic validation (Field(ge=0) rejects None). The loader must return None
    instead so the uncalibrated-widen fallback is used.
    """
    from rainmaker.store.query import load_calibration

    conn = connect(":memory:")
    init_schema(conn)
    # Simulate a legacy row: var_a and var_b are NULL.
    conn.execute(
        "INSERT INTO calibration (station, variable, lead_time, bias, var_a, var_b, n_samples)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("KLGA", "TMAX", 1, -0.5, None, None, 30),
    )
    conn.commit()
    result = load_calibration(conn, "KLGA", "TMAX", 1)
    conn.close()
    assert result is None


def test_migration_adds_calibration_df_column():
    conn = connect(":memory:")
    init_schema(conn)
    conn.execute(
        "INSERT INTO calibration (station, variable, lead_time, bias, var_a, var_b, df, n_samples)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("KKMDW", "TMIN", 1, -0.5, 1.2, 0.8, 6.5, 45),
    )
    conn.commit()
    row = conn.execute("SELECT df FROM calibration").fetchone()
    conn.close()
    assert row["df"] == pytest.approx(6.5)


def test_apply_migrations_crash_safe_when_alter_already_applied():
    """apply_migrations must succeed when a column was added but never recorded.

    Simulates a crash after the 0001 ALTER TABLE ran but before its
    schema_migrations INSERT committed.  The column exists; no tracking row
    does.  apply_migrations must recover (skip duplicate, record 0001) then
    apply 0002-0005 normally - testing both paths in one call.
    """
    # Create the base tables only (no migration columns, no schema_migrations).
    conn = connect(":memory:")
    for stmt in _SQLITE_SCHEMA.split(";"):
        if stmt.strip():
            conn.execute(stmt)
    conn.commit()

    # Simulate: 0001 ALTER ran but the process crashed before the INSERT.
    # Apply the first migration's SQL manually without recording it.
    first_id, first_stmts = _MIGRATIONS[0]
    for stmt in first_stmts:
        conn.execute(stmt)
    conn.commit()

    # apply_migrations must not raise 'duplicate column name' for 0001 and must
    # apply 0002-0007 forward normally.
    apply_migrations(conn)

    rows = {r["id"] for r in conn.execute("SELECT id FROM schema_migrations").fetchall()}
    conn.close()
    # _MIGRATIONS holds DDL steps; 0007_backfill_venue, 0010_widen_float4_columns,
    # and 0013_enable_rls are recorded outside that list.
    assert rows == {mid for mid, _ in _MIGRATIONS} | {
        "0007_backfill_venue",
        "0010_widen_float4_columns",
        "0013_enable_rls",
    }


def test_backfill_venue_sets_polymarket_for_numeric_id():
    """A NULL-venue market with a numeric id is inferred as 'polymarket'."""
    conn = connect(":memory:")
    init_schema(conn)
    # Insert a legacy market: numeric id, no venue (simulates pre-0005 row)
    conn.execute("INSERT INTO markets (id) VALUES (?)", ("700001",))
    conn.commit()

    _backfill_venue(conn)

    row = conn.execute("SELECT venue FROM markets WHERE id = ?", ("700001",)).fetchone()
    conn.close()
    assert row["venue"] == "polymarket"


def test_backfill_venue_sets_kalshi_for_ticker_id():
    """A NULL-venue market with an alphanumeric ticker id is inferred as 'kalshi'."""
    conn = connect(":memory:")
    init_schema(conn)
    conn.execute("INSERT INTO markets (id) VALUES (?)", ("KXHIGHNY-26JUN08",))
    conn.commit()

    _backfill_venue(conn)

    row = conn.execute("SELECT venue FROM markets WHERE id = ?", ("KXHIGHNY-26JUN08",)).fetchone()
    conn.close()
    assert row["venue"] == "kalshi"


def test_backfill_venue_does_not_overwrite_explicit_venue():
    """A market that already has a venue set is not overwritten."""
    conn = connect(":memory:")
    init_schema(conn)
    conn.execute("INSERT INTO markets (id, venue) VALUES (?, ?)", ("700001", "kalshi"))
    conn.commit()

    _backfill_venue(conn)

    row = conn.execute("SELECT venue FROM markets WHERE id = ?", ("700001",)).fetchone()
    conn.close()
    # Non-NULL venue must not be touched; only venue IS NULL rows are backfilled
    assert row["venue"] == "kalshi"


def test_migration_0012_backfills_venue_all_for_legacy_row():
    """A legacy tracking_snapshot row (no venue column) gets venue='all' after
    apply_migrations, and the new composite PK allows a second venue row for the
    same date.
    """
    conn = connect(":memory:")
    conn.execute("CREATE TABLE schema_migrations (id TEXT PRIMARY KEY, applied_at TEXT)")
    # The pre-#344 shape: single-column PK, no venue.
    conn.execute(
        "CREATE TABLE tracking_snapshot ("
        "snapshot_date TEXT PRIMARY KEY, n_bets INTEGER, wins INTEGER, losses INTEGER, "
        "total_pnl REAL, roi REAL, brier REAL, hit_rate REAL, n_scored INTEGER, "
        "created_at TEXT)"
    )
    already_applied = [m for m, _ in _MIGRATIONS if m != "0012_tracking_snapshot_venue"] + [
        "0007_backfill_venue",
        "0010_widen_float4_columns",
        "0013_enable_rls",
    ]
    for mid in already_applied:
        conn.execute("INSERT INTO schema_migrations (id, applied_at) VALUES (?, ?)", (mid, "t"))
    conn.execute(
        "INSERT INTO tracking_snapshot "
        "(snapshot_date, n_bets, wins, losses, total_pnl, roi, brier, hit_rate, "
        "n_scored, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("2026-06-04", 2, 1, 1, 0.3, 0.42, 0.13, 0.5, 2, "t"),
    )
    conn.commit()

    apply_migrations(conn)

    row = conn.execute(
        "SELECT * FROM tracking_snapshot WHERE snapshot_date = ?", ("2026-06-04",)
    ).fetchone()
    assert row["venue"] == "all"
    assert row["n_bets"] == 2

    # Composite PK holds: a second venue row for the same date does not conflict.
    conn.execute(
        "INSERT INTO tracking_snapshot (snapshot_date, venue, n_bets, created_at) "
        "VALUES (?, ?, ?, ?)",
        ("2026-06-04", "polymarket", 1, "t"),
    )
    conn.commit()
    n = conn.execute(
        "SELECT count(*) AS n FROM tracking_snapshot WHERE snapshot_date = ?", ("2026-06-04",)
    ).fetchone()["n"]
    conn.close()
    assert n == 2


def test_migration_0012_second_apply_is_a_noop():
    """Re-running apply_migrations after 0012 has landed must not error or re-run it."""
    conn = connect(":memory:")
    conn.execute("CREATE TABLE schema_migrations (id TEXT PRIMARY KEY, applied_at TEXT)")
    conn.execute(
        "CREATE TABLE tracking_snapshot ("
        "snapshot_date TEXT PRIMARY KEY, n_bets INTEGER, wins INTEGER, losses INTEGER, "
        "total_pnl REAL, roi REAL, brier REAL, hit_rate REAL, n_scored INTEGER, "
        "created_at TEXT)"
    )
    already_applied = [m for m, _ in _MIGRATIONS if m != "0012_tracking_snapshot_venue"] + [
        "0007_backfill_venue",
        "0010_widen_float4_columns",
        "0013_enable_rls",
    ]
    for mid in already_applied:
        conn.execute("INSERT INTO schema_migrations (id, applied_at) VALUES (?, ?)", (mid, "t"))
    conn.execute(
        "INSERT INTO tracking_snapshot "
        "(snapshot_date, n_bets, wins, losses, total_pnl, roi, brier, hit_rate, "
        "n_scored, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("2026-06-04", 2, 1, 1, 0.3, 0.42, 0.13, 0.5, 2, "t"),
    )
    conn.commit()

    apply_migrations(conn)
    apply_migrations(conn)  # second pass must be a no-op

    n = conn.execute("SELECT count(*) AS n FROM tracking_snapshot").fetchone()["n"]
    row = conn.execute("SELECT * FROM tracking_snapshot").fetchone()
    conn.close()
    assert n == 1
    assert row["venue"] == "all"


def test_migration_0012_records_harmlessly_on_fresh_db():
    """A fresh DB gets the new shape directly from the base schema; 0012 still
    records itself but the rebuild is a no-op over zero rows.
    """
    conn = connect(":memory:")
    init_schema(conn)
    ids = {r["id"] for r in conn.execute("SELECT id FROM schema_migrations").fetchall()}
    assert "0012_tracking_snapshot_venue" in ids

    conn.execute(
        "INSERT INTO tracking_snapshot (snapshot_date, venue, n_bets, created_at) "
        "VALUES (?, ?, ?, ?)",
        ("2026-06-04", "all", 0, "t"),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM tracking_snapshot").fetchone()
    conn.close()
    assert row["venue"] == "all"


def test_backfill_venue_is_idempotent():
    """Running _backfill_venue twice produces the same result."""
    conn = connect(":memory:")
    init_schema(conn)
    conn.execute("INSERT INTO markets (id) VALUES (?)", ("700001",))
    conn.execute("INSERT INTO markets (id) VALUES (?)", ("KXHIGHNY-26JUN08",))
    conn.commit()

    _backfill_venue(conn)
    _backfill_venue(conn)

    rows = {r["id"]: r["venue"] for r in conn.execute("SELECT id, venue FROM markets").fetchall()}
    conn.close()
    assert rows["700001"] == "polymarket"
    assert rows["KXHIGHNY-26JUN08"] == "kalshi"


def test_rls_tables_match_base_schema_tables():
    """_RLS_TABLES must name exactly the tables the base schema creates, so
    every table gets RLS enabled and no drift sneaks in when a table is added.
    """
    # Parse table names out of the shared base schema (works for both backends;
    # the table set is identical, only column types differ).
    table_names = re.findall(r"CREATE TABLE IF NOT EXISTS (\w+) \(", _SQLITE_SCHEMA)
    assert sorted(table_names) == sorted(_RLS_TABLES)


def test_enable_rls_statements_shape():
    """Exact ENABLE ROW LEVEL SECURITY statements, one per table, Postgres syntax.
    Mirrors test_widen_float4_columns_statements_are_exact: pins the emitted SQL
    so a typo or stray policy can't sneak in.
    """
    assert _enable_rls_statements() == [
        "ALTER TABLE runs ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE markets ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE prices ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE forecasts ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE predictions ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE outcomes ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE calibration ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE forecast_accuracy ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE tracking_snapshot ENABLE ROW LEVEL SECURITY",
    ]


def test_migration_0013_records_on_sqlite_without_error():
    """On SQLite (no RLS concept) 0013 records itself as a no-op and is skipped
    on a second pass. Mirrors test_apply_migrations_records_0010_once_on_sqlite.
    """
    conn = connect(":memory:")
    init_schema(conn)
    ids = {r["id"] for r in conn.execute("SELECT id FROM schema_migrations").fetchall()}
    assert "0013_enable_rls" in ids

    apply_migrations(conn)  # second pass: must not re-record

    n = conn.execute(
        "SELECT count(*) AS n FROM schema_migrations WHERE id = ?", ("0013_enable_rls",)
    ).fetchone()["n"]
    conn.close()
    assert n == 1
