# rainmaker-bot MVP 1.0 (advisory) - design

Date: 2026-05-29
Status: approved, pre-implementation

## Goal

A Python advisory bot that produces a daily, confidence-ranked "best call" for
US-city weather markets on Polymarket. It gathers forecasts from multiple free
sources, turns them into a calibrated probability for each market outcome,
compares that probability to the market price, and ranks the bets by edge
(expected value). The user reviews the report and places bets manually. No
automated trading in 1.0.

The whole value depends on the forecast being near-perfect for the exact
quantity that settles each market, so accuracy of the data foundation is the
top priority.

## Roadmap context

- MVP 1.0: advisory (this doc).
- MVP 2.0: tracking. Log positions, daily P&L, settle markets against NOAA
  actuals, and report calibration over time. Likely the point we move the
  datastore from SQLite to Supabase and add a web dashboard.
- MVP 3.0: fully automated trading via Polymarket's CLOB API.

Paid data sources are a revenue-gated roadmap item, not part of 1.0.

## Decisions (locked for 1.0)

- Language: Python.
- Data sources: free only. NWS/NOAA (official US forecasts and observations)
  and Open-Meteo (multi-model deterministic + ensemble API).
- Geography: US cities only. NWS/NOAA station data is the settlement ground
  truth.
- Markets: mixed. Daily high temperature (bucketed), precipitation yes/no, and
  threshold temperature. The bot scans whatever weather markets are live.
- Ranking: by edge (forecast probability minus implied market price), gated by
  a confidence floor and a minimum-source requirement.
- Run model: manual CLI run that prints a ranked report and writes a dated
  markdown + JSON file.
- Datastore: SQLite, with a schema designed to port to Supabase Postgres later
  (JSON columns map to jsonb, no rewrite expected).

## Top validation risk

It is not confirmed that Polymarket currently lists US-city weather markets in
the volume and form assumed here. Kalshi is the dominant US venue for weather
and temperature markets. The exact resolution source per market (station,
agency, rounding, settlement time) is also unconfirmed and is the single most
important input to accuracy.

Mitigation: Phase 0 is a discovery spike that enumerates live Polymarket
weather markets and documents their resolution rules. It is a decision gate. If
markets are absent or thin, stop and reconsider (for example, add or pivot to
Kalshi) before building the rest.

## Architecture

A Python package with a CLI entry point. `rainmaker run` executes a linear
pipeline; each stage is an isolated, independently testable unit with a clear
input and output. No web server and no always-on process.

### Daily pipeline

1. Discover markets. Read Polymarket's public API (Gamma for market metadata,
   CLOB for prices and order book). Find live US-city weather markets. Capture
   each market's resolution rule: station, variable, buckets or threshold,
   settlement time, rounding.
2. Resolve settlement target. Turn each market into a concrete forecastable
   quantity: station id (for example KNYC = Central Park), variable (Tmax or
   precipitation occurrence), local date, units.
3. Fetch forecasts. Query NWS and Open-Meteo (multi-model + ensemble) for that
   exact quantity and lead time. Normalize units and timezones.
4. Build calibrated distribution. Combine members into a predictive
   distribution, then apply a per-(station, variable, lead_time) bias and
   spread-scale correction learned from historical backfill.
5. Compute outcome probabilities. Integrate the calibrated distribution over
   each market's outcome definition to get P(win).
6. Rank by edge. edge = P(win) - implied price (the price actually paid, for
   example best ask for a YES buy). Apply the confidence floor and minimum
   source gate. Sort survivors by edge.
7. Report and persist. Terminal table + dated markdown/JSON file. Write
   everything to SQLite.
8. (MVP 2.0) Settle and recalibrate. Once NOAA actuals land, record the
   realized outcome and update calibration.

### Repo layout

```
rainmaker-bot/
  pyproject.toml              # deps: httpx, pydantic, numpy, scipy, pandas
  CLAUDE.md
  docs/architecture/decisions.md
  docs/superpowers/specs/...
  src/rainmaker/
    cli.py                    # `rainmaker run`, `rainmaker backfill`
    config.py                 # settings, station registry, source toggles
    polymarket/               # client.py, markets.py (-> ResolutionTarget)
    forecasts/                # nws.py, openmeteo.py, base.py, aggregate.py
    probability/              # distribution.py, calibration.py, outcomes.py
    ranking/                  # edge.py (EV, confidence floor, ranking)
    report/                   # render.py (terminal + markdown + json)
    store/                    # db.py (SQLite, Postgres-ready), models.py
  tests/
```

Each subpackage has one job and a narrow interface. The probability engine never
knows which API a forecast came from; `forecasts` just exposes normalized
samples for a target.

## The brains

### Forecast aggregation (forecasts/aggregate.py)

Each source implements a common `ForecastSource` protocol returning normalized
samples for a target. NWS gives one official point forecast. Open-Meteo gives
multi-model deterministic runs plus ensemble members. We pool these into a
sample set, tagging each by source and model so we can weight and audit them.
Model disagreement is itself a signal: wide spread means low confidence,
regardless of the mean.

### Calibrated distribution (probability/distribution.py + calibration.py)

Raw ensemble spread is reliably overconfident, so we do not trust it directly.
Fit a predictive distribution (Gaussian for temperature via mean and calibrated
sigma; a pooled probability for binary precipitation), then correct it with a
per-(station, variable, lead_time) bias and spread-scale learned from history.

`backfill` pulls past Open-Meteo forecasts and NOAA actuals, builds
forecast-vs-outcome pairs, and fits those correction terms, so the bot is
calibrated before going live. Until a cell has enough samples, fall back to a
conservative widened spread and flag low confidence.

### Outcome probability (probability/outcomes.py)

Integrate the calibrated distribution over each market's outcome spec:

- bucket: CDF(upper) - CDF(lower)
- threshold: 1 - CDF(threshold)
- binary precipitation: the pooled probability

Output is a clean P(win) per tradeable outcome.

### Edge ranking (ranking/edge.py)

edge = P(win) - implied_price, using the price actually paid. Two gates before
anything is recommended:

- confidence floor: calibrated P(win) >= threshold (for example 0.90)
- minimum source requirement: at least N independent sources present

Rank survivors by edge. Config holds the thresholds.

### Report (report/render.py)

Terminal table + dated markdown + JSON, sorted by edge. For each market it
shows P(win), confidence band, implied price, edge, sources used, and data
freshness. A "recommended" flag marks the gated picks. Nothing is hidden, so the
reason for every call is visible.

## Error handling

- Forecast source down: proceed with the rest, record coverage, reflect it in
  confidence. Never silently use partial data.
- Polymarket API down: abort with a clear message. No markets means nothing to
  do.
- Stale data past a freshness limit: excluded and noted.
- A recommendation is only emitted when the data behind it is complete and fresh
  enough to defend.

## Data model

SQLite now, Postgres/Supabase-ready (JSON columns map to jsonb).

| table        | purpose |
|--------------|---------|
| runs         | one row per run: id, started/finished, status, source coverage |
| markets      | polymarket id, slug, title, city, variable, resolution_source, settlement_date, outcome_spec (json), raw (json), captured_at |
| prices       | market_id, outcome, price, implied_prob, captured_at |
| forecasts    | run_id, market_id, source, model, variable, values/members (json), lead_time, fetched_at |
| predictions  | run_id, market_id, p_win, confidence, dist_params (json), edge, recommended, created_at |
| outcomes     | market_id, actual_value, won, settled_at (filled in MVP 2.0) |
| calibration  | station, variable, lead_time, bias, spread_scale, n_samples, updated_at |

Every run is fully reconstructable for auditing and calibration.

## Testing

TDD where it counts.

- Math (distribution, calibration, outcomes, edge): pure functions, tested first
  against synthetic inputs with known answers.
- API clients (polymarket, nws, openmeteo): tested against saved JSON fixtures,
  never live endpoints. Fast and deterministic.
- One golden end-to-end test: fixture markets + fixture forecasts produce an
  expected ranked report.

## Phasing within MVP 1.0

- Phase 0 - Discovery spike. Enumerate live Polymarket weather markets and
  document resolution rules. Decision gate.
- Phase 1 - Forecasts. NWS + Open-Meteo fetch and normalize for one
  city+variable, end to end.
- Phase 2 - Engine + report. Uncalibrated ensemble, outcome probability, edge
  ranking, report. First real daily call.
- Phase 3 - Persistence. SQLite logging of runs/markets/prices/forecasts/
  predictions.
- Phase 4 - Calibration. Historical backfill + bias/spread fitting. The accuracy
  payoff.
- Phase 5 - Breadth. More cities and market types.

Each phase is independently verifiable and leaves a working tool.
