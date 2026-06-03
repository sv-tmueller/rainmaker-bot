# MVP 2.0 sub-project 2: settle markets against NOAA actuals

Date: 2026-06-03. Issue: #27. Status: approved design, pre-implementation.

Second sub-project of MVP 2.0 (tracking dashboard). Sub-project 1 (Supabase store
+ scheduled cloud run) is done. This one settles recorded markets against actuals
and writes the result to the `outcomes` table, so sub-project 3 can compute P&L
and calibration accuracy over time.

## Goal

For each recorded market whose settlement date has passed, determine the actual
settled temperature and record it, so we can later score our forecasts and bets
against reality.

## Decision: settle against NOAA NCEI, as a documented proxy

Markets truly resolve on Weather Underground's reading of a named airport
station. Weather Underground has no free API, so settling against it would mean
scraping (fragile, a ToS gray area, and a new source needing explicit sign-off).
Instead we settle against NOAA NCEI daily extremes: free, already wired into the
backfill, and reading the same airport GHCND stations we forecast. NOAA's daily
extreme can differ slightly from Weather Underground's reading, so settlement is
a documented proxy, not the exact market resolution. For an advisory bot that
tracks forecast accuracy and hypothetical P&L, the proxy is sound. Quantifying
the NOAA-vs-WU gap is a possible later study, out of scope here.

## No schema change

Settlement populates the existing `outcomes` table: `actual_value` (the settled
whole-degree-F temperature) and `settled_at`. The winning bucket is derivable
from `actual_value` plus the market's buckets, so it is not stored. The `won`
column (did our bet win) is a P&L concept and is left for sub-project 3. Because
nothing alters an existing table, no migration mechanism is needed here (the
broader migration gap stays open for a future schema-changing change).

## Components

### `src/rainmaker/settle.py`

- `unsettled_markets(conn, before)`: recorded markets with `settlement_date <
  before` and no `outcomes` row. Returns the market id, city, variable, and
  settlement date from the `markets` table.
- `run_settlement(conn, client, today)`: for each unsettled market, resolve the
  station's GHCND id from the recorded city via `STATIONS`, fetch the NOAA daily
  extreme for that date and variable, and record the outcome when available.
  When NCEI does not yet have the date (it lags a day or two), the market is left
  unsettled and picked up on a later run. The job is idempotent: re-running never
  duplicates or overwrites a settled row. A recorded city absent from `STATIONS`
  is skipped with a warning rather than aborting the run.

### Actuals fetch (reuse and extend `backfill.fetch_actuals`)

`fetch_actuals` is TMAX-only today. Make it variable-aware so it fetches TMAX or
TMIN, and settlement and backfill share one NCEI fetch. Only TMAX markets are
recorded right now (the `run` command gates to TMAX), so TMIN settlement simply
rides along for when that Phase 5 slice lands.

### Store helpers (`store/record.py`, `store/query.py`)

- `record_outcome(conn, market_id, actual_value, settled_at)`: UPSERT into
  `outcomes` keyed by `market_id`.
- `unsettled_markets(conn, before)` query: the `markets` left-join-`outcomes`
  read above. Works on SQLite and Postgres through the `Conn` wrapper.

### CLI and workflow

- `rainmaker settle [--db]`: resolves the datastore the same way `run` does
  (`DATABASE_URL` env, else the SQLite default), connects, settles, prints a
  short summary (how many settled, how many still waiting on data).
- `.github/workflows/daily-run.yml`: add a `settle` step after the `run` step,
  same `DATABASE_URL` secret, so prod settles daily.

## Timing

A market can only settle after its date has passed and NCEI has published the
daily summary, which lags roughly one to two days. The catch-up design handles
this: settlement only considers markets with a past settlement date, and silently
leaves a market unsettled until its NOAA data exists. No special scheduling
beyond the daily run is required.

## Testing (TDD)

- `unsettled_markets`: returns only past-dated markets with no outcome; excludes
  already-settled and future-dated markets.
- Fetch-and-record: given a fixture market and a fixture NOAA response, settle
  writes `actual_value` and `settled_at` to `outcomes`.
- Idempotency: running settlement twice leaves a single, unchanged outcome row.
- Skip-when-no-data: a market whose NOAA date is absent stays unsettled and the
  run still completes.
- End-to-end: record a run (existing helpers), settle against a fixture actual,
  read back the outcome.
- All tests run on SQLite and never hit live NCEI (saved fixtures only).

## Risks

- NOAA-vs-Weather-Underground gap: settlement is a proxy and may disagree with a
  market's real resolution by a degree on edge days. Documented as a proxy;
  acceptable for advisory tracking. A future validation study can quantify it.
- NCEI lag: recent dates may be unavailable for a day or two. Handled by the
  idempotent catch-up; markets settle once their data appears.

## Out of scope (sub-project 3)

P&L, the `won` column, calibration-accuracy reporting, and the Vercel dashboard.
