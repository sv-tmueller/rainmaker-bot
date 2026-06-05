# Backtest Harness Implementation Plan (#59 Part 1)

**Goal:** Add a `rainmaker backtest` command that quantifies, over history, whether
the forecast model is good enough to bet on: forecast calibration and win-rate.
No betting P/L (that is Part 2, blocked on historical market prices).

**Scope (agreed: "both"):**
1. Synthetic long-history pass over a standard 2F bucket scheme (the primary,
   long-history calibration evidence).
2. A recent real-closed-market reality check (confirm the synthetic scheme is
   representative).

**Architecture:** One new analysis module `src/rainmaker/backtest.py` plus a CLI
subcommand. It reuses, without changing the math:
- `backfill.fetch_historical_forecasts` (Open-Meteo archive -> per-date Gaussian)
- `backfill.fetch_actuals` (NCEI daily extremes)
- `probability.outcomes.bucket_probability` (Gaussian + bucket -> p_win)
- `polymarket.markets.parse_bucket_label` / `event_to_markets`
- `probability.calibration.apply_calibration` (optional calibrated vs raw)

**Key decisions (from discussion):**
- Lead is nominal. `fetch_historical_forecasts` sends no horizon param; the
  archive returns one series per date and `run_backfill`'s `lead_time` is only a
  label. The backtest reports at that single horizon (~lead 1) and says so. Real
  per-lead backtesting needs a different API path; out of scope.
- Synthetic buckets are centered on `round(forecast mu)`, never on the actual
  (non-leaky; mirrors how a market maker centers the ladder).
- Output is a terminal + markdown + JSON report under `reports/`. No DB or
  dashboard wiring in this PR.
- Win logic must stay single-source. Extract the parsed-bucket win rule into
  `outcomes.py` and have both `tracking._won` and the backtest call it, so the
  live tracker and the backtest can never disagree.

## Domain facts (verified)

- Real markets: a below-tail, 2F-wide range buckets, an above-tail. NYC example
  spans 59F or below .. 78F or higher (11 buckets, 20F core). Stable across
  cities; only the center moves seasonally.
- `bucket_probability` is continuity-corrected (settlement rounds to whole F):
  below = cdf(t+0.5); above = 1-cdf(t-0.5); range = cdf(hi+0.5)-cdf(lo-0.5).
- `_won` rounds the actual with Python `round` (half-to-even) and compares.
- Gamma `closed=true` returns closed events with their bucket definitions; the
  client currently hardcodes `closed=false`.

---

## Task 1: Single-source the win rule

**Files:** `src/rainmaker/probability/outcomes.py`, `src/rainmaker/tracking.py`

- [ ] Add `settles(kind, lo, hi, threshold, actual_value) -> bool` to `outcomes.py`:
  `v = round(actual_value)`; below -> `v <= threshold`; above -> `v >= threshold`;
  range -> `lo <= v <= hi`.
- [ ] Refactor `tracking._won` to `return settles(*parse_bucket_label(label), actual_value)`.
- [ ] Test: `settles` for below/above/range incl. the half-to-even boundary
  (e.g. 70.5 -> 70). Existing `test_tracking` + the golden e2e must stay green.

## Task 2: Synthetic bucket scheme

**Files:** `src/rainmaker/backtest.py` (new), `tests/test_backtest.py` (new)

- [ ] `standard_buckets(center, *, width=2, span=10) -> list[Bucket]`: range los
  aligned to even degrees, ranges covering `round(center) +/- span`, plus a
  below-tail (`lo0 - 1` or below) and above-tail (`hiN + 1` or higher). Labels
  must round-trip through `parse_bucket_label`. `yes_token_id=""`, asks `None`.
- [ ] Tests: count and coverage (e.g. center 70 -> "59F or below", "60-61F"..,
  "78F or higher"); probabilities over the full scheme sum to ~1.0 for any
  Gaussian; labels parse back to the same kind/lo/hi/threshold.

## Task 3: Day scoring + aggregation (pure, TDD)

**Files:** `src/rainmaker/backtest.py`, `tests/test_backtest.py`

- [ ] `score_day(g, buckets, actual) -> DayScore` with: per-bucket p_win, modal
  index + modal p_win, `modal_won`, multi-bucket Brier
  `sum_b (p_b - 1{won_b})^2`, and coverage flags at 50/80/90 from the Gaussian
  (`covered_q = abs(cdf(actual)-0.5) <= q/2`).
- [ ] `aggregate(day_scores) -> BacktestResult`: `n`, `modal_hit_rate`,
  `mean_modal_p`, `mean_brier`, `coverage_50/80/90`, and a reliability table
  (predicted-prob decile -> observed win frequency, predicted mean, count) over
  all (p_b, won_b) bucket-day pairs.
- [ ] Tests with hand-computable inputs: a sharp Gaussian centered on a bucket
  gives modal_won=True and high coverage; reliability binning math; Brier of a
  known case.

## Task 4: Orchestration over history

**Files:** `src/rainmaker/backtest.py`, `tests/test_backtest_io.py` (new)

- [ ] `backtest_synthetic(station, variable, start, end, client, *, width, span)
  -> BacktestResult`: reuse `fetch_historical_forecasts` + `fetch_actuals`, pair
  on date, build `standard_buckets(round(g.mu))` per day, score, aggregate.
- [ ] Test against the existing NCEI + historical-forecast fixtures
  (pytest-httpx), asserting `n` and that metrics are finite and in range.

## Task 5: Real closed-market reality check

**Files:** `src/rainmaker/polymarket/client.py`, `src/rainmaker/backtest.py`,
`tests/fixtures/polymarket_closed_weather_events.json` (new), tests

- [ ] `fetch_closed_weather_events(client, *, page_size, max_pages)` in
  `client.py` (same as the live fetch but `closed=true`, no `active`).
- [ ] `backtest_real(events, client, *, on_or_after) -> BacktestResult`: for each
  closed market parse to `Market`, fetch that single date's forecast + NCEI
  actual, score with the real buckets, aggregate. Skip markets we cannot map to
  a station or that lack an actual.
- [ ] Fixture for closed events (adapt the live fixture, `closed=true`). Tests:
  the fetch hits `closed=true`; scoring over the fixture yields a result with
  the expected `n`.

## Task 6: Report + CLI

**Files:** `src/rainmaker/backtest.py` (render), `src/rainmaker/cli.py`, tests

- [ ] `render_report(synthetic_by_city, real_summary) -> (text, md, json)`:
  per-city and overall table (n, modal hit-rate, mean modal p, Brier,
  coverage), the reliability table, and the synthetic-vs-real comparison.
- [ ] `backtest` subcommand: `--city` (default all), `--days` (default 730),
  `--width`, `--span`, `--reports-dir`, `--real/--no-real`. Writes
  `reports/backtest-<date>.md` and `.json`, prints the terminal table.
- [ ] Test the CLI wiring with mocked fetchers (no live calls).

## Verification

- `uv run pytest` green, including the new tests and the golden e2e.
- `uv run ruff check .` and `uv run ruff format --check .` clean.
- `uv run mypy src` clean.
- One real run: `uv run rainmaker backtest --city NYC --days 365` produces a
  sane report (n in the hundreds, calibration numbers plausible, reliability
  table monotonic-ish). Eyeball, do not assert exact values.
- Confirm the report answers #58's question: does mean modal p_win match the
  modal hit-rate, and are the reliability bins well-calibrated.
