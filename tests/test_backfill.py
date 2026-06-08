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
    PREVIOUS_RUNS_URL,
    build_pairs,
    fetch_actuals,
    fetch_historical_forecasts,
    fetch_historical_point_forecasts,
    fetch_monthly_precip,
    run_backfill,
    run_backfill_accuracy,
)
from rainmaker.cli import _backfill
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


_HIST_MIN = {
    "daily": {
        "time": ["2026-03-01", "2026-03-02"],
        "temperature_2m_min_gfs_seamless": [40.0, 32.0],
        "temperature_2m_min_ecmwf_ifs025": [38.0, 30.0],
    }
}


def test_fetch_historical_forecasts_requests_min_field_for_tmin(httpx_mock):
    httpx_mock.add_response(url=re.compile(re.escape(HISTORICAL_FORECAST_URL)), json=_HIST_MIN)
    with httpx.Client() as client:
        forecasts = fetch_historical_forecasts(
            KLGA, date(2026, 3, 1), date(2026, 3, 2), client, "TMIN"
        )
    assert "daily=temperature_2m_min" in str(httpx_mock.get_requests()[0].url)
    # The min model keys were parsed: 40/38 -> mean 39 on the first date.
    assert forecasts[date(2026, 3, 1)].mu == pytest.approx(39.0)


def test_fetch_monthly_precip_reads_gsom_inches(httpx_mock):
    gsom = json.loads((FIXTURES / "ncei_gsom_precip_nyc.json").read_text())
    httpx_mock.add_response(url=re.compile(re.escape(NCEI_URL)), json=gsom)
    with httpx.Client() as client:
        value = fetch_monthly_precip("USW00094728", 2026, 6, client)
    assert value == pytest.approx(4.10)


def test_fetch_monthly_precip_none_when_unpublished(httpx_mock):
    httpx_mock.add_response(url=re.compile(re.escape(NCEI_URL)), json=[])
    with httpx.Client() as client:
        assert fetch_monthly_precip("USW00094728", 2026, 6, client) is None


def test_run_backfill_tmin_pairs_min_forecast_with_tmin_actual(httpx_mock):
    rows = [
        {"DATE": "2026-03-01", "STATION": KLGA.ghcnd_id, "TMIN": "39"},
        {"DATE": "2026-03-02", "STATION": KLGA.ghcnd_id, "TMIN": "31"},
    ]
    httpx_mock.add_response(url=re.compile(re.escape(HISTORICAL_FORECAST_URL)), json=_HIST_MIN)
    httpx_mock.add_response(url=re.compile(re.escape(NCEI_URL)), json=rows)
    with httpx.Client() as client:
        cal, acc = run_backfill(KLGA, "TMIN", 1, date(2026, 3, 1), date(2026, 3, 2), client)
    assert cal.variable == "TMIN"
    assert cal.n_samples == 2
    assert acc.n == 2


def _previous_runs_fixture() -> dict[str, Any]:
    return json.loads((FIXTURES / "openmeteo_previous_runs_klga.json").read_text())


def test_fetch_historical_point_forecasts_reduces_hourly_to_daily_mean(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(re.escape(PREVIOUS_RUNS_URL)), json=_previous_runs_fixture()
    )
    with httpx.Client() as client:
        point = fetch_historical_point_forecasts(
            KLGA, (2, 3), date(2026, 3, 1), date(2026, 3, 2), client
        )
    assert point[2] == {
        date(2026, 3, 1): pytest.approx(49.0),
        date(2026, 3, 2): pytest.approx(37.0),
    }
    assert point[3] == {
        date(2026, 3, 1): pytest.approx(45.0),
        date(2026, 3, 2): pytest.approx(33.5),
    }
    req = httpx_mock.get_requests()[0]
    assert "hourly=temperature_2m_previous_day2" in str(req.url)
    assert "previous_day3" in str(req.url)
    assert "models=" in str(req.url)


def test_run_backfill_accuracy_scores_each_lead(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(re.escape(PREVIOUS_RUNS_URL)), json=_previous_runs_fixture()
    )
    httpx_mock.add_response(url=re.compile(re.escape(NCEI_URL)), json=_actuals_fixture())
    with httpx.Client() as client:
        accs = run_backfill_accuracy(
            KLGA, "TMAX", (2, 3), date(2026, 3, 1), date(2026, 3, 2), client
        )
    assert set(accs) == {2, 3}
    # lead 2: mu 49.0 vs 43 (+6), 37.0 vs 34 (+3) -> bias 4.5, mae 4.5
    assert accs[2].n == 2
    assert accs[2].bias_f == pytest.approx(4.5)
    assert accs[2].mae_f == pytest.approx(4.5)
    # lead 3: mu 45.0 vs 43 (+2), 33.5 vs 34 (-0.5) -> bias 0.75, mae 1.25
    assert accs[3].n == 2
    assert accs[3].bias_f == pytest.approx(0.75)
    assert accs[3].mae_f == pytest.approx(1.25)


def test_fetch_historical_point_forecasts_uses_min_for_tmin(httpx_mock):
    data = {
        "hourly": {
            "time": ["2026-03-01T06:00", "2026-03-01T12:00"],
            "temperature_2m_previous_day2_gfs_seamless": [30.0, 41.0],
            "temperature_2m_previous_day2_ecmwf_ifs025": [32.0, 39.0],
        }
    }
    httpx_mock.add_response(url=re.compile(re.escape(PREVIOUS_RUNS_URL)), json=data)
    with httpx.Client() as client:
        point = fetch_historical_point_forecasts(
            KLGA, (2,), date(2026, 3, 1), date(2026, 3, 1), client, "TMIN"
        )
    # min reduction: gfs min 30, ecmwf min 32 -> mean 31.0
    assert point[2] == {date(2026, 3, 1): pytest.approx(31.0)}
    assert "daily=" not in str(httpx_mock.get_requests()[0].url)


# 'NYC' now resolves to two settlement stations (LaGuardia and Kalshi's Central
# Park), so the same mocked endpoints are hit once per station; allow reuse.
@pytest.mark.httpx_mock(can_send_already_matched_responses=True)
def test_backfill_cli_saves_a_backtest_row_per_lead(httpx_mock, tmp_path, monkeypatch):
    import rainmaker.cli as cli

    httpx_mock.add_response(
        url=re.compile(re.escape(HISTORICAL_FORECAST_URL)), json=_hist_fixture()
    )
    httpx_mock.add_response(
        url=re.compile(re.escape(PREVIOUS_RUNS_URL)), json=_previous_runs_fixture()
    )
    # NCEI is hit twice: once for lead 1 (run_backfill), once for leads 2-3 (run_backfill_accuracy)
    httpx_mock.add_response(url=re.compile(re.escape(NCEI_URL)), json=_actuals_fixture())
    httpx_mock.add_response(url=re.compile(re.escape(NCEI_URL)), json=_actuals_fixture())
    monkeypatch.setattr(cli, "_today", lambda: date(2026, 3, 6))
    db = str(tmp_path / "t.db")

    _backfill("NYC", "TMAX", 5, (1, 2, 3), db)

    from rainmaker.store.db import connect

    conn = connect(db)
    rows = conn.execute(
        "SELECT lead_time, kind, n FROM forecast_accuracy "
        "WHERE station = 'KLGA' AND variable = 'TMAX' ORDER BY lead_time"
    ).fetchall()
    knyc = conn.execute(
        "SELECT count(*) AS n FROM forecast_accuracy WHERE station = 'KNYC' AND variable = 'TMAX'"
    ).fetchone()["n"]
    conn.close()
    leads = sorted(r[0] for r in rows)
    assert leads == [1, 2, 3]
    assert all(r[1] == "backtest" for r in rows)
    assert knyc == 3  # the Kalshi Central Park station is backfilled alongside LaGuardia
