# MVP 2.0 sub-project 1: Supabase Postgres store + scheduled cloud run

Date: 2026-06-03. Issue: #23. Status: approved design, pre-implementation.

MVP 2.0 (full tracking dashboard) is a program of four sub-projects: this store
+ scheduler foundation, then settlement against actuals, then P&L and
calibration tracking, then the Vercel dashboard. This spec covers only the
foundation. Everything else depends on a shared cloud datastore and data
flowing into it, so it goes first.

## Goal

Run the existing pipeline daily in the cloud, persisting to Supabase Postgres
instead of a local SQLite file, with no change to the forecasting, probability,
ranking, or report logic.

## Why this is small

The store was built to port (see `store/db.py` header). The UPSERTs
(`INSERT ... ON CONFLICT (col) DO UPDATE SET x = excluded.x`) are already valid
Postgres. `conn.execute(sql, params).fetchone()` behaves the same in sqlite3 and
psycopg3. The only real divergences are: the `?` vs `%s` placeholder style, the
`INTEGER PRIMARY KEY` rowids (which become Postgres identity columns),
`executescript` (SQLite-only), and the row factory.

## Decision

Thin dual-backend store. Keep the hand-written SQL and add a small connection
abstraction. Rejected alternatives: SQLAlchemy Core (a large dependency and a
rewrite of working code for six tables, YAGNI) and a Postgres-only switch (slow
networked tests, CI database infrastructure, no offline development).

SQLite stays the default for local development and the entire test suite, so the
suite stays fast and offline. Postgres is used only when `DATABASE_URL` is set.

## Components

### Store abstraction (`store/db.py`)

`connect(dsn)` dispatches on the DSN string: a `postgresql://` or `postgres://`
DSN opens a psycopg3 connection, anything else is treated as a SQLite file path
(unchanged default).

Both backends are wrapped so callers see one interface: an object with
`.execute(sql, params) -> cursor`, `.commit()`, and `.close()`, where the cursor
yields dict-like rows and supports `.fetchone()` / `.fetchall()`. SQLite gets
`row_factory = sqlite3.Row`; psycopg3 gets `row_factory = dict_row`. For the
Postgres backend the wrapper rewrites `?` placeholders to `%s` before executing.
Our SQL contains no `?` inside string literals, so a positional replacement is
safe; the wrapper asserts the count of `?` matches the parameter count as a
guard.

`init_schema(conn)` runs the SQLite DDL (current `executescript`) or, for
Postgres, executes the Postgres DDL. The Postgres DDL is the current schema with
two edits: `id INTEGER PRIMARY KEY` becomes `id BIGINT GENERATED ALWAYS AS
IDENTITY PRIMARY KEY` (tables `prices`, `forecasts`, `predictions`), and the
JSON `TEXT` columns become `jsonb`. The recorder already passes JSON via
`json.dumps`, which Postgres accepts into a `jsonb` column.

`record.py` is unchanged except for the connection type annotation (the wrapper,
not `sqlite3.Connection`); it only writes, using `?` params that the wrapper
translates.

`query.py` needs one real change. `get_run`, `get_predictions`, and
`load_calibration` already read by column name via `dict(row)`, which works for
both `sqlite3.Row` and psycopg3 `dict_row`. But `count_rows` does
`(count,) = ...fetchone()`, which unpacks a dict's keys (not its values) under a
dict-row factory and would break on Postgres. Fix it to be backend-agnostic:
`SELECT count(*) AS n ...` then read `row["n"]`. That works on both backends.
The f-string table name stays (a fixed internal identifier, never user input).

To keep both backends behind one row interface, SQLite uses
`row_factory = sqlite3.Row` (supports `dict(row)` and name access) and psycopg3
uses `dict_row`. No SQL string contains a literal `%`, so the `?`->`%s`
translation cannot collide with a literal.

### Backend selection (`cli.py`)

Both `_run` and `_backfill` resolve the datastore as
`os.environ.get("DATABASE_URL") or args.db`. With no env var set, behaviour is
exactly as today (the `--db` SQLite path). When `DATABASE_URL` is set (the cloud
run), the same code persists to Postgres.

### Scheduled run (`.github/workflows/daily-run.yml`)

A GitHub Actions workflow on a cron schedule (default `0 13 * * *`, 13:00 UTC,
after US markets post; adjustable). Steps: checkout, install uv, `uv sync`, run
`uv run rainmaker run` with `DATABASE_URL` from the `DATABASE_URL` repository
secret, then upload `reports/*.md` and `reports/*.json` as build artifacts.
Postgres is the durable source of truth; the artifacts are for eyeballing a run.
A `workflow_dispatch` trigger is included so the run can be kicked off by hand.

If Polymarket is down the run exits non-zero (current behaviour), which fails the
workflow and surfaces the problem. Individual forecast-source failures degrade
gracefully, as today.

## Testing (TDD)

- Placeholder translation: a unit test that the Postgres wrapper turns
  `"... VALUES (?, ?)"` into `"... VALUES (%s, %s)"` and that a mismatched
  placeholder/param count raises.
- Backend dispatch: `connect("postgresql://...")` selects the Postgres path and
  `connect("x.db")` selects SQLite (assert on the wrapper's backend tag; no live
  connection needed for the dispatch test).
- DDL selection: `init_schema` issues the Postgres DDL for a Postgres connection
  and the SQLite DDL otherwise (assert against a fake/recording connection).
- All existing store, CLI, and golden tests keep running on SQLite, unchanged.
- One integration round-trip test (record a run, read it back) parametrised to
  run against Postgres only when `DATABASE_URL` is set, and skipped otherwise, so
  CI without a database stays green.
- The live Postgres path is verified manually against the real Supabase project
  during implementation (record a run, confirm rows via the Supabase table
  editor), the same way NCEI ids were verified.

## Setup (maintainer)

Create a Supabase project and add its session-pooler connection string as the
`DATABASE_URL` GitHub Actions secret (and to a local `.env` for the manual
verification). No other credentials are needed: Polymarket, NOAA NCEI, and
Open-Meteo stay free and keyless.

## Out of scope (later 2.0 sub-projects)

Settlement against actuals, P&L tracking, calibration backfill in the cloud, and
the Vercel dashboard. Each gets its own spec.

## Risks

- The `?`->`%s` rewrite is positional. Mitigation: the wrapper guards on the
  placeholder/param count, and the integration round-trip test exercises every
  write against real Postgres.
- Supabase requires SSL and may sit behind a connection pooler. Mitigation: use
  the connection string Supabase provides (it carries the right parameters);
  psycopg3 honours them.
- A long cloud run could hit GitHub Actions limits. Mitigation: the run is a few
  minutes for eleven cities; the default job timeout is far higher.
