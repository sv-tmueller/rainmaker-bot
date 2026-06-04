import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest

from rainmaker.backfill import (
    HISTORICAL_FORECAST_URL,
    NCEI_URL,
    build_pairs,
    fetch_actuals,
    fetch_historical_forecasts,
    run_backfill,
)
from rainmaker.config import STATIONS
from rainmaker.probability.calibration import CalibrationPair
from rainmaker.probability.distribution import Gaussian

FIXTURES = Path(__file__).parent / "fixtures"
KLGA = STATIONS["NYC"]


def _actuals_fixture() -> list[dict[str, Any]]:
    return json.loads((FIXTURES / "ncei_actuals_klga.json").read_text())


def _hist_fixture() -> dict[str, Any]:
    return json.loads((FIXTURES / "openmeteo_hist_multimodel_klga.json").read_text())


def test_fetch_actuals_parses_daily_max(httpx_mock):
    httpx_mock.add_response(url=re.compile(re.escape(NCEI_URL)), json=_actuals_fixture())
    with httpx.Client() as client:
        actuals = fetch_actuals(KLGA.ghcnd_id, date(2026, 3, 1), date(2026, 3, 5), client)
    assert actuals == {
        date(2026, 3, 1): 43.0,
        date(2026, 3, 2): 34.0,
        date(2026, 3, 3): 35.0,
        date(2026, 3, 4): 50.0,
        date(2026, 3, 5): 45.0,
    }


def test_fetch_actuals_skips_missing_tmax(httpx_mock):
    rows = [
        {"DATE": "2026-03-01", "STATION": KLGA.ghcnd_id, "TMAX": "43"},
        {"DATE": "2026-03-02", "STATION": KLGA.ghcnd_id, "TMAX": ""},
    ]
    httpx_mock.add_response(url=re.compile(re.escape(NCEI_URL)), json=rows)
    with httpx.Client() as client:
        actuals = fetch_actuals(KLGA.ghcnd_id, date(2026, 3, 1), date(2026, 3, 2), client)
    assert actuals == {date(2026, 3, 1): 43.0}


def test_fetch_historical_forecasts_builds_gaussian_from_model_spread(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(re.escape(HISTORICAL_FORECAST_URL)), json=_hist_fixture()
    )
    with httpx.Client() as client:
        forecasts = fetch_historical_forecasts(KLGA, date(2026, 3, 1), date(2026, 3, 5), client)
    assert set(forecasts) == {date(2026, 3, i) for i in range(1, 6)}
    # 2026-03-01 models: 43.6, 37.6, 41.2, 39.1, 38.8 -> mean 40.06, sample stdev 2.366
    g = forecasts[date(2026, 3, 1)]
    assert g.mu == pytest.approx(40.06)
    assert g.sigma == pytest.approx(2.366, abs=1e-3)


def test_fetch_historical_forecasts_skips_dates_with_one_model(httpx_mock):
    data = {"daily": {"time": ["2026-03-01"], "temperature_2m_max_gfs_seamless": [40.0]}}
    httpx_mock.add_response(url=re.compile(re.escape(HISTORICAL_FORECAST_URL)), json=data)
    with httpx.Client() as client:
        forecasts = fetch_historical_forecasts(KLGA, date(2026, 3, 1), date(2026, 3, 1), client)
    assert forecasts == {}  # one model is not enough to estimate a spread


def test_build_pairs_joins_forecasts_and_actuals_on_date():
    forecasts = {
        date(2026, 3, 1): Gaussian(mu=40.0, sigma=2.0),
        date(2026, 3, 2): Gaussian(mu=32.0, sigma=2.0),  # no actual -> dropped
        date(2026, 3, 3): Gaussian(mu=35.0, sigma=2.0),
    }
    actuals = {date(2026, 3, 1): 43.0, date(2026, 3, 3): 35.0}
    pairs = build_pairs(forecasts, actuals)
    assert pairs == [
        CalibrationPair(mu=40.0, sigma=2.0, actual=43.0),
        CalibrationPair(mu=35.0, sigma=2.0, actual=35.0),
    ]


def test_run_backfill_fits_calibration_and_accuracy_from_history(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(re.escape(HISTORICAL_FORECAST_URL)), json=_hist_fixture()
    )
    httpx_mock.add_response(url=re.compile(re.escape(NCEI_URL)), json=_actuals_fixture())
    with httpx.Client() as client:
        cal, acc = run_backfill(KLGA, "TMAX", 1, date(2026, 3, 1), date(2026, 3, 5), client)
    assert cal.station == "KLGA"
    assert cal.variable == "TMAX"
    assert cal.lead_time == 1
    assert cal.n_samples == 5
    # forecasts run cold across the window (mean signed error mu - actual is negative)
    assert cal.bias == pytest.approx(-2.38, abs=1e-2)
    assert cal.spread_scale > 0
    # accuracy is measured over the same pairs
    assert acc.n == 5
    assert acc.bias_f == pytest.approx(cal.bias)
    assert acc.mae_f >= abs(acc.bias_f)  # mean |e| always >= |mean e|
    assert acc.mae_f > 0


def test_fetch_actuals_reads_tmin_when_asked(httpx_mock):
    rows = [{"DATE": "2026-03-01", "STATION": "X", "TMIN": "29"}]
    httpx_mock.add_response(url=re.compile(re.escape(NCEI_URL)), json=rows)
    with httpx.Client() as client:
        actuals = fetch_actuals("X", date(2026, 3, 1), date(2026, 3, 1), client, "TMIN")
    assert actuals == {date(2026, 3, 1): 29.0}
