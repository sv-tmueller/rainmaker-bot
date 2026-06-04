# MVP 2.0 sub-project 3: P&L and calibration tracking

Date: 2026-06-04. Issue: #29. Status: approved design, pre-implementation.

Third sub-project of MVP 2.0. Sub-projects 1 (Supabase store + scheduler) and 2
(settlement) are done. This one scores the bot's recommendations and forecasts
against settled outcomes: hypothetical P&L and forecast calibration over time.

## Prerequisite: predictions must record their bucket

P&L is about which recommended bucket we would have bet and whether it won. The
persisted `predictions` table records `p_win`, `edge`, and `recommended` per
bucket but no bucket identifier, so a prediction row cannot be joined to its ask
(in `prices`, keyed by bucket) or scored against the settled value. Fixing this
means adding a column to an existing, prod-populated table, which is the schema
change the earlier sub-projects deferred. So this sub-project also builds the
migration mechanism.

## Decisions

- **Migration mechanism: a small versioned runner.** A `schema_migrations`
  table records applied migration ids; `apply_migrations(conn)` runs each
  unapplied migration once and records it. The base schema in `db.py` is left
  as-is; from now on, schema changes are migrations, not edits to the base DDL.
  Rejected: Alembic (too heavy here) and ad-hoc `ADD COLUMN IF NOT EXISTS`
  (SQLite does not support it).
- **P&L and calibration are computed on read** from `predictions` + `prices` +
  `outcomes`. No new metrics tables (the dashboard can query the derivation).
- **Hypothetical P&L, flat one-unit stake.** Assume we took every recommended
  bet at the listed ask, one unit each. Measures the bot's own edge and needs no
  input from the operator.

## Components

### Migration mechanism (`src/rainmaker/store/migrate.py`)

- `_MIGRATIONS`: an ordered list of `(id, [sql, ...])`. First entry:
  `("0001_predictions_bucket", ["ALTER TABLE predictions ADD COLUMN bucket TEXT"])`.
  `ALTER TABLE ... ADD COLUMN` is valid on both SQLite and Postgres.
- `apply_migrations(conn)`: create `schema_migrations(id TEXT PRIMARY KEY,
  applied_at TEXT)` if absent, read the applied ids, run each unapplied
  migration's statements, and insert its id. Idempotent: re-running applies
  nothing.
- `init_schema(conn)` calls `apply_migrations(conn)` after creating the base
  tables, so every entry point (run, settle, backfill, tests) upgrades the
  schema automatically. The base schema never gains the `bucket` column; the
  migration owns it, which keeps fresh and existing databases on the same path.

### Record the bucket (`store/record.py`)

`_record_predictions` writes `o.bucket_label` into the new `bucket` column.
Historical prod rows keep `bucket = NULL` and are excluded from per-bucket
scoring, so tracking accrues from when the column lands. The `INSERT` column
list and tuple gain `bucket`.

### Hypothetical P&L (`src/rainmaker/tracking.py`)

For each settled market (it has an `outcomes` row), take its recommended
predictions (`recommended = 1` and `bucket IS NOT NULL`). For each:

- ask = `prices.price` for the same `(run_id, market_id, bucket = outcome)`.
- won = the settled value falls in the bucket. Re-parse the bucket label with
  `parse_bucket_label` (kind, lo, hi, threshold) and test containment of the
  actual rounded to the nearest whole degree: range `lo <= v <= hi`, below
  `v <= threshold`, above `v >= threshold`.
- `pnl = (1 - ask)` if won else `-ask` (one-unit stake).

Aggregate to total P&L, win/loss record, and ROI (`total_pnl / total_staked`,
where each bet stakes `ask`). Each recommended prediction row is one one-unit
bet, so a market re-recommended across daily runs contributes several bets at
different asks (this measures "bet one unit every time the bot recommended";
deduping to one bet per market is a possible later refinement). The by-date time
series is deferred to the dashboard (sub-project 4).

### Calibration accuracy (`src/rainmaker/tracking.py`)

Over all settled bucket-predictions (`bucket IS NOT NULL`, market settled):

- Brier score: `mean((p_win - won) ** 2)`, where `won` is 1 if that bucket
  contains the settled value else 0. Lower is better.
- Recommended-bet hit rate: of recommended bets, the fraction that won.

### CLI (`src/rainmaker/cli.py`)

`rainmaker track [--db]`: resolve the datastore like the other commands,
connect, and print the P&L summary (total, record, ROI) and calibration (Brier,
hit rate) over all settled data. Read-only; works on SQLite and Postgres.

## Data flow

`predictions` (recommended, bucket, p_win) joined with `prices` (ask per bucket)
and `outcomes` (actual_value) and `markets` (for the settled markets) yields,
per recommended bet, an ask and a win/loss, hence P&L; the same join over all
buckets yields the Brier score. Everything reads the existing store.

## Testing (TDD)

- Migration: `apply_migrations` adds the `bucket` column to `predictions`; a
  second call is a no-op (idempotent); `schema_migrations` records `0001`.
- Recording: a recorded run stores `bucket` on each prediction row.
- P&L: synthetic settled market with one winning and one losing recommended bet;
  assert per-bet pnl (`1 - ask` and `-ask`), total, record, and ROI.
- Calibration: known p_win values and outcomes give a hand-computed Brier score
  and hit rate.
- CLI: `rainmaker track` prints the summary lines.
- All tests run on SQLite; no live calls.

## Risks

- Historical predictions (already in prod) have `bucket = NULL` and cannot be
  scored. Acceptable: tracking starts accumulating once the column lands; only a
  few days of early data are unscored.
- The win check rounds the NCEI actual to the nearest whole degree before testing
  containment, matching the market's whole-degree resolution. Documented.

## Out of scope (sub-project 4)

The Vercel dashboard (a read-only UI over these derivations and the settled data).
