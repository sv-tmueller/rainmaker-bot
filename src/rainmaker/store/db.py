"""SQLite store, designed to port to Supabase Postgres.

Portability rules (see CLAUDE.md / spec data model):
- JSON columns are TEXT here and map to jsonb in Postgres.
- Timestamps are ISO-8601 UTC TEXT; booleans are INTEGER 0/1.
- Surrogate `INTEGER PRIMARY KEY` columns are SQLite rowids; on the Postgres port
  they become identity/serial columns. No SQLite-only features (no JSON1
  functions, no AUTOINCREMENT) are used.
"""

import sqlite3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id            TEXT PRIMARY KEY,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        TEXT NOT NULL,
    coverage      TEXT          -- json
);

CREATE TABLE IF NOT EXISTS markets (
    id                TEXT PRIMARY KEY,   -- polymarket id
    slug              TEXT,
    title             TEXT,
    city              TEXT,
    variable          TEXT,
    resolution_source TEXT,
    settlement_date   TEXT,
    outcome_spec      TEXT,               -- json
    raw               TEXT,               -- json
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
    values_json TEXT,              -- json: sample values / ensemble members
    lead_time   INTEGER,
    fetched_at  TEXT
);

CREATE TABLE IF NOT EXISTS predictions (
    id          INTEGER PRIMARY KEY,
    run_id      TEXT REFERENCES runs(id),
    market_id   TEXT REFERENCES markets(id),
    p_win       REAL,
    confidence  REAL,
    dist_params TEXT,              -- json
    edge        REAL,
    recommended INTEGER,           -- bool 0/1
    created_at  TEXT
);

CREATE TABLE IF NOT EXISTS outcomes (
    market_id    TEXT PRIMARY KEY REFERENCES markets(id),
    actual_value REAL,
    won          INTEGER,          -- bool 0/1
    settled_at   TEXT              -- filled in MVP 2.0
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
"""


def connect(path: str) -> sqlite3.Connection:
    """Open a connection with row access by name and foreign keys enforced."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create every table if absent. Idempotent."""
    conn.executescript(_SCHEMA)
    conn.commit()
