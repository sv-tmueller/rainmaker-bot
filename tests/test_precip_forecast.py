import json
import re
from datetime import date
from pathlib import Path

import httpx
import pytest

from rainmaker.backfill import NCEI_URL
from rainmaker.forecasts.openmeteo import ENSEMBLE_URL, FORECAST_URL
from rainmaker.forecasts.precip import (
    _lag1_from_dated_series,
    build_precip_forecast_set,
    monthly_total_moments,
    parse_nws_qpf,
    parse_precip_open_meteo,
    variance_inflation_factor,
)
from rainmaker.polymarket.precip_markets import parse_precip_event

FIXTURES = Path(__file__).parent / "fixtures"


def _nyc_target():
    return parse_precip_event(
        json.loads((FIXTURES / "polymarket_precip_monthly_nyc.json").read_text())
    ).target


def _multimodel():
    return json.loads((FIXTURES / "openmeteo_precip_multimodel_nyc.json").read_text())


def _ensemble():
    return json.loads((FIXTURES / "openmeteo_precip_ensemble_nyc.json").read_text())


def test_moments_sum_observed_forecast_climatology():
    m, v = monthly_total_moments(
        observed_total=1.20,
        forecast_daily=[[0.1, 0.2, 0.0], [0.0, 0.0, 0.1]],
        clim_daily_mean=0.12,
        clim_daily_var=0.04,
        n_tail_days=10,
        floor=0.01,
    )
    assert m == pytest.approx(1.20 + 0.10 + (0.1 / 3) + 1.20, abs=1e-6)
    assert v > 10 * 0.04


def test_early_month_is_wider_than_late_month():
    _, early_v = monthly_total_moments(
        observed_total=0.0,
        forecast_daily=[[0.1, 0.1]],
        clim_daily_mean=0.12,
        clim_daily_var=0.05,
        n_tail_days=28,
        floor=0.01,
    )
    _, late_v = monthly_total_moments(
        observed_total=3.0,
        forecast_daily=[[0.05, 0.05]],
        clim_daily_mean=0.12,
        clim_daily_var=0.05,
        n_tail_days=0,
        floor=0.01,
    )
    assert early_v > late_v


def test_var_floor_applied():
    _, v = monthly_total_moments(
        observed_total=3.0,
        forecast_daily=[],
        clim_daily_mean=0.0,
        clim_daily_var=0.0,
        n_tail_days=0,
        floor=0.01,
    )
    assert v == pytest.approx(0.01)


def test_parse_precip_open_meteo_pools_per_day_in_inches():
    pooled = parse_precip_open_meteo(_multimodel())
    # Every model reports June 6, so the pool has one value per model.
    assert len(pooled[date(2026, 6, 6)]) == 5
    assert pooled[date(2026, 6, 6)] == pytest.approx([0.043, 0.213, 0.004, 0.102, 0.0])
    # meteofrance is null on June 10, so that day pools only the four models present.
    assert len(pooled[date(2026, 6, 10)]) == 4


def test_parse_precip_open_meteo_pools_ensemble_members():
    pooled = parse_precip_open_meteo(_ensemble())
    # The control run plus five members all report June 6.
    assert len(pooled[date(2026, 6, 6)]) == 6


def test_parse_precip_open_meteo_rejects_non_inch_units():
    data = _multimodel()
    data["daily_units"] = {
        k: ("mm" if k.startswith("precipitation_sum") else v)
        for k, v in data["daily_units"].items()
    }
    with pytest.raises(ValueError, match="inch"):
        parse_precip_open_meteo(data)


def test_parse_nws_qpf_skips_null_value_entries():
    # NWS allows null in the value field; float(None) raises TypeError.
    # Null entries must be skipped without crashing.
    grid_json = {
        "properties": {
            "quantitativePrecipitation": {
                "uom": "wmoUnit:mm",
                "values": [
                    {"validTime": "2026-06-10T06:00:00+00:00/PT6H", "value": None},
                    {"validTime": "2026-06-10T12:00:00+00:00/PT6H", "value": 5.08},
                    {"validTime": "2026-06-11T00:00:00+00:00/PT6H", "value": None},
                ],
            }
        }
    }
    result = parse_nws_qpf(grid_json, "America/New_York")
    # Only the non-null entry contributes: 5.08 mm / 25.4 = 0.2 inches on June 10.
    assert date(2026, 6, 10) in result
    assert result[date(2026, 6, 10)] == pytest.approx(0.2)
    # June 11 had only a null entry; it must not appear in the result.
    assert date(2026, 6, 11) not in result


def _mock_build(httpx_mock):
    daily = json.loads((FIXTURES / "ncei_daily_precip_nyc.json").read_text())
    clim = json.loads((FIXTURES / "ncei_climatology_precip_nyc.json").read_text())
    # NCEI is hit twice: observed-to-date first, then the climatology span.
    httpx_mock.add_response(url=re.compile(re.escape(NCEI_URL)), json=daily)
    httpx_mock.add_response(url=re.compile(re.escape(NCEI_URL)), json=clim)
    httpx_mock.add_response(url=re.compile(re.escape(FORECAST_URL)), json=_multimodel())
    for _ in range(3):  # OPENMETEO_ENSEMBLE_MODELS has three entries
        httpx_mock.add_response(url=re.compile(re.escape(ENSEMBLE_URL)), json=_ensemble())
    httpx_mock.add_response(
        url="https://api.weather.gov/points/40.779,-73.9692",
        json={"properties": {"forecastGridData": "https://api.weather.gov/gridpoints/OKX/34,45"}},
    )
    httpx_mock.add_response(
        url="https://api.weather.gov/gridpoints/OKX/34,45",
        json=json.loads((FIXTURES / "nws_qpf_nyc.json").read_text()),
    )


def test_build_precip_forecast_set_climatology_only_when_both_sources_fail(httpx_mock):
    # Both live forecast sources down: coverage degrades but observed-to-date plus
    # climatology still yield a usable monthly total (the run does not abort).
    daily = json.loads((FIXTURES / "ncei_daily_precip_nyc.json").read_text())
    clim = json.loads((FIXTURES / "ncei_climatology_precip_nyc.json").read_text())
    httpx_mock.add_response(url=re.compile(re.escape(NCEI_URL)), json=daily)
    httpx_mock.add_response(url=re.compile(re.escape(NCEI_URL)), json=clim)
    httpx_mock.add_response(url=re.compile(re.escape(FORECAST_URL)), status_code=500)
    httpx_mock.add_response(url="https://api.weather.gov/points/40.779,-73.9692", status_code=500)
    with httpx.Client() as client:
        fs = build_precip_forecast_set(
            _nyc_target(),
            today=date(2026, 6, 6),
            client=client,
            var_floor=0.01,
            lookback_years=20,
        )
    assert all(not c.ok and c.error is not None for c in fs.coverage)
    assert fs.n_forecast_days == 0
    assert fs.n_observed_days > 0
    assert fs.n_clim_days > 0
    assert fs.mean > 0
    assert fs.var > 0


def test_build_precip_forecast_set_pools_all_sources(httpx_mock):
    _mock_build(httpx_mock)
    with httpx.Client() as client:
        fs = build_precip_forecast_set(
            _nyc_target(),
            today=date(2026, 6, 6),
            client=client,
            var_floor=0.01,
            lookback_years=20,
        )
    assert fs.mean > 0
    assert fs.var > 0
    assert {c.source for c in fs.coverage} == {"open-meteo", "nws"}
    assert all(c.ok for c in fs.coverage)
    # Five elapsed days observed, the seven-day forecast horizon, the rest climatology.
    assert fs.n_observed_days == 5
    assert fs.n_forecast_days == 7
    assert fs.n_observed_days + fs.n_forecast_days + fs.n_clim_days == 30


# ---------------------------------------------------------------------------
# Lag-1 autocorrelation inflation factor (issue #88)
# ---------------------------------------------------------------------------

# The finite-N AR(1) variance-inflation factor for the sum of N correlated days:
#   f(N, rho) = Var(S_N) / (N * sigma^2)
# For AR(1): Var(S_N) = sigma^2 * [N + 2 * sum_{k=1}^{N-1} (N-k) * rho^k]
# Small-N identities (derived analytically):
#   f(2, rho) = 1 + rho
#   f(3, rho) = (3 + 4*rho + 2*rho^2) / 3
#   f(N, 0)   = 1  for all N  (no-op boundary)


def test_variance_inflation_factor_n2_rho05():
    # f(2, 0.5) = 1 + 0.5 = 1.5
    assert variance_inflation_factor(2, 0.5) == pytest.approx(1.5)


def test_variance_inflation_factor_n3_rho05():
    # f(3, 0.5) = (3 + 4*0.5 + 2*0.25) / 3 = (3 + 2 + 0.5) / 3 = 5.5 / 3
    assert variance_inflation_factor(3, 0.5) == pytest.approx(5.5 / 3)


def test_variance_inflation_factor_rho_zero_is_noop():
    # With no autocorrelation the inflated variance must equal the independence sum.
    for n in (1, 5, 10, 30):
        assert variance_inflation_factor(n, 0.0) == pytest.approx(1.0), f"n={n}"


def test_variance_inflation_factor_rho_zero_n1():
    # N=1: only one day, no lag-1 pair, factor is always 1.
    assert variance_inflation_factor(1, 0.5) == pytest.approx(1.0)


def test_monthly_total_moments_rho_zero_is_noop():
    # Default rho=0 must reproduce the pre-existing independence result exactly.
    m0, v0 = monthly_total_moments(
        observed_total=0.5,
        forecast_daily=[[0.1, 0.2]],
        clim_daily_mean=0.10,
        clim_daily_var=0.03,
        n_tail_days=20,
        floor=0.001,
        rho=0.0,
    )
    m1, v1 = monthly_total_moments(
        observed_total=0.5,
        forecast_daily=[[0.1, 0.2]],
        clim_daily_mean=0.10,
        clim_daily_var=0.03,
        n_tail_days=20,
        floor=0.001,
    )
    assert m0 == pytest.approx(m1)
    assert v0 == pytest.approx(v1)


def test_monthly_total_moments_rho_inflates_variance():
    # rho=0.5 with 20 climatology tail days must produce a strictly wider variance
    # than rho=0. The expected inflation factor on the tail contribution is
    # variance_inflation_factor(20, 0.5); the overall variance increases.
    _, v_base = monthly_total_moments(
        observed_total=0.0,
        forecast_daily=[],
        clim_daily_mean=0.10,
        clim_daily_var=0.04,
        n_tail_days=20,
        floor=0.001,
        rho=0.0,
    )
    _, v_inflated = monthly_total_moments(
        observed_total=0.0,
        forecast_daily=[],
        clim_daily_mean=0.10,
        clim_daily_var=0.04,
        n_tail_days=20,
        floor=0.001,
        rho=0.5,
    )
    # Hand-computed: variance_inflation_factor(20, 0.5) = 2.8000001907348633
    # v_base = 20 * 0.04 = 0.8; v_inflated = 0.8 * 2.8000001907 = 2.2400001525...
    assert v_inflated == pytest.approx(2.2400001525878905, rel=1e-6)
    assert v_inflated > v_base


# ---------------------------------------------------------------------------
# _lag1_from_dated_series: the production fitter (issue #88)
# ---------------------------------------------------------------------------


def test_lag1_from_dated_series_year_boundary_gap_skipped():
    # Three values: one dry day at year-end, then two wet consecutive days in the
    # next June. The year-boundary pair (2024-06-30 -> 2025-06-01) spans 366 days
    # and must be excluded by the date-diff guard.
    #
    # With the guard: only the valid pair (2025-06-01 -> 2025-06-02) contributes.
    # Both values (1.0) are above the series mean (2/3), so the cross-product is
    # positive -> rho = 0.5.
    #
    # Without the guard (mutation a): the year-boundary pair is also included. Its
    # cross-product (0.0 - mean) * (1.0 - mean) is negative, which drives the mean
    # covariance below zero and the result back to 0.0.
    #
    # This test pins the guard: rho = 0.5 iff the guard is present, 0.0 otherwise.
    series = [
        (date(2024, 6, 30), 0.0),
        (date(2025, 6, 1), 1.0),
        (date(2025, 6, 2), 1.0),
    ]
    assert _lag1_from_dated_series(series) == pytest.approx(0.5)


def test_lag1_from_dated_series_positive_rho():
    # A 20-day block pattern has strong positive lag-1 autocorrelation.
    # All pairs are consecutive-day so the guard does not suppress them.
    from datetime import timedelta

    base = date(2025, 6, 1)
    # 10 dry days followed by 10 wet days: clear wet-spell structure
    series = [(base + timedelta(days=i), 0.0 if i < 10 else 1.0) for i in range(20)]
    rho = _lag1_from_dated_series(series)
    assert rho > 0.3


def test_lag1_from_dated_series_skips_gap_keeps_valid_pairs():
    # A series spanning two Junes with a year gap in the middle.
    # The year-boundary pair is skipped; the pairs within each June still
    # contribute, so rho is non-zero when within-June structure is positive.
    from datetime import timedelta

    base1 = date(2024, 6, 1)
    june_2024 = [(base1 + timedelta(days=i), 0.0 if i < 15 else 1.0) for i in range(30)]
    base2 = date(2025, 6, 1)
    june_2025 = [(base2 + timedelta(days=i), 0.0 if i < 15 else 1.0) for i in range(30)]
    combined = june_2024 + june_2025
    rho = _lag1_from_dated_series(combined)
    assert rho > 0.3


def test_lag1_from_dated_series_constant_returns_zero():
    from datetime import timedelta

    base = date(2025, 6, 1)
    series = [(base + timedelta(days=i), 0.5) for i in range(30)]
    assert _lag1_from_dated_series(series) == pytest.approx(0.0)


def test_lag1_from_dated_series_short_returns_zero():
    assert _lag1_from_dated_series([]) == pytest.approx(0.0)
    assert _lag1_from_dated_series([(date(2025, 6, 1), 1.0)]) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Build-level: var widens end-to-end when rho > 0 (issue #88)
# ---------------------------------------------------------------------------


def test_build_precip_forecast_set_var_widens_with_positive_rho(httpx_mock, monkeypatch):
    # Monkeypatch fetch_precip_climatology to return rho=0.4 vs rho=0.0 in two
    # separate calls. The second call (rho=0.4) must produce a strictly wider
    # forecast variance, proving the rho path is wired end-to-end.
    import rainmaker.forecasts.precip as precip_mod

    _call_count = [0]
    _rho_values = [0.0, 0.4]

    def _fake_climatology(ghcnd_id, month, year, client, *, lookback_years):
        rho = _rho_values[_call_count[0] % 2]
        _call_count[0] += 1
        return (0.12, 0.04, rho)

    monkeypatch.setattr(precip_mod, "fetch_precip_climatology", _fake_climatology)

    def _setup_mock():
        daily = json.loads((FIXTURES / "ncei_daily_precip_nyc.json").read_text())
        # observed-to-date only (climatology call is monkeypatched)
        httpx_mock.add_response(url=re.compile(re.escape(NCEI_URL)), json=daily)
        httpx_mock.add_response(url=re.compile(re.escape(FORECAST_URL)), json=_multimodel())
        for _ in range(3):
            httpx_mock.add_response(url=re.compile(re.escape(ENSEMBLE_URL)), json=_ensemble())
        httpx_mock.add_response(
            url="https://api.weather.gov/points/40.779,-73.9692",
            json={
                "properties": {"forecastGridData": "https://api.weather.gov/gridpoints/OKX/34,45"}
            },
        )
        httpx_mock.add_response(
            url="https://api.weather.gov/gridpoints/OKX/34,45",
            json=json.loads((FIXTURES / "nws_qpf_nyc.json").read_text()),
        )

    _setup_mock()
    with httpx.Client() as client:
        fs_rho0 = build_precip_forecast_set(
            _nyc_target(),
            today=date(2026, 6, 6),
            client=client,
            var_floor=0.01,
            lookback_years=20,
        )

    # Reset mock and set up again for the rho=0.4 run
    httpx_mock.reset()
    _setup_mock()
    with httpx.Client() as client:
        fs_rho_pos = build_precip_forecast_set(
            _nyc_target(),
            today=date(2026, 6, 6),
            client=client,
            var_floor=0.01,
            lookback_years=20,
        )

    assert fs_rho_pos.var > fs_rho0.var
