from rainmaker.store.db import _SQLITE_SCHEMA, connect, init_schema
from rainmaker.store.migrate import _MIGRATIONS, apply_migrations


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
    assert n == 5


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
    # apply 0002-0005 forward normally.
    apply_migrations(conn)

    rows = {r["id"] for r in conn.execute("SELECT id FROM schema_migrations").fetchall()}
    conn.close()
    assert rows == {mid for mid, _ in _MIGRATIONS}
