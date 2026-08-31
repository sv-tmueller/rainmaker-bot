"""Forward schema migrations, tracked so each runs once.

The base schema in db.py is the initial shape; every change since is a migration
here. Both backends accept `ALTER TABLE ... ADD COLUMN`.
"""

import re
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
    # Student-t degrees of freedom for a calibration cell (#291). NULL is a valid
    # Gaussian row, not an absent one: apply_calibration trusts df presence to
    # dispatch family, so a legacy or Gaussian-fit row must stay NULL, not 0.
    ("0011_calibration_df", ["ALTER TABLE calibration ADD COLUMN df REAL"]),
    # Split tracking_snapshot into per-venue rows (all/polymarket/kalshi) so the
    # dashboard can show a venue breakdown. Neither backend supports ALTER TABLE
    # ADD COLUMN ... PRIMARY KEY, so this is a table rebuild: build the new shape
    # alongside the old one, backfill every existing row as venue='all' (the only
    # venue that existed before this migration), then swap. Each statement runs
    # in the migration's own transaction (see apply_migrations), so a crash before
    # the final commit leaves the original table untouched, not half-rebuilt; the
    # leading DROP guards a stale tracking_snapshot_new from an earlier partial
    # attempt within that same uncommitted transaction.
    (
        "0012_tracking_snapshot_venue",
        [
            "DROP TABLE IF EXISTS tracking_snapshot_new",
            "CREATE TABLE tracking_snapshot_new ("
            "snapshot_date TEXT NOT NULL, "
            "venue         TEXT NOT NULL DEFAULT 'all', "
            "n_bets        INTEGER, "
            "wins          INTEGER, "
            "losses        INTEGER, "
            "total_pnl     REAL, "
            "roi           REAL, "
            "brier         REAL, "
            "hit_rate      REAL, "
            "n_scored      INTEGER, "
            "created_at    TEXT, "
            "PRIMARY KEY (snapshot_date, venue))",
            "INSERT INTO tracking_snapshot_new "
            "(snapshot_date, venue, n_bets, wins, losses, total_pnl, roi, brier, "
            "hit_rate, n_scored, created_at) "
            "SELECT snapshot_date, 'all', n_bets, wins, losses, total_pnl, roi, "
            "brier, hit_rate, n_scored, created_at FROM tracking_snapshot",
            "DROP TABLE tracking_snapshot",
            "ALTER TABLE tracking_snapshot_new RENAME TO tracking_snapshot",
        ],
    ),
]

# Tables created by the base schema. Every one gets RLS enabled on Postgres so
# the public anon key cannot read or mutate rows through the PostgREST API.
# No policies are granted: anon and authenticated roles are denied by default,
# while the service-role key and the postgres owner (used by the scheduled run
# via DATABASE_URL) bypass RLS and keep working unchanged. FORCE ROW LEVEL
# SECURITY is deliberately NOT set, so the table owner stays exempt. SQLite has
# no RLS concept and skips this migration (see the 0013 dialect gate below).
_RLS_TABLES: list[str] = [
    "runs",
    "markets",
    "prices",
    "forecasts",
    "predictions",
    "outcomes",
    "calibration",
    "forecast_accuracy",
    "tracking_snapshot",
]


def _enable_rls_statements() -> list[str]:
    """ENABLE ROW LEVEL SECURITY for every table, Postgres-flavored SQL."""
    return [f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY" for t in _RLS_TABLES]


# Corrective ALTERs for the six columns that landed as float4 on Postgres before
# the REAL -> DOUBLE PRECISION substitution existed (0008 var_a/var_b, 0009
# crps/coverage_*). SQLite has no ALTER COLUMN ... TYPE, so these only run on
# Postgres; see the 0010 dialect gate in apply_migrations below.
_WIDEN_FLOAT4_COLUMNS: list[str] = [
    "ALTER TABLE calibration ALTER COLUMN var_a TYPE double precision",
    "ALTER TABLE calibration ALTER COLUMN var_b TYPE double precision",
    "ALTER TABLE forecast_accuracy ALTER COLUMN crps TYPE double precision",
    "ALTER TABLE forecast_accuracy ALTER COLUMN coverage_50 TYPE double precision",
    "ALTER TABLE forecast_accuracy ALTER COLUMN coverage_80 TYPE double precision",
    "ALTER TABLE forecast_accuracy ALTER COLUMN coverage_90 TYPE double precision",
]


def _for_backend(statement: str, backend: str) -> str:
    """Render a migration statement for the target backend.

    Mirrors the REAL -> DOUBLE PRECISION substitution in db.py's base schema:
    SQLite REAL is 8-byte, Postgres REAL is 4-byte float4 and underflows on tiny
    tail probabilities. Without this, every future `... REAL` migration statement
    would silently create a float4 column on Postgres.

    The substitution is textual (matches the word "real" case-insensitively in
    the statement string), not a parsed SQL type.
    """
    if backend == "postgres":
        return re.sub(r"\bREAL\b", "DOUBLE PRECISION", statement, flags=re.IGNORECASE)
    return statement


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
                conn.execute(_for_backend(statement, conn.backend))
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

    # 0010: widen the six columns that landed as float4 on Postgres before the
    # REAL -> DOUBLE PRECISION substitution existed. SQLite has no
    # ALTER COLUMN ... TYPE, so this is dialect-gated (Python, not SQL, like 0007):
    # only Postgres runs the ALTERs; SQLite just records the migration once.
    if "0010_widen_float4_columns" not in applied:
        if conn.backend == "postgres":
            for statement in _WIDEN_FLOAT4_COLUMNS:
                conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_migrations (id, applied_at) VALUES (?, ?)",
            ("0010_widen_float4_columns", datetime.now(UTC).isoformat()),
        )
        conn.commit()

    # 0013: enable Row Level Security on every table (Postgres only). Supabase
    # flags rls_disabled_in_public because, without RLS, the public anon key can
    # read and mutate all rows via PostgREST. Enabling RLS with no policies
    # denies anon/authenticated by default; the service-role key and the postgres
    # owner (scheduled run via DATABASE_URL) bypass RLS and keep working. SQLite
    # has no RLS and records the migration as a no-op. ENABLE ROW LEVEL SECURITY
    # is idempotent, so a crash before the record commits is recovered naturally
    # on the next run (the statement re-enables harmlessly).
    if "0013_enable_rls" not in applied:
        if conn.backend == "postgres":
            for statement in _enable_rls_statements():
                conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_migrations (id, applied_at) VALUES (?, ?)",
            ("0013_enable_rls", datetime.now(UTC).isoformat()),
        )
        conn.commit()
