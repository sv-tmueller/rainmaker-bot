"""Forward schema migrations, tracked so each runs once.

The base schema in db.py is the initial shape; every change since is a migration
here. Both backends accept `ALTER TABLE ... ADD COLUMN`.
"""

import sqlite3
from datetime import UTC, datetime

from rainmaker.store.db import Conn

_MIGRATIONS: list[tuple[str, list[str]]] = [
    ("0001_predictions_bucket", ["ALTER TABLE predictions ADD COLUMN bucket TEXT"]),
    ("0002_predictions_side", ["ALTER TABLE predictions ADD COLUMN side TEXT"]),
    ("0003_prices_side", ["ALTER TABLE prices ADD COLUMN side TEXT"]),
    # The exact settlement-station GHCND, so settlement uses the market's real
    # station (e.g. Kalshi NYC = Central Park) instead of re-deriving it from city.
    ("0004_markets_settlement_ghcnd", ["ALTER TABLE markets ADD COLUMN settlement_ghcnd TEXT"]),
    # The venue a market came from ("polymarket" or "kalshi").
    ("0005_markets_venue", ["ALTER TABLE markets ADD COLUMN venue TEXT"]),
]


def _is_duplicate_column(exc: Exception) -> bool:
    """Return True if exc is a 'column already exists' error from either backend."""
    # SQLite raises OperationalError with 'duplicate column name' in the message.
    if isinstance(exc, sqlite3.OperationalError) and "duplicate column name" in str(exc):
        return True
    # Postgres (psycopg) raises an error with sqlstate 42701 (duplicate_column).
    # We check via getattr so this file does not import psycopg at the top level.
    if getattr(exc, "sqlstate", None) == "42701":
        return True
    # Fallback: some DB drivers surface 'already exists' without a sqlstate.
    if "already exists" in str(exc).lower() or "duplicate column" in str(exc).lower():
        return True
    return False


def apply_migrations(conn: Conn) -> None:
    """Run each not-yet-applied migration once and record it.

    Crash-safe: if a previous run applied an ALTER but crashed before recording
    it in schema_migrations, the duplicate-column error is caught and treated as
    already-applied. Each migration is committed individually so partial state
    is never left unrecorded.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (id TEXT PRIMARY KEY, applied_at TEXT)"
    )
    conn.commit()
    applied = {r["id"] for r in conn.execute("SELECT id FROM schema_migrations").fetchall()}
    for migration_id, statements in _MIGRATIONS:
        if migration_id in applied:
            continue
        for statement in statements:
            try:
                conn.execute(statement)
            except Exception as exc:
                if _is_duplicate_column(exc):
                    # Column already exists from a previous crashed run.
                    # On Postgres, a failed statement aborts the transaction;
                    # roll back before the INSERT so it can proceed.
                    conn.rollback()
                else:
                    raise
        conn.execute(
            "INSERT INTO schema_migrations (id, applied_at) VALUES (?, ?)",
            (migration_id, datetime.now(UTC).isoformat()),
        )
        conn.commit()
