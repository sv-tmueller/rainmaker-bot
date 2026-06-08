"""Forward schema migrations, tracked so each runs once.

The base schema in db.py is the initial shape; every change since is a migration
here. Both backends accept `ALTER TABLE ... ADD COLUMN`.
"""

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


def apply_migrations(conn: Conn) -> None:
    """Run each not-yet-applied migration once and record it. Idempotent."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (id TEXT PRIMARY KEY, applied_at TEXT)"
    )
    applied = {r["id"] for r in conn.execute("SELECT id FROM schema_migrations").fetchall()}
    for migration_id, statements in _MIGRATIONS:
        if migration_id in applied:
            continue
        for statement in statements:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_migrations (id, applied_at) VALUES (?, ?)",
            (migration_id, datetime.now(UTC).isoformat()),
        )
    conn.commit()
