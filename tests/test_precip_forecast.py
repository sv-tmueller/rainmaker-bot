import json
import re
from datetime import date
from pathlib import Path

import httpx
import pytest

from rainmaker.backfill import NCEI_URL
from rainmaker.forecasts.openmeteo import ENSEMBLE_URL, FORECAST_URL
from rainmaker.forecasts.precip import (
    build_precip_forecast_set,
    monthly_total_moments,
    parse_nws_qpf,
    parse_precip_open_meteo,
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
