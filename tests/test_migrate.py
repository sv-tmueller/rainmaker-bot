import sqlite3

from rainmaker.store.db import _SQLITE_SCHEMA, connect, init_schema
from rainmaker.store.migrate import (
    _MIGRATIONS,
    _backfill_venue,
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
    # _MIGRATIONS holds DDL steps; 0007_backfill_venue is recorded as a separate
    # Python step outside that list, so the total count is len(_MIGRATIONS) + 1.
    assert n == len(_MIGRATIONS) + 1


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
    # _MIGRATIONS holds DDL steps; 0007_backfill_venue is recorded outside that list.
    assert rows == {mid for mid, _ in _MIGRATIONS} | {"0007_backfill_venue"}


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
