# Monthly precipitation math design (#35, Tasks 3-6)

Approved design for the probability/forecast math that turns a monthly US
precipitation market into edge-ranked outcomes. The foundation (Tasks 0-2:
precip station registry, `Variable=PRCP`, monthly market parsing, discovery)
shipped in #71. This covers Tasks 3-6.

## Scope

Monthly US form only ("Precipitation in NYC/Seattle in <Month>?"). The daily
binary form ("Will it rain on <date>?") is deferred to a follow-up. Free sources
only (NOAA/NCEI, NWS, Open-Meteo). Parallel path: the temperature pipeline, the
Gaussian, and the whole-degree outcome integration are not modified; the golden
e2e stays green.

## Decisions (from the brainstorm)

1. Distribution family: gamma, fit by method of moments from the monthly-total
   mean and variance.
2. Partial-month conditioning: monthly total = observed-to-date + forecast-
   horizon sum + climatology tail for days beyond the forecast horizon.

## The math

### Monthly-total distribution

For a market settling on the total precipitation `X` (inches) over a calendar
month, model `X` as gamma with mean `m` and variance `v`:

- shape `k = m^2 / v`, scale `theta = v / m` (`scipy.stats.gamma(a=k, scale=theta)`).
- `v` is floored at `PRECIP_VAR_FLOOR` (a small positive constant) so a confident
  forecast cannot collapse to a spike; mirrors `MIN_SIGMA_F`'s role for
  temperature.
- Degenerate guard: if `m <= 0`, return a distribution that puts ~all mass in the
  lowest bracket (a bone-dry forecast), rather than dividing by zero.

`fit_gamma(mean, var, *, floor)` lives in `probability/precip_distribution.py`.

### Monthly-total moments (the conditioning)

`monthly_total_moments(...)` in `forecasts/precip.py` builds `(m, v)` as the sum
of three independent pieces for the calendar month:

- observed-to-date: sum of NCEI daily PRCP for elapsed days `1..d`. Deterministic
  (mean = the observed sum, variance = 0).
- forecast-horizon: for days `d+1 .. min(d+H, last)` (H = the Open-Meteo daily
  horizon), per-day precip from the pooled multi-model + ensemble
  `precipitation_sum` and NWS QPF. Per day, take the mean and variance across
  the pooled members; sum the daily means and (assuming day-to-day independence)
  the daily variances into `(m_f, v_f)`.
- climatology tail: for the remaining out-of-horizon days `N_tail`, use the
  station's own NCEI history for that calendar month to get a climatological
  daily mean `mu_c` and daily variance `sigma2_c`; tail mean = `N_tail * mu_c`,
  tail variance = `N_tail * sigma2_c`.

`m = observed + m_f + N_tail*mu_c`, `v = v_f + N_tail*sigma2_c * f(N_tail, rho)` (floored).

Early in the month the tail dominates, so `v` is large, the gamma is wide, and
per-bracket probabilities are low, so few bets clear the floor/edge gates. As the
month progresses the deterministic observed sum grows and the forecast covers
more days, so `v` shrinks and the distribution sharpens. Coverage (how much of
the month is observed vs forecast vs climatology) is tracked and surfaced like
`SourceCoverage`.

Lag-1 autocorrelation inflation (issue #88, implemented): real daily precipitation
is positively autocorrelated (wet spells cluster), so the day-independence
approximation understates the climatology tail variance. The exact finite-N AR(1)
inflation factor `f(N, rho) = 1 + (2/N) * sum_{k=1}^{N-1} (N-k) * rho^k` is
applied to the tail variance only; the forecast-horizon variance (inter-model
spread) and the monthly mean are unchanged. `rho` is fit from NCEI history for
the target calendar month using `_lag1_from_dated_series`, which skips
year-boundary pairs (date diff != 1 day). Negative `rho` is clamped to 0.

### Outcome integration

`bracket_probability(dist, bracket)` in `probability/precip_outcomes.py`
integrates the gamma over each inch bracket using the CDF, consistent with the
settlement rule (a value on a boundary rounds up, so brackets are half-open
`[lo, hi)`):

- interior `[lo, hi)`: `F(hi) - F(lo)`.
- open low tail `[0, lo)`: `F(lo)`.
- open high tail `[hi, inf)`: `1 - F(hi)`.

The full bracket partition sums to 1 by construction. `precip_settles(brackets,
actual)` (added in the foundation) decides which bracket a settled 2-decimal
total lands in, with the round-up-between rule; the YES side wins if the bracket
settles, the NO side wins otherwise (reusing the existing side logic).

### Forecast sourcing

`forecasts/precip.py` adds precip parsers parallel to the temperature ones:

- Open-Meteo `precipitation_sum` (daily) for the multi-model and ensemble
  endpoints, in inches; reject non-inch units the way `_check_fahrenheit` guards
  temperature.
- NWS QPF (`quantitativePrecipitation` from the gridpoint product) as an
  additional member.
- climatology from the station's own NCEI daily-summaries PRCP history (about 20
  years for the target calendar month) -> `mu_c`, `sigma2_c`.

These feed `monthly_total_moments`. A `PrecipForecastSet` carries the coverage.

### Settlement

`backfill.py` adds `fetch_monthly_precip(ghcnd_id, year, month, client)` reading
NCEI `global-summary-of-the-month` PRCP (inches). `settle.py` branches on the
market variable: `PRCP` markets settle against `PRECIP_STATIONS` + the monthly
GSOM total; the existing "wait until the actual is published" path is unchanged
(GSOM publishes a few days into the next month). `tracking.py` gets a precip
branch in `_won` using `precip_settles`.

### Ranking + report + CLI

- `ranking/edge.py`: `evaluate_precip_market(market, forecast_set, *, floor,
  min_sources, min_edge, ...)` builds the gamma, integrates over brackets, and
  produces the existing `MarketReport`/`RankedOutcome` with the same
  floor/min_sources/min_edge gates. No gate change.
- `report/render.py`: the `mu`/`sigma` unit label is conditional on the
  variable (`in` for PRCP, `F` for temperature).
- `cli.py` `_run`: discover precip markets (`discover_precip_markets` from the
  foundation) and route them through `evaluate_precip_market` alongside the
  temperature markets; calibration is `None` (uncalibrated) for now.

## Data model / store

No new tables, no migration. Precip reuses `markets`/`prices`/`forecasts`/
`predictions`/`outcomes` with `variable="PRCP"`; bracket bounds are floats in the
existing JSON spec column; `outcomes.actual_value` holds the monthly total in
inches; `settlement_date` holds the month's resolution date. Dual-backend SQL
portability is preserved.

## Testing (TDD)

- `precip_distribution`: method-of-moments recovers a known `(mean, var)`;
  variance floor; degenerate/near-zero handling.
- `precip_outcomes`: partition sums to ~1; the mode bracket has the highest
  probability; tails integrate correctly; `precip_settles` round-up rule.
- `monthly_total_moments`: early-month (climatology-dominated, wide) vs
  late-month (forecast-dominated, narrow) on synthetic inputs; observed +
  forecast + climatology add up.
- forecast sourcing + `fetch_monthly_precip`: against saved fixtures, never live.
- `evaluate_precip_market`: YES partition sums to ~1; gates reused.
- a new precip golden e2e (fixture precip market + synthetic monthly forecast ->
  expected ranked report); the temperature golden stays green.

## Edge cases

- Dry month / near-zero forecast: the degenerate guard puts mass in the lowest
  bracket; no division by zero.
- Mid-month with no forecast coverage yet (horizon entirely climatology): valid
  but wide; the gates naturally suppress weak bets.
- Thin/incoherent live prices (the monthly books are new and wide): not a math
  concern here; surfaced as edge/coverage in the report. Liquidity-aware sizing
  is out of scope (advisory only).

## Open / deferred

- Daily binary precip form (single probability of >= 0.01 in), deferred.
- Precip calibration cells (the temperature calibration path is variable-keyed
  and could be extended later); MVP runs uncalibrated.
