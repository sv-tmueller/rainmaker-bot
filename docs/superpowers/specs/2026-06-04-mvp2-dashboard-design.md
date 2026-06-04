# MVP 2.0 sub-project 4: read-only dashboard

Date: 2026-06-04. Issue: #31. Status: approved design, pre-implementation.

Final sub-project of MVP 2.0. Sub-projects 1-3 (store + scheduler, settlement,
tracking) are done. This adds a read-only web dashboard showing today's
recommended bets and the bot's performance, deployed to Vercel and reading
Supabase.

## Goal

See the bot's daily recommendations and its running P&L / calibration in a
browser, without running the CLI.

## Decisions

- **Metrics source: Python writes a daily snapshot.** The derived metrics (P&L,
  calibration) are computed by the existing `tracking.py` and written to a
  `tracking_snapshot` table; the dashboard reads that row. Python stays the
  single source of truth; the dashboard is a thin reader and never reimplements
  the scoring logic.
- **Auth: Cloudflare Access at the edge.** The deployment sits behind Cloudflare
  Access (the same setup that fronts the operator's other Vercel project). The
  app has no auth code and treats requests as already authenticated.
- **Data access: server-side reads with the Supabase service-role key.** Next.js
  server components query Supabase with the service key held only in Vercel env;
  it never reaches the browser. No Supabase RLS policies are needed.
- **Repo: a `dashboard/` subdir.** The Next.js app lives in `dashboard/` in this
  repo; Vercel's root directory is set to `dashboard/`.

## Backend (Python)

### `tracking_snapshot` table

A new table added to the base schema (`db.py`). Because it is new,
`CREATE TABLE IF NOT EXISTS` creates it on both fresh and existing databases on
the next `init_schema`, so no migration is required (migrations remain only for
altering existing tables).

```
tracking_snapshot (
    snapshot_date TEXT PRIMARY KEY,   -- the UTC date the snapshot was taken
    n_bets        INTEGER,
    wins          INTEGER,
    losses        INTEGER,
    total_pnl     REAL,               -- DOUBLE PRECISION on Postgres, per the db rules
    roi           REAL,
    brier         REAL,               -- nullable (no settled data yet)
    hit_rate      REAL,               -- nullable
    n_scored      INTEGER,            -- settled bucket-predictions scored (calibration sample)
    created_at    TEXT
)
```

The columns map directly to the two existing functions: `n_bets`, `wins`,
`losses`, `total_pnl`, `roi` from `compute_pnl`; `brier`, `hit_rate`, and
`n_scored` (its `n`) from `compute_calibration`.

### `write_snapshot(conn, on_date, created_at)` in `tracking.py`

Compute `compute_pnl(conn)` and `compute_calibration(conn)` and upsert one row
keyed by `snapshot_date = on_date`. Re-running on the same day overwrites that
day's row (idempotent). `brier` / `hit_rate` are stored as-is, including null
when nothing is settled.

### `rainmaker snapshot` CLI + workflow step

A `snapshot` subcommand resolves the datastore like the other commands and calls
`write_snapshot`. The daily workflow runs it as a step after `settle`, so each
day's metrics are persisted for the dashboard. Local dev (SQLite) works the same.

## Frontend (`dashboard/`)

Next.js (App Router) + TypeScript + Tailwind, deployed to Vercel with root
directory `dashboard/`. A single page rendered server-side, two panels:

1. **Today's bets.** Read the latest run's recommended predictions joined to
   `prices` (ask) and `markets` (title, settlement date), ordered by edge. This
   mirrors the report's "Recommended bets" view. If there are none, say so.
2. **Performance.** Read the most recent `tracking_snapshot` row and show P&L
   (total, record, ROI), Brier, and recommended hit rate. Show "no settled data
   yet" when the metrics are null.

Data access: a small server-only Supabase client built from `SUPABASE_URL` and
`SUPABASE_SERVICE_ROLE_KEY` (Vercel env). All queries run in server components;
nothing sensitive reaches the browser. The page is dynamic (no caching of live
data) or revalidates on a short interval.

## Data flow

Daily workflow: `run` -> `settle` -> `snapshot` writes `tracking_snapshot`. The
dashboard reads `predictions` + `prices` + `markets` (today's bets) and
`tracking_snapshot` (performance) from Supabase, server-side, behind Cloudflare
Access.

## Testing

- Python: `write_snapshot` is TDD'd on SQLite (seed predictions/prices/outcomes,
  write a snapshot, read the row back and assert the stored metrics); the
  `snapshot` CLI prints/persists a summary; an empty store writes a zeroed row
  with null brier/hit_rate.
- Frontend: verified by `next build` (type-checks and compiles) and a manual
  check against Supabase. A read-only MVP page does not get component tests.

## Setup (operator)

- Create the Vercel project, root directory `dashboard/`.
- Set `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` in Vercel env.
- Put Cloudflare Access in front of the dashboard hostname (policy + DNS), as on
  the existing Vercel project.

## Risks

- The service-role key bypasses RLS, so it must stay server-only (Vercel env,
  server components). The design keeps all queries server-side; the key is never
  referenced in client code.
- Early on the dashboard shows few or zero settled results (settlement is still
  catching up); the panels handle the empty/null case explicitly.

## Out of scope

Charts and time-series history (the snapshot table makes them possible later),
per-user accounts, and any write/interaction from the dashboard (it is read-only;
trading stays manual, automated trading is MVP 3.0).
