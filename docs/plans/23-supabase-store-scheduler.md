# Supabase Postgres store + scheduled cloud run Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the existing pipeline daily in the cloud, persisting to Supabase Postgres when `DATABASE_URL` is set, with SQLite unchanged as the local/test default and no change to forecasting, probability, ranking, or report logic.

**Architecture:** A thin connection wrapper (`Conn`) sits over either sqlite3 or psycopg3. `connect(dsn)` dispatches on the DSN; the wrapper gives both backends a uniform `.execute()/.commit()/.close()` with name-keyed rows and rewrites `?` to `%s` for Postgres. The schema is identical across backends except three surrogate keys become Postgres identity columns. A GitHub Actions cron runs the CLI against Supabase.

**Tech Stack:** Python 3.11+, sqlite3 (stdlib), psycopg3, httpx, pydantic, pytest, GitHub Actions.

---

## Task 1: Add the psycopg dependency

**Files:**
- Modify: `pyproject.toml`, `uv.lock` (via `uv add`)

- [ ] **Step 1: Add psycopg**

Run: `uv add "psycopg[binary]"`
Expected: psycopg is added to `[project].dependencies` in `pyproject.toml` and `uv.lock` updates.

- [ ] **Step 2: Confirm it imports**

Run: `uv run python -c "import psycopg; from psycopg.rows import dict_row; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "build: add psycopg for the Postgres store backend"
```

---

## Task 2: Dual-backend store

**Files:**
- Modify: `src/rainmaker/store/db.py` (rewrite)
- Modify: `src/rainmaker/store/query.py` (annotations + `count_rows` fix)
- Modify: `src/rainmaker/store/record.py` (annotations)
- Test: `tests/test_store_db.py`

- [ ] **Step 1: Write failing tests for the pure helpers**

Add to the top imports of `tests/test_store_db.py` (it currently imports only `sqlite3` and `connect, init_schema`):

```python
import pytest

from rainmaker.store.db import _backend_for, _schema_for, _translate
```

Append these tests to `tests/test_store_db.py`:

```python
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
    # all three surrogate-key tables (prices, forecasts, predictions) must get an
    # identity column on Postgres; a partial .replace would break inserts at runtime
    assert _schema_for("postgres").count("GENERATED ALWAYS AS IDENTITY") == 3
    assert "GENERATED ALWAYS AS IDENTITY" not in _schema_for("sqlite")
    assert _schema_for("sqlite").count("INTEGER PRIMARY KEY") == 3
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run pytest tests/test_store_db.py -q`
Expected: FAIL on import (`cannot import name '_backend_for'`).

- [ ] **Step 3: Rewrite `src/rainmaker/store/db.py`**

Replace the entire file with:

```python
"""Datastore: SQLite by default, Supabase Postgres when given a postgres DSN.

A thin `Conn` wrapper gives both backends one interface (execute/commit/close
with name-keyed rows). Portability rules (see CLAUDE.md / spec data model):
- JSON columns are TEXT on both backends (jsonb is deferred; the recorder writes
  json.dumps strings, which Postgres will not implicitly cast into jsonb).
- Timestamps are ISO-8601 UTC TEXT; booleans are INTEGER 0/1.
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
"""

# Identical to the SQLite schema except the three surrogate keys, which must be
# identity columns in Postgres (a plain INTEGER PRIMARY KEY does not auto-generate).
_POSTGRES_SCHEMA = _SQLITE_SCHEMA.replace(
    "id           INTEGER PRIMARY KEY,",
    "id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,",
).replace(
    "id          INTEGER PRIMARY KEY,",
    "id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,",
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
    """Create every table if absent. Idempotent. Backend-appropriate DDL."""
    for statement in _schema_for(conn.backend).split(";"):
        if statement.strip():
            conn.execute(statement)
    conn.commit()
```

- [ ] **Step 4: Update `src/rainmaker/store/query.py`**

Replace its `import sqlite3` line with:

```python
from rainmaker.store.db import Conn
```

Change every `conn: sqlite3.Connection` annotation to `conn: Conn` (four functions: `get_run`, `get_predictions`, `count_rows`, `load_calibration`). Then replace the body of `count_rows` with the dict-row-safe version:

```python
def count_rows(conn: Conn, table: str) -> int:
    # table is a fixed internal identifier, never user input.
    row = conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()
    return int(row["n"])
```

- [ ] **Step 5: Update `src/rainmaker/store/record.py` annotations**

Replace its `import sqlite3` line with:

```python
from rainmaker.store.db import Conn
```

Change every `conn: sqlite3.Connection` annotation to `conn: Conn` (in `record_run`, `_record_market`, `_record_prices`, `_record_forecasts`, `_record_predictions`, `save_calibration`). No other changes; the SQL and `?` params are untouched.

- [ ] **Step 6: Settle imports, then run the store and CLI tests**

Run: `uv run ruff check --fix . && uv run ruff format . && uv run pytest tests/test_store_db.py tests/test_store_record.py tests/test_cli.py -q`
Expected: ruff sorts the new imports (the `import sqlite3` removals and the `Conn` imports land in the right groups), formatting is clean, and tests PASS. Existing SQLite tests still pass through the wrapper; the new helper tests pass.

- [ ] **Step 7: Type check**

Run: `uv run mypy src`
Expected: `Success: no issues found`.

- [ ] **Step 8: Commit**

```bash
git add src/rainmaker/store/db.py src/rainmaker/store/query.py src/rainmaker/store/record.py tests/test_store_db.py
git commit -m "feat: dual-backend store (SQLite default, Postgres via DSN)"
```

---

## Task 3: Select the backend from the environment

**Files:**
- Modify: `src/rainmaker/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cli.py`:

```python
def test_datastore_prefers_database_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    assert cli._datastore("local.db") == "postgresql://x/y"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert cli._datastore("local.db") == "local.db"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_cli.py::test_datastore_prefers_database_url -q`
Expected: FAIL (`module 'rainmaker.cli' has no attribute '_datastore'`).

- [ ] **Step 3: Add the env import and helper**

In `src/rainmaker/cli.py`, add `import os` to the stdlib imports (top of file, before `import sys`). Add this helper next to the other module-level helpers (for example after `_new_run_id`):

```python
def _datastore(default: str) -> str:
    """Use the Postgres DSN from the environment when set, else the SQLite path."""
    return os.environ.get("DATABASE_URL") or default
```

- [ ] **Step 4: Use it in `main` and guard the local mkdir**

In `main`, change the dispatch to resolve the datastore once:

```python
    db = _datastore(args.db)
    if args.command == "run":
        _run(args.reports_dir, db)
    elif args.command == "backfill":
        _backfill(args.city, args.variable, args.days, args.lead, db)
```

In both `_run` and `_backfill`, replace the line `Path(db_path).parent.mkdir(parents=True, exist_ok=True)` with:

```python
    if "://" not in db_path:  # a Postgres DSN has no local parent dir to create
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
```

- [ ] **Step 5: Run CLI tests and type check**

Run: `uv run pytest tests/test_cli.py -q && uv run mypy src`
Expected: PASS and `Success`. Existing CLI tests do not set `DATABASE_URL`, so they keep using the SQLite `--db` path unchanged.

- [ ] **Step 6: Commit**

```bash
git add src/rainmaker/cli.py tests/test_cli.py
git commit -m "feat: select Postgres via DATABASE_URL, else the local SQLite path"
```

---

## Task 4: GitHub Actions daily run

**Files:**
- Create: `.github/workflows/daily-run.yml`

- [ ] **Step 1: Create the workflow**

Create `.github/workflows/daily-run.yml` with exactly:

```yaml
name: daily-run
on:
  schedule:
    - cron: "0 13 * * *"  # 13:00 UTC daily, after US markets post
  workflow_dispatch: {}
jobs:
  run:
    runs-on: ubuntu-latest
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@v4
      - name: Install uv
        uses: astral-sh/setup-uv@v5
      - name: Sync dependencies
        run: uv sync
      - name: Run the bot
        env:
          DATABASE_URL: ${{ secrets.DATABASE_URL }}
        run: uv run rainmaker run
      - name: Upload report artifacts
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: daily-report
          path: reports/
          if-no-files-found: ignore
```

- [ ] **Step 2: Validate the YAML parses**

Run: `uv run python -c "import yaml; yaml.safe_load(open('.github/workflows/daily-run.yml')); print('ok')"`
Expected: prints `ok`. (If PyYAML is not present, this check can be skipped; GitHub validates on push.)

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/daily-run.yml
git commit -m "ci: daily scheduled run writing to the cloud datastore"
```

---

## Task 5: Postgres integration round-trip (auto-skipped)

**Files:**
- Create: `tests/test_store_postgres.py`

- [ ] **Step 1: Write the integration test**

Create `tests/test_store_postgres.py` with:

```python
import os

import pytest

from rainmaker.store.db import connect, init_schema
from rainmaker.store.query import count_rows, get_run
from rainmaker.store.record import record_run

DSN = os.environ.get("DATABASE_URL")


@pytest.mark.skipif(not DSN, reason="DATABASE_URL not set; Postgres integration skipped")
def test_postgres_round_trip():
    conn = connect(DSN)
    try:
        init_schema(conn)
        conn.execute("DELETE FROM runs WHERE id = ?", ("it-roundtrip",))
        conn.commit()
        record_run(
            conn,
            run_id="it-roundtrip",
            started_at="2026-06-03T00:00:00Z",
            finished_at="2026-06-03T00:01:00Z",
            status="ok",
            evaluated=[],
        )
        run = get_run(conn, "it-roundtrip")
        assert run is not None and run["status"] == "ok"
        assert count_rows(conn, "runs") >= 1
        conn.execute("DELETE FROM runs WHERE id = ?", ("it-roundtrip",))
        conn.commit()
    finally:
        conn.close()
```

- [ ] **Step 2: Run it (skips without a DB)**

Run: `uv run pytest tests/test_store_postgres.py -q`
Expected: `1 skipped` (no `DATABASE_URL` locally).

- [ ] **Step 3: Commit**

```bash
git add tests/test_store_postgres.py
git commit -m "test: Postgres round-trip integration test (skips without DATABASE_URL)"
```

---

## Task 6: Full verification, manual Postgres check, finalize

**Files:** none (verification only)

- [ ] **Step 1: Full check suite**

Run:
```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest -q
```
Expected: ruff clean, format clean, mypy `Success`, all tests pass (the Postgres integration test shows as skipped).

- [ ] **Step 2: Manual Postgres verification (needs the maintainer's Supabase DSN)**

With the Supabase session-pooler connection string exported as `DATABASE_URL`:
```bash
export DATABASE_URL="postgresql://...supabase..."
uv run pytest tests/test_store_postgres.py -q          # now runs, expect 1 passed
uv run rainmaker run                                   # persists today's run to Postgres
```
Then confirm in the Supabase table editor that `runs` has a new row and `predictions` has rows. This is a manual gate, not a committed test.

- [ ] **Step 3: Add the secret and mark the PR ready**

In the GitHub repo settings, add the `DATABASE_URL` secret (the same Supabase connection string). Then:
```bash
git push
gh pr ready 24
```

---

## Notes

- SQLite stays the default everywhere `DATABASE_URL` is unset, so local dev and the full offline test suite are unchanged.
- `jsonb` is deferred: JSON columns are TEXT on both backends until a later sub-project queries inside the JSON.
- Out of scope (later 2.0 sub-projects): settlement against actuals, P&L, calibration backfill in the cloud, the Vercel dashboard.
```
