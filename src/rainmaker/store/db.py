"""Datastore: SQLite by default, Supabase Postgres when given a postgres DSN.

A thin `Conn` wrapper gives both backends one interface (execute/commit/close
with name-keyed rows). Portability rules (see CLAUDE.md / spec data model):
- JSON columns are TEXT on both backends (jsonb is deferred; the recorder writes
  json.dumps strings, which Postgres will not implicitly cast into jsonb).
- Timestamps are ISO-8601 UTC TEXT; booleans are INTEGER 0/1.
- Floating-point columns are REAL in SQLite (8-byte) and DOUBLE PRECISION in
  Postgres (its REAL is 4-byte float4 and underflows on tiny tail probabilities).
- The three surrogate INTEGER PRIMARY KEY columns are SQLite rowids; in Postgres
  they are identity columns. The shared SQL uses no SQLite-only features.
"""

import sqlite3
from collections.abc import Sequence
from typing import Any

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id            TEXT PRIMARY KEY,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        TEXT NOT NULL,
    coverage      TEXT
);

CREATE TABLE IF NOT EXISTS markets (
    id                TEXT PRIMARY KEY,
    slug              TEXT,
    title             TEXT,
    city              TEXT,
    variable          TEXT,
    resolution_source TEXT,
    settlement_date   TEXT,
    outcome_spec      TEXT,
    raw               TEXT,
    captured_at       TEXT
);

CREATE TABLE IF NOT EXISTS prices (
    id           INTEGER PRIMARY KEY,
    run_id       TEXT REFERENCES runs(id),
    market_id    TEXT REFERENCES markets(id),
    outcome      TEXT,
    price        REAL,
    implied_prob REAL,
    captured_at  TEXT
);

CREATE TABLE IF NOT EXISTS forecasts (
    id          INTEGER PRIMARY KEY,
    run_id      TEXT REFERENCES runs(id),
    market_id   TEXT REFERENCES markets(id),
    source      TEXT,
    model       TEXT,
    variable    TEXT,
    values_json TEXT,
    lead_time   INTEGER,
    fetched_at  TEXT
);

CREATE TABLE IF NOT EXISTS predictions (
    id          INTEGER PRIMARY KEY,
    run_id      TEXT REFERENCES runs(id),
    market_id   TEXT REFERENCES markets(id),
    p_win       REAL,
    confidence  REAL,
    dist_params TEXT,
    edge        REAL,
    recommended INTEGER,
    created_at  TEXT
);

CREATE TABLE IF NOT EXISTS outcomes (
    market_id    TEXT PRIMARY KEY REFERENCES markets(id),
    actual_value REAL,
    won          INTEGER,
    settled_at   TEXT
);

CREATE TABLE IF NOT EXISTS calibration (
    station      TEXT NOT NULL,
    variable     TEXT NOT NULL,
    lead_time    INTEGER NOT NULL,
    bias         REAL,
    spread_scale REAL,
    n_samples    INTEGER,
    updated_at   TEXT,
    PRIMARY KEY (station, variable, lead_time)
);

CREATE TABLE IF NOT EXISTS forecast_accuracy (
    station    TEXT NOT NULL,
    city       TEXT,
    variable   TEXT NOT NULL,
    lead_time  INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    n          INTEGER,
    mae_f      REAL,
    bias_f     REAL,
    updated_at TEXT,
    PRIMARY KEY (station, variable, lead_time, kind)
);

CREATE TABLE IF NOT EXISTS tracking_snapshot (
    snapshot_date TEXT PRIMARY KEY,
    n_bets        INTEGER,
    wins          INTEGER,
    losses        INTEGER,
    total_pnl     REAL,
    roi           REAL,
    brier         REAL,
    hit_rate      REAL,
    n_scored      INTEGER,
    created_at    TEXT
);
"""

# The SQLite schema with two backend differences:
# - the three surrogate keys become identity columns (a plain Postgres INTEGER
#   PRIMARY KEY does not auto-generate);
# - REAL becomes DOUBLE PRECISION. SQLite REAL is 8-byte (double); Postgres REAL
#   is 4-byte float4 and underflows on the tiny tail-bucket probabilities the
#   engine produces, so we match SQLite's precision with float8.
_POSTGRES_SCHEMA = (
    _SQLITE_SCHEMA.replace(
        "id           INTEGER PRIMARY KEY,",
        "id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,",
    )
    .replace(
        "id          INTEGER PRIMARY KEY,",
        "id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,",
    )
    .replace(" REAL,", " DOUBLE PRECISION,")
)


def _backend_for(dsn: str) -> str:
    return "postgres" if dsn.startswith(("postgres://", "postgresql://")) else "sqlite"


def _schema_for(backend: str) -> str:
    return _POSTGRES_SCHEMA if backend == "postgres" else _SQLITE_SCHEMA


def _translate(sql: str, n_params: int) -> str:
    """Rewrite SQLite '?' placeholders to Postgres '%s', guarding the count."""
    if sql.count("?") != n_params:
        raise ValueError(f"placeholder/param mismatch: {sql.count('?')} != {n_params}")
    return sql.replace("?", "%s")


class Conn:
    """Uniform wrapper over a sqlite3 or psycopg connection.

    Callers use .execute(sql, params), .commit(), .close(); rows come back keyed
    by column name on both backends. For Postgres, '?' placeholders become '%s'.
    """

    def __init__(self, raw: Any, backend: str) -> None:
        self._raw = raw
        self.backend = backend

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        if self.backend == "postgres":
            sql = _translate(sql, len(params))
        if params:
            return self._raw.execute(sql, params)
        return self._raw.execute(sql)

    def commit(self) -> None:
        self._raw.commit()

    def rollback(self) -> None:
        self._raw.rollback()

    def close(self) -> None:
        self._raw.close()


def connect(dsn: str) -> Conn:
    """Open a datastore connection. A postgres DSN uses Postgres; else a SQLite file."""
    if _backend_for(dsn) == "postgres":
        import psycopg
        from psycopg.rows import dict_row

        return Conn(psycopg.connect(dsn, row_factory=dict_row), "postgres")
    raw = sqlite3.connect(dsn)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA foreign_keys = ON")
    return Conn(raw, "sqlite")


def init_schema(conn: Conn) -> None:
    """Create every table if absent, then apply forward migrations. Idempotent."""
    for statement in _schema_for(conn.backend).split(";"):
        if statement.strip():
            conn.execute(statement)
    conn.commit()
    # Imported here (not at module top) to avoid a db <-> migrate import cycle.
    from rainmaker.store.migrate import apply_migrations

    apply_migrations(conn)
