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
    # Per-prediction settlement outcome: 1 if the recommended bet won, 0 if lost,
    # NULL if not yet graded. Populated by the settlement grading pass in settle.py
    # so the dashboard reads persisted values instead of re-deriving them in TS.
    ("0006_predictions_won", ["ALTER TABLE predictions ADD COLUMN won INTEGER"]),
    # EMOS calibration: replace RMS spread_scale with affine variance model
    # (var = var_a + var_b * ensemble_var) fit by minimizing mean CRPS.
    (
        "0008_calibration_emos",
        [
            "ALTER TABLE calibration ADD COLUMN var_a REAL",
            "ALTER TABLE calibration ADD COLUMN var_b REAL",
        ],
    ),
    # Probability-calibration columns for forecast_accuracy rows with kind='calibration'.
    # Pooled per (variable, lead) across cities; station is the sentinel 'ALL'.
    # reliability is stored as a JSON TEXT array of ReliabilityBin dicts.
    (
        "0009_forecast_accuracy_calibration",
        [
            "ALTER TABLE forecast_accuracy ADD COLUMN crps REAL",
            "ALTER TABLE forecast_accuracy ADD COLUMN coverage_50 REAL",
            "ALTER TABLE forecast_accuracy ADD COLUMN coverage_80 REAL",
            "ALTER TABLE forecast_accuracy ADD COLUMN coverage_90 REAL",
            "ALTER TABLE forecast_accuracy ADD COLUMN reliability TEXT",
        ],
    ),
]


def _backfill_venue(conn: Conn) -> None:
    """Infer and set venue for markets where venue IS NULL.

    Polymarket market ids are numeric strings (e.g. '700001').
    Kalshi market ids are alphanumeric tickers (e.g. 'KXHIGHNY-26JUN08').
    The inference is str.isdigit() which is portable across SQLite and Postgres;
    no GLOB or regex function is used.

    Idempotent: only rows with venue IS NULL are updated.
    """
    rows = conn.execute("SELECT id FROM markets WHERE venue IS NULL").fetchall()
    for row in rows:
        market_id = str(row["id"])
        venue = "polymarket" if market_id.isdigit() else "kalshi"
        conn.execute("UPDATE markets SET venue = ? WHERE id = ?", (venue, market_id))
    conn.commit()


def _is_duplicate_column(exc: Exception) -> bool:
    """Return True if exc is a 'column already exists' error from either backend."""
    # SQLite raises OperationalError with 'duplicate column name' in the message.
    if isinstance(exc, sqlite3.OperationalError) and "duplicate column name" in str(exc):
        return True
    # Postgres (psycopg) raises an error with sqlstate 42701 (duplicate_column).
    # We check via getattr so this file does not import psycopg at the top level.
    if getattr(exc, "sqlstate", None) == "42701":
        return True
    # No broad string fallback: these two exact signals cover every ADD COLUMN
    # migration, and a looser match would swallow genuine duplicate_table or
    # duplicate_index errors from a future CREATE TABLE/INDEX and falsely record
    # the migration as applied.
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

    # 0007: backfill venue for legacy NULL rows (Python, not SQL, for portability).
    # Polymarket ids are numeric strings; Kalshi ids are alphanumeric tickers.
    if "0007_backfill_venue" not in applied:
        _backfill_venue(conn)
        conn.execute(
            "INSERT INTO schema_migrations (id, applied_at) VALUES (?, ?)",
            ("0007_backfill_venue", datetime.now(UTC).isoformat()),
        )
        conn.commit()
