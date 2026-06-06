# Monthly precipitation math (#35, Tasks 3-6) - implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn a monthly US precipitation market into edge-ranked outcomes, mirroring the temperature pipeline but with a gamma distribution over inch brackets. Parallel path only: the Gaussian, the temperature parsing, and the golden e2e are not modified.

**Architecture:** Two new probability modules (`precip_distribution.py`, `precip_outcomes.py`), one new forecast module (`forecasts/precip.py`), thin precip branches in `backfill.py`/`settle.py`/`tracking.py`, a new `evaluate_precip_market` in `ranking/edge.py` returning the existing `MarketReport`, a unit-label tweak in `report/render.py`, and precip discovery/routing in `cli.py`. No new tables, no migration: precip reuses `markets`/`prices`/`predictions`/`outcomes` with `variable="PRCP"` and float bracket bounds in the JSON spec column.

**Tech stack:** Python 3.11, scipy (`scipy.stats.gamma`), numpy, pydantic, httpx, pytest, pytest-httpx, ruff, mypy. No new dependencies.

- Spec: `docs/superpowers/specs/2026-06-06-precip-monthly-math-design.md` (authoritative; read first)
- Foundation merged in #71: `config.py` (PrecipStation, PRECIP_STATIONS), `polymarket/precip_markets.py`, `polymarket/client.py` (`discover_precip_markets`)
- Issue: #35, branch `feat/35-precip-math`

---

## Decisions (resolved here; each carries a recommendation)

- **D1 `precip_settles` does not exist yet.** The foundation shipped `SETTLEMENT_DECIMALS` and `ROUND_BETWEEN_BRACKETS_UP` in `precip_markets.py` but no `precip_settles`. Add it in `probability/precip_outcomes.py` as a per-bracket function `precip_settles(kind, lo, hi, threshold, actual_value)`, mirroring `outcomes.settles`. Round-up-between-brackets = half-open intervals: `below` is `v < threshold`, `range` is `lo <= v < hi`, `above` is `v >= threshold`, after `v = round(actual, SETTLEMENT_DECIMALS)`.
- **D2 `PRECIP_VAR_FLOOR = 0.01`** (in^2, ~0.1in std). Tests pass `floor` explicitly so they never depend on the constant.
- **D3 Open-Meteo endpoint:** the live forecast endpoints (`api.open-meteo.com/v1/forecast` multimodel + `ensemble-api.open-meteo.com/v1/ensemble`) with `daily=precipitation_sum`, `precipitation_unit=inch`. Climatology comes from NCEI daily-summaries PRCP, not the archive.
- **D4 NWS QPF:** the gridpoint product `GET /gridpoints/{wfo}/{x},{y}` -> `properties.quantitativePrecipitation.values` (6-hourly mm); aggregate to daily, convert mm/25.4 -> inch; guard the unit code like `_check_fahrenheit`. Resolve `{wfo}/{x},{y}` via `/points/{lat},{lon}`.
- **D5 Per-day pooling:** equal-weight pool of all members (each multimodel model = 1, each ensemble member = 1, daily NWS QPF = 1). Per day: mean + sample variance (ddof=1; variance 0 when fewer than 2 members). Sum daily means into `m_f`, daily variances into `v_f` (day-independence approximation).
- **D6 Climatology lookback: 20 years.** `mu_c`, `sigma2_c` = mean + sample variance of all daily PRCP for the target calendar month across the most recent 20 complete years (NCEI daily-summaries).
- **D7 `min_sources`:** count only live forecast sources (`open-meteo`, `nws`) in `coverage`, like temperature. Observed-to-date and climatology are baselines, surfaced as day-coverage counts, not gate "sources."
- **D8 cli persistence (the one integration seam):** `record_run` is shaped for `Market`/`Target`/`Station` and will not duck-type over `PrecipMonthlyMarket`/`PrecipTarget`/`PrecipStation`. Recommended: persist via a minimal recorder extension - an optional `precip_evaluated` argument to `record_run` plus `_record_precip_market` (maps `settlement_date` -> the market-date column, `resolution_name` -> the `resolution_source` column, float bounds into `outcome_spec`), reusing `_record_prices`/`_record_predictions`; skip the `forecasts` table for precip. Alternative (if keeping this issue strictly math+report): include precip `MarketReport`s in the rendered report only and defer persistence; then drop Step 5.4 and settle/track stay unit-tested but dormant.

---

## Task 1: gamma distribution by method of moments, TDD

**Files:**
- Create: `src/rainmaker/probability/precip_distribution.py`
- Modify: `src/rainmaker/config.py` (add `PRECIP_VAR_FLOOR` after `MIN_EDGE`)
- Test: `tests/test_precip_distribution.py`

- [ ] **Step 1.1: Write the failing tests** in `tests/test_precip_distribution.py`:

```python
import pytest
from scipy.stats import gamma as _gamma

from rainmaker.probability.precip_distribution import Gamma, fit_gamma


def test_fit_gamma_recovers_known_mean_and_var():
    g = fit_gamma(4.0, 2.0, floor=0.01)
    assert g.k == pytest.approx(8.0)        # mean^2 / var
    assert g.scale == pytest.approx(0.5)    # var / mean
    assert _gamma(a=g.k, scale=g.scale).mean() == pytest.approx(4.0)
    assert _gamma(a=g.k, scale=g.scale).var() == pytest.approx(2.0)
    assert g.degenerate is False


def test_fit_gamma_applies_variance_floor():
    g = fit_gamma(4.0, 1e-9, floor=0.01)
    assert g.scale == pytest.approx(0.01 / 4.0)
    assert g.k == pytest.approx(4.0**2 / 0.01)


def test_fit_gamma_dry_month_is_degenerate():
    assert fit_gamma(0.0, 0.5, floor=0.01).degenerate is True
    assert fit_gamma(-1.0, 0.5, floor=0.01).degenerate is True
```

- [ ] **Step 1.2: Run, confirm failure:** `uv run pytest tests/test_precip_distribution.py -q` -> `ModuleNotFoundError`.

- [ ] **Step 1.3: Implement** `src/rainmaker/probability/precip_distribution.py`:

```python
from pydantic import BaseModel, ConfigDict


class Gamma(BaseModel):
    """A gamma distribution for a monthly precipitation total (inches).

    shape k and scale, or a degenerate bone-dry spike at 0 when the forecast
    mean is non-positive (the one-sided analogue of the MIN_SIGMA_F guard).
    """

    model_config = ConfigDict(frozen=True)

    k: float
    scale: float
    degenerate: bool = False


def fit_gamma(mean: float, var: float, *, floor: float) -> Gamma:
    """Method-of-moments gamma: k = mean^2/var, scale = var/mean.

    `var` is floored at `floor`. If `mean <= 0` the month is forecast bone-dry;
    return a degenerate distribution (all mass at 0) rather than dividing by zero.
    """
    v = max(var, floor)
    if mean <= 0:
        return Gamma(k=1.0, scale=v, degenerate=True)
    return Gamma(k=mean * mean / v, scale=v / mean)
```

- [ ] **Step 1.4: Add the constant** in `src/rainmaker/config.py`, after `MIN_EDGE`:

```python
PRECIP_VAR_FLOOR = 0.01  # in^2: variance floor for the monthly-total gamma (~0.1in std)
```

- [ ] **Step 1.5: Re-run** `uv run pytest tests/test_precip_distribution.py -q` -> all pass.

---

## Task 2: bracket probability via gamma CDF + precip_settles, TDD

**Files:**
- Create: `src/rainmaker/probability/precip_outcomes.py`
- Test: `tests/test_precip_outcomes.py`

- [ ] **Step 2.1: Write the failing tests** in `tests/test_precip_outcomes.py`:

```python
import json
from pathlib import Path

import pytest

from rainmaker.polymarket.precip_markets import parse_precip_event
from rainmaker.probability.precip_distribution import fit_gamma
from rainmaker.probability.precip_outcomes import bracket_probability, precip_settles

FIXTURES = Path(__file__).parent / "fixtures"


def _nyc_market():
    return parse_precip_event(json.loads((FIXTURES / "polymarket_precip_monthly_nyc.json").read_text()))


def test_partition_sums_to_one():
    g = fit_gamma(3.0, 4.0, floor=0.01)
    total = sum(bracket_probability(g, b) for b in _nyc_market().buckets)
    assert total == pytest.approx(1.0, abs=1e-9)


def test_mode_bracket_has_highest_probability():
    g = fit_gamma(2.5, 0.5, floor=0.01)
    probs = {b.label: bracket_probability(g, b) for b in _nyc_market().buckets}
    assert max(probs, key=probs.get) == '2-3"'


def test_open_tails_use_cdf_complement():
    from scipy.stats import gamma as sgamma
    g = fit_gamma(3.0, 4.0, floor=0.01)
    buckets = {b.label: b for b in _nyc_market().buckets}
    cdf = lambda x: float(sgamma.cdf(x, a=g.k, scale=g.scale))
    assert bracket_probability(g, buckets['<2"']) == pytest.approx(cdf(2.0))
    assert bracket_probability(g, buckets['>6"']) == pytest.approx(1 - cdf(6.0))


def test_degenerate_dry_puts_mass_in_lowest_bracket():
    g = fit_gamma(0.0, 0.5, floor=0.01)
    buckets = {b.label: b for b in _nyc_market().buckets}
    assert bracket_probability(g, buckets['<2"']) == pytest.approx(1.0)
    assert bracket_probability(g, buckets['2-3"']) == pytest.approx(0.0)


def test_precip_settles_round_up_between_brackets():
    assert precip_settles("below", None, None, 2.0, 1.99) is True
    assert precip_settles("below", None, None, 2.0, 2.00) is False
    assert precip_settles("range", 2.0, 3.0, None, 2.00) is True
    assert precip_settles("range", 2.0, 3.0, None, 3.00) is False
    assert precip_settles("above", None, None, 6.0, 6.00) is True
    assert precip_settles("above", None, None, 6.0, 5.99) is False


def test_brackets_tile_so_exactly_one_settles():
    buckets = _nyc_market().buckets
    for actual in (0.0, 1.99, 2.0, 3.5, 6.0, 9.9):
        hits = [b for b in buckets if precip_settles(b.kind, b.lo, b.hi, b.threshold, actual)]
        assert len(hits) == 1
```

- [ ] **Step 2.2: Run, confirm failure** -> `ModuleNotFoundError`.

- [ ] **Step 2.3: Implement** `src/rainmaker/probability/precip_outcomes.py`:

```python
from scipy.stats import gamma as _gamma

from rainmaker.polymarket.markets import BucketKind
from rainmaker.polymarket.precip_markets import SETTLEMENT_DECIMALS, PrecipBracket
from rainmaker.probability.precip_distribution import Gamma


def bracket_probability(g: Gamma, bracket: PrecipBracket) -> float:
    """P(monthly total in this inch bracket) via the gamma CDF.

    Half-open [lo, hi): low tail [0, lo), interior [lo, hi), high tail [hi, inf).
    A degenerate (bone-dry) gamma puts all mass in the lowest bracket.
    """

    def cdf(x: float) -> float:
        if g.degenerate:
            return 1.0 if x > 0 else 0.0
        return float(_gamma.cdf(x, a=g.k, scale=g.scale))

    if bracket.kind == "below":
        assert bracket.threshold is not None
        return cdf(bracket.threshold)
    if bracket.kind == "above":
        assert bracket.threshold is not None
        return 1.0 - cdf(bracket.threshold)
    assert bracket.lo is not None and bracket.hi is not None
    return cdf(bracket.hi) - cdf(bracket.lo)


def precip_settles(
    kind: BucketKind,
    lo: float | None,
    hi: float | None,
    threshold: float | None,
    actual_value: float,
) -> bool:
    """Whether the settled monthly total lands in this bracket. A boundary value
    resolves to the higher bracket (half-open intervals encode the round-up)."""
    v = round(actual_value, SETTLEMENT_DECIMALS)
    if kind == "below":
        assert threshold is not None
        return v < threshold
    if kind == "above":
        assert threshold is not None
        return v >= threshold
    assert lo is not None and hi is not None
    return lo <= v < hi
```

- [ ] **Step 2.4: Re-run** -> all pass.

---

## Task 3: forecast sourcing + monthly-total moments, TDD

**Files:**
- Create: `src/rainmaker/forecasts/precip.py`
- Create fixtures: `tests/fixtures/openmeteo_precip_multimodel_nyc.json`, `tests/fixtures/openmeteo_precip_ensemble_nyc.json`, `tests/fixtures/nws_qpf_nyc.json`, `tests/fixtures/ncei_daily_precip_nyc.json`
- Test: `tests/test_precip_forecast.py`

Pure `monthly_total_moments` is tested on synthetic inputs (no HTTP). Parsers + `build_precip_forecast_set` are fixture-tested only (never live).

- [ ] **Step 3.1: Failing pure-math tests** in `tests/test_precip_forecast.py`:

```python
import pytest

from rainmaker.forecasts.precip import monthly_total_moments


def test_moments_sum_observed_forecast_climatology():
    m, v = monthly_total_moments(
        observed_total=1.20,
        forecast_daily=[[0.1, 0.2, 0.0], [0.0, 0.0, 0.1]],
        clim_daily_mean=0.12, clim_daily_var=0.04, n_tail_days=10, floor=0.01,
    )
    assert m == pytest.approx(1.20 + 0.10 + (0.1 / 3) + 1.20, abs=1e-6)
    assert v > 10 * 0.04


def test_early_month_is_wider_than_late_month():
    _, early_v = monthly_total_moments(
        observed_total=0.0, forecast_daily=[[0.1, 0.1]], clim_daily_mean=0.12,
        clim_daily_var=0.05, n_tail_days=28, floor=0.01,
    )
    _, late_v = monthly_total_moments(
        observed_total=3.0, forecast_daily=[[0.05, 0.05]], clim_daily_mean=0.12,
        clim_daily_var=0.05, n_tail_days=0, floor=0.01,
    )
    assert early_v > late_v


def test_var_floor_applied():
    _, v = monthly_total_moments(
        observed_total=3.0, forecast_daily=[], clim_daily_mean=0.0,
        clim_daily_var=0.0, n_tail_days=0, floor=0.01,
    )
    assert v == pytest.approx(0.01)
```

- [ ] **Step 3.2: Run, confirm failure** -> `ModuleNotFoundError`.

- [ ] **Step 3.3: Implement the pure core** in `src/rainmaker/forecasts/precip.py`:

```python
import statistics


def monthly_total_moments(
    *,
    observed_total: float,
    forecast_daily: list[list[float]],
    clim_daily_mean: float,
    clim_daily_var: float,
    n_tail_days: int,
    floor: float,
) -> tuple[float, float]:
    """Mean and variance of the monthly total: observed-to-date (deterministic) +
    pooled forecast horizon + climatology tail. Daily precip is treated as
    independent across days (a stated approximation that understates variance)."""
    m_f = sum(statistics.fmean(day) for day in forecast_daily if day)
    v_f = sum(statistics.variance(day) for day in forecast_daily if len(day) >= 2)
    m = observed_total + m_f + n_tail_days * clim_daily_mean
    v = v_f + n_tail_days * clim_daily_var
    return m, max(v, floor)
```

- [ ] **Step 3.4: Re-run** the pure tests -> pass.

- [ ] **Step 3.5: Fixture tests for the parsers + orchestrator.** Append to `tests/test_precip_forecast.py` tests for `parse_precip_open_meteo` (pools per-day values in inches, one per model key starting with `precipitation_sum`; raises `ValueError` matching "inch" when `daily_units` are mm) and `build_precip_forecast_set` (mocks NCEI daily-summaries for observed + climatology, the two Open-Meteo URLs, and the NWS `/points/` + `/gridpoints/`; asserts `PrecipForecastSet` with `mean>0`, `var>0`, coverage sources `{open-meteo, nws}`, and `n_observed_days + n_forecast_days + n_clim_days == 30`). Use `httpx_mock`, URLs matched with `re.compile(re.escape(...))`.

- [ ] **Step 3.6: Implement parsers + orchestrator + `PrecipForecastSet`** in `forecasts/precip.py`:
  - `_check_inches(data, field)`: raise unless every `daily_units` key starting with `precipitation_sum` contains "inch".
  - `parse_precip_open_meteo(data, *, models) -> dict[date, list[float]]`: per-date list of `precipitation_sum*` values; pools multimodel and ensemble identically.
  - `fetch_raw_precip_multimodel` / `fetch_raw_precip_ensemble`: mirror `openmeteo.fetch_raw_*` with `daily=precipitation_sum`, `precipitation_unit=inch` (no `temperature_unit`). URLs `FORECAST_URL`, `ENSEMBLE_URL`.
  - `parse_nws_qpf(grid_json, tz) -> dict[date, float]`: read `properties.quantitativePrecipitation.values`, guard `uom == "wmoUnit:mm"`, expand each ISO8601 interval to its local date, sum, convert mm -> inch. Resolve gridpoint via `/points/{lat},{lon}` -> `properties.forecastGridData`.
  - `fetch_precip_climatology(ghcnd_id, month, year, client, *, lookback_years) -> tuple[float, float]`: `backfill.fetch_actuals(..., "PRCP")` over the 20-prior-year span, filter to `d.month == month`, return `(fmean, variance)` (ddof=1; `(0.0, 0.0)` if empty).
  - `build_precip_forecast_set(target, *, today, client, var_floor, lookback_years)`: observed-to-date via `fetch_actuals(..., "PRCP")` over `[month_start, today-1]`; pool Open-Meteo multimodel + each ensemble model + NWS QPF into `forecast_daily` for in-month days after the last observed day (each source wrapped in try/except -> `SourceCoverage(ok=False)` on failure, like `aggregate`); climatology `(mu_c, sigma2_c)`; `n_tail_days = days_in_month - n_observed_days - n_forecast_days`; `mean, var = monthly_total_moments(...)`; return `PrecipForecastSet`.

```python
from pydantic import BaseModel
from rainmaker.forecasts.base import SourceCoverage
from rainmaker.polymarket.precip_markets import PrecipTarget


class PrecipForecastSet(BaseModel):
    target: PrecipTarget
    mean: float
    var: float
    coverage: list[SourceCoverage]
    n_observed_days: int
    n_forecast_days: int
    n_clim_days: int
```

- [ ] **Step 3.7: Build the four fixtures** (small, matching real response shapes; `precipitation_sum_<model>: "inch"` units, June days from 2026-06-06; NWS QPF `quantitativePrecipitation` 6-hourly mm; NCEI daily-summaries PRCP rows for the elapsed June days plus ~20 prior-June spans). Capture once via curl if available; otherwise hand-write the shape.

- [ ] **Step 3.8: Run** `uv run pytest tests/test_precip_forecast.py -q` -> all pass.

---

## Task 4: monthly settlement + tracking, TDD

**Files:**
- Modify: `src/rainmaker/backfill.py` (`fetch_monthly_precip`)
- Modify: `src/rainmaker/settle.py` (branch on `variable == "PRCP"`)
- Modify: `src/rainmaker/tracking.py` (`_won` takes `variable`; `_settled_rows` joins `markets` and selects `m.variable`)
- Create fixture: `tests/fixtures/ncei_gsom_precip_nyc.json`
- Test: append to `tests/test_backfill.py`, `tests/test_settle.py`, `tests/test_tracking.py`

- [ ] **Step 4.1: Failing test for `fetch_monthly_precip`** (GSOM, inches; `None` when unpublished) in `tests/test_backfill.py`.

- [ ] **Step 4.2: Implement `fetch_monthly_precip`** in `backfill.py`:

```python
def fetch_monthly_precip(
    ghcnd_id: str, year: int, month: int, client: httpx.Client
) -> float | None:
    """Monthly total precipitation (inches) from NCEI global-summary-of-the-month.

    Returns None when the month is not yet published, so the settle loop waits.
    Raises on HTTP error.
    """
    ym = f"{year:04d}-{month:02d}"
    resp = client.get(
        NCEI_URL,
        params={
            "dataset": "global-summary-of-the-month",
            "stations": ghcnd_id,
            "dataTypes": "PRCP",
            "startDate": ym,
            "endDate": ym,
            "units": "standard",
            "format": "json",
        },
    )
    resp.raise_for_status()
    for r in resp.json():
        if r.get("PRCP") not in (None, ""):
            return float(r["PRCP"])
    return None
```

- [ ] **Step 4.3: Failing tests for the settle branch** in `tests/test_settle.py` (precip market settles via GSOM; unknown precip city is skipped).

- [ ] **Step 4.4: Implement the settle branch** in `settle.py`: branch on `m["variable"] == "PRCP"` -> `PRECIP_STATIONS.get(m["city"])` + `fetch_monthly_precip(station.ghcnd_id, day.year, day.month, client)`; else the existing `STATIONS` + `fetch_actuals(...)` path. `value is None` -> `waiting += 1`; else `record_outcome` + `settled += 1`.

- [ ] **Step 4.5: Failing test for tracking `_won` on precip** in `tests/test_tracking.py` (a PRCP bet at bucket `2-3"` with actual 2.50 wins via `precip_settles`).

- [ ] **Step 4.6: Implement the tracking branch** in `tracking.py`: add `JOIN markets m ON m.id = p.market_id` to `_settled_rows`, select `m.variable AS variable`; replace `_won`:

```python
from rainmaker.polymarket.precip_markets import parse_precip_bracket_label
from rainmaker.probability.precip_outcomes import precip_settles


def _won(variable: str, bucket_label: str, actual_value: float) -> bool:
    if variable == "PRCP":
        return precip_settles(*parse_precip_bracket_label(bucket_label), actual_value)
    return settles(*parse_bucket_label(bucket_label), actual_value)
```

  Update the two call sites (`_bet_won` and the `compute_calibration` Brier loop) to pass `row["variable"]`. Existing temperature tracking tests already insert a `markets` row with `variable`, so they stay green.

- [ ] **Step 4.7: Run** `uv run pytest tests/test_backfill.py tests/test_settle.py tests/test_tracking.py -q` -> all pass.

---

## Task 5: edge ranking, report unit label, CLI routing, TDD

**Files:**
- Modify: `src/rainmaker/ranking/edge.py` (`evaluate_precip_market`)
- Modify: `src/rainmaker/report/render.py` (unit label conditional on `variable`)
- Modify: `src/rainmaker/cli.py` (discover + route + optional persist)
- Modify (D8): `src/rainmaker/store/record.py` (optional `precip_evaluated` arg + `_record_precip_market`)
- Test: append to `tests/test_edge.py`, `tests/test_render.py`, `tests/test_cli.py`

- [ ] **Step 5.1: Failing test for `evaluate_precip_market`** in `tests/test_edge.py`: YES partition (6 brackets) sums to ~1; `report.variable == "PRCP"`; outcomes sorted by edge desc; `report.mu == mean`, `report.sigma == sqrt(var)`.

- [ ] **Step 5.2: Implement `evaluate_precip_market`** in `ranking/edge.py` (new function; temperature `evaluate_market` untouched): `n_sources = sum(ok)`, `gamma = fit_gamma(mean, var, floor=var_floor)`, per bracket build YES off `best_ask` and NO off `no_ask` with the same `recommended = p >= floor and n_sources >= min_sources and edge >= min_edge`; sort by edge desc; return `MarketReport(..., calibrated=False, mu=mean, sigma=math.sqrt(var), ...)`. Station label = `market.target.station.resolution_name`.

- [ ] **Step 5.3: Render unit label.** Failing test in `tests/test_render.py` (`mu=3.06in sigma=2.00in` for a PRCP report). Implement: in `render_terminal` and `render_markdown`, format the forecast line conditional on `m.variable` (`in` for PRCP, `F` otherwise).

- [ ] **Step 5.4: CLI routing (+ persistence, D8).** Failing test in `tests/test_cli.py` (`_run` routes a discovered precip market: stdout shows the resolution station + PRCP; with the recommended D8 path, `predictions` has >= 6 rows). Implement: import `discover_precip_markets`, `evaluate_precip_market`, `build_precip_forecast_set`, `PRECIP_VAR_FLOOR`; add `_precip_forecast_for(target, today, client)`; in `_run`, after the temperature loop, discover + evaluate precip markets, add their `MarketReport`s to `Report.markets`, and (D8 recommended) persist via `record_run(..., precip_evaluated=...)`. In `store/record.py` add `PrecipEvaluatedMarket`, an optional `precip_evaluated` param, and `_record_precip_market` reusing `_record_prices`/`_record_predictions` (skip `_record_forecasts`).

- [ ] **Step 5.5: Run** `uv run pytest tests/test_edge.py tests/test_render.py tests/test_cli.py tests/test_store_record.py -q` -> all pass.

---

## Task 6: precip golden e2e (temperature golden stays green)

**Files:**
- Modify: `tests/test_golden_e2e.py` (add a precip golden; leave the temperature goldens byte-identical)

- [ ] **Step 6.1: Add `test_golden_precip_pipeline_on_fixture_market`**: parse the NYC precip fixture, build a tight `PrecipForecastSet` (mean 2.5, var 0.6 -> peaks in `2-3"`), `evaluate_precip_market`, assert 6 YES outcomes summing to ~1, mode bucket `2-3"`, edges sorted desc, and `render_markdown` contains the resolution station + `2026-06-30` + the inch unit label.

- [ ] **Step 6.2: Confirm the temperature goldens are unchanged.** `uv run pytest tests/test_golden_e2e.py -q` -> all green.

---

## Verification (whole plan)

```bash
uv run pytest tests/test_precip_distribution.py tests/test_precip_outcomes.py -q
uv run pytest tests/test_precip_forecast.py -q
uv run pytest tests/test_backfill.py tests/test_settle.py tests/test_tracking.py -q
uv run pytest tests/test_edge.py tests/test_render.py tests/test_cli.py -q
uv run pytest tests/test_golden_e2e.py -q          # all three goldens green
uv run pytest                                       # full suite
uv run ruff check . && uv run ruff format --check . && uv run mypy src
```

Also update the `## Toolchain` / `## Repo layout` bullets in `CLAUDE.md` to mention the precip modules (docs-in-same-change; not test-gated).

Constraints honored: TDD throughout; API clients fixture-tested only (`re.compile(re.escape(...))` + `httpx_mock`, never live); free sources only (NCEI daily-summaries + GSOM, NWS gridpoint QPF, Open-Meteo forecast/ensemble); surgical parallel path (Gaussian, temperature parsing, and the two temperature goldens untouched); dual-backend SQL preserved (no new tables, no migration, `variable="PRCP"`, float bounds in the JSON `outcome_spec` column).
