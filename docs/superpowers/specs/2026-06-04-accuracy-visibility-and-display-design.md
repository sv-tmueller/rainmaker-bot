# Forecast accuracy visibility, C/F display, min-edge gate - design

Date: 2026-06-04
Status: approved, pre-implementation

## Goal

Three improvements to the live advisory system:

1. Make forecast accuracy visible: how many degrees off the temperature
   forecasts are, per city, on the dashboard. Today the calibration backfill
   computes forecast-vs-actual pairs and throws the accuracy away, and live
   tracking scores only probability-space metrics (Brier, hit rate).
2. Show Celsius alongside Fahrenheit on the dashboard: in the bucket labels and
   in a new per-bet forecast column.
3. Stop recommending near-worthless bets (pay 0.99 to win 0.01) by adding a
   minimum-edge threshold to the recommendation gates. The 2026-06-04 dashboard
   showed four recommended bets with edge at or below +0.01.

## Background

- `rainmaker backfill` (backfill.py) builds historical forecast-vs-actual pairs
  per (station, variable, lead) and fits a calibration cell. Only the fitted
  bias and spread scale survive; MAE and bias in degrees are discarded.
- `rainmaker settle` records NOAA actuals into `outcomes`; `rainmaker snapshot`
  (tracking.py) writes a daily probability-space snapshot the dashboard reads.
- Predictions store the forecast Gaussian as `dist_params` JSON (mu, sigma,
  n_sources) per bucket row, so degrees-space accuracy of the bot's own runs is
  computable from data already persisted.
- The recommendation gate (ranking/edge.py) is `p_win >= floor AND n_sources >=
  min_sources AND edge > 0`. The code comments already flag a minimum-edge
  threshold as the natural next tuning knob.

## Decisions

- Accuracy comes from both sources: a historical backtest (available
  immediately) and the bot's own settled predictions (grows over time). Both
  land in one new table; the dashboard is the only display surface.
- The dashboard stays a thin reader. All accuracy math lives in Python.
- Unit conversion is display-only. Storage and domain math stay in Fahrenheit
  (the settlement unit). Celsius is derived at render time.
- Minimum edge default: 0.05.

## Data model

One new table in the base schema (`store/db.py`, both backends). New tables go
in the base schema per repo convention; `CREATE TABLE IF NOT EXISTS` picks it
up on existing databases, no migration needed.

```sql
CREATE TABLE IF NOT EXISTS forecast_accuracy (
    station    TEXT NOT NULL,      -- ICAO, e.g. KSEA
    city       TEXT,               -- display name, denormalized for the dashboard
    variable   TEXT NOT NULL,      -- TMAX
    lead_time  INTEGER NOT NULL,   -- days ahead
    kind       TEXT NOT NULL,      -- 'backtest' | 'live'
    n          INTEGER,            -- samples behind the numbers
    mae_f      REAL,               -- mean absolute error, degrees F
    bias_f     REAL,               -- mean signed error (forecast - actual), degrees F
    updated_at TEXT,
    PRIMARY KEY (station, variable, lead_time, kind)
);
```

Mirrors the `calibration` table key plus `kind`. Rows are upserted
(idempotent). REAL becomes DOUBLE PRECISION on Postgres via the existing schema
rewrite.

## Component changes

### Backtest accuracy (backfill path)

- `backfill.py`: a pure function computes n, MAE, and bias from the
  `CalibrationPair` list that `run_backfill` already builds. `run_backfill`
  returns the accuracy result alongside the `Calibration`.
- `store/record.py`: `save_accuracy` upserts one row.
- `cli.py`: `rainmaker backfill` saves both rows and prints the accuracy.
  New convenience: `--city all` loops over every station in the registry, so
  one command populates all 11 cities. A city whose fetch fails is reported
  and skipped; the loop continues.
- No accuracy row is written when there are zero pairs.
- Honest labeling: backtest numbers measure the Open-Meteo multi-model mean
  (what backfill uses), an approximation of the live pipeline. The dashboard
  column is labeled "backtest".

### Live accuracy (snapshot path)

- `tracking.py`: new `compute_live_accuracy(conn)`. Join settled `outcomes`
  with `predictions` (mu from `dist_params`, one value per run+market, deduped
  across bucket rows), `markets` (city, variable, settlement date), and `runs`
  (start date). Lead time = settlement date minus run start date, in days.
  Aggregate per
  (station, variable, lead): n, MAE, bias. Station comes from the city via the
  station registry; rows with an unknown city or a null mu are skipped.
- `write_snapshot` upserts these rows as `kind='live'` after the existing
  snapshot row. The daily cron (run, settle, snapshot) needs no workflow
  change; live numbers grow as markets settle.

### Min-edge gate

- `config.py`: `MIN_EDGE = 0.05`.
- `ranking/edge.py`: `evaluate_market` gains a required `min_edge` keyword.
  The gate becomes `edge >= min_edge` (replacing `edge > 0`). The stale code
  comment about the future tuning knob is updated.
- `cli.py` passes `MIN_EDGE`.
- Stored historical predictions are untouched; only future runs change.

### Dashboard (dashboard/app/page.tsx)

- New "Forecast accuracy" section below Performance, reading
  `forecast_accuracy`. One row per (city, lead), columns: City, Lead, Backtest
  MAE / bias (n), Live MAE / bias (n). Empty state: "No accuracy data yet."
- Degrees display: errors shown as degrees F with degrees C in parentheses.
  Error deltas convert as C = F * 5/9 (no offset; these are differences).
- Bucket labels in the bets table get the Celsius equivalent appended, e.g.
  "69°F or below (<= 20.6°C)", "68-69°F (20.0-20.6°C)". A small TS helper
  converts every Fahrenheit number found in the label (C = (F - 32) * 5/9,
  one decimal).
- New "Forecast" column in the bets table: the bot's mu for that market
  rendered as "67.2°F / 19.6°C". The predictions query additionally selects
  `dist_params`; mu is parsed from the JSON in the server component, not
  in SQL.

## Error handling

- Backfill `--city all`: per-city failures print to stderr and the loop
  continues. Exit 0 if at least one city succeeded, exit 1 if all failed.
- Live accuracy: skip (do not fail on) predictions with null mu, unknown
  cities, or unparsable dist_params. Mirrors settle.py's skip-and-report
  pattern.
- Dashboard: missing or empty `forecast_accuracy` renders the empty state;
  a bet row with no parsable mu shows a blank forecast cell.

## Testing

TDD per repo convention (the math first, against synthetic inputs with known
answers):

- Accuracy from pairs: a fixed pair list with hand-computed MAE and bias.
- `compute_live_accuracy`: seeded in-memory SQLite store (runs, markets,
  predictions, prices, outcomes), known expected aggregates; dedup across
  bucket rows verified.
- Upsert idempotency: writing the same accuracy row twice leaves one row.
- Min-edge gate: a near-certain bucket (p_win 0.997) at ask 0.99 is not
  recommended; the same bucket at ask 0.90 is.
- Golden e2e: stays green; expectations updated deliberately where the
  min-edge gate changes recommendations.
- Dashboard: `npm run build` passes. The TS Celsius helper is exercised by
  the build's type check; no JS test infra exists and none is added.

## Work breakdown

Three issues, three PRs, in order:

1. Min-edge gate. Smallest, independent, fixes the visible problem today.
2. Forecast accuracy visibility: table, backfill and snapshot writers,
   dashboard section.
3. C/F display: bucket labels plus the forecast column.

## Out of scope

- Precipitation and TMIN markets (separate roadmap slices).
- Unit toggle or user preference storage; both units are always shown.
- Accuracy-based tuning of the confidence floor or minimum edge (a later
  decision once numbers accumulate).
- CLI display of accuracy (dashboard only, per the design decision).
