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
    fetch_historical_lead_forecasts,
    fetch_monthly_precip,
    run_backfill,
    season_window,
)
from rainmaker.cli import _backfill
from rainmaker.config import STATIONS
from rainmaker.forecasts.asos import MESONET_ASOS_URL
from rainmaker.probability.calibration import CalibrationPair
from rainmaker.probability.distribution import Gaussian

FIXTURES = Path(__file__).parent / "fixtures"
KLGA = STATIONS["NYC"]


def _actuals_fixture() -> list[dict[str, Any]]:
    return json.loads((FIXTURES / "ncei_actuals_klga.json").read_text())


def _hist_fixture() -> dict[str, Any]:
    return json.loads((FIXTURES / "openmeteo_hist_multimodel_klga.json").read_text())


def _previous_runs_fixture() -> dict[str, Any]:
    return json.loads((FIXTURES / "openmeteo_previous_runs_klga.json").read_text())


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
        CalibrationPair(mu=40.0, sigma=2.0, ensemble_var=4.0, actual=43.0),
        CalibrationPair(mu=35.0, sigma=2.0, ensemble_var=4.0, actual=35.0),
    ]


def test_run_backfill_fits_calibration_and_accuracy_per_lead(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(re.escape(PREVIOUS_RUNS_URL)), json=_previous_runs_fixture()
    )
    # KLGA is a Polymarket station (in ICAO_TO_ASOS_STATION): actuals come from ASOS.
    # 2 days of forecast history (03-01, 03-02); daily TMAX max of the two readings:
    # 03-01 -> 44.6F, 03-02 -> 35.6F.
    asos_csv = (
        "station,valid,tmpc\n"
        "LGA,2026-03-01 06:00,5.0\n"
        "LGA,2026-03-01 12:00,7.0\n"
        "LGA,2026-03-02 06:00,-1.0\n"
        "LGA,2026-03-02 12:00,2.0\n"
    )
    httpx_mock.add_response(url=re.compile(re.escape(MESONET_ASOS_URL)), text=asos_csv)
    with httpx.Client() as client:
        results = run_backfill(KLGA, "TMAX", (1, 2), date(2026, 3, 1), date(2026, 3, 2), client)
    assert set(results) == {1, 2}
    cal, acc = results[1]
    assert cal.station == "KLGA"
    assert cal.variable == "TMAX"
    assert cal.lead_time == 1
    assert cal.n_samples == 2
    assert cal.var_a >= 0.0
    assert cal.var_b >= 0.0
    assert acc.n == 2
    # lead 1 mu: 50.0 (03-01), 38.0 (03-02); errors vs 44.6/35.6 -> +5.4, +2.4
    assert acc.bias_f == pytest.approx(3.9)
    assert acc.mae_f == pytest.approx(3.9)
    cal2, acc2 = results[2]
    assert cal2.lead_time == 2
    assert acc2.n == 2
    # lead 2 mu: 49.0 (03-01), 37.0 (03-02); errors vs 44.6/35.6 -> +4.4, +1.4
    assert acc2.bias_f == pytest.approx(2.9)
    assert acc2.mae_f == pytest.approx(2.9)


def test_run_backfill_omits_leads_with_no_overlapping_actuals(httpx_mock):
    # Actuals only cover 03-01; the previous-runs fixture has forecasts for
    # 03-01 and 03-02 at every lead, so requesting a window that ends before
    # 03-02 leaves every lead with exactly one pair (not zero), which still fits.
    # To exercise "no overlapping actuals", ask for a lead absent from the fixture
    # (lead 9): fetch_historical_lead_forecasts returns an empty dict for it, so
    # build_pairs has nothing to join and the lead is omitted rather than erroring.
    httpx_mock.add_response(
        url=re.compile(re.escape(PREVIOUS_RUNS_URL)), json=_previous_runs_fixture()
    )
    asos_fixture = (FIXTURES / "mesonet_asos_klga_2026-03-01_05.csv").read_text()
    httpx_mock.add_response(url=re.compile(re.escape(MESONET_ASOS_URL)), text=asos_fixture)
    with httpx.Client() as client:
        results = run_backfill(KLGA, "TMAX", (1, 9), date(2026, 3, 1), date(2026, 3, 2), client)
    assert set(results) == {1}


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


_PREVIOUS_RUNS_MIN = {
    "hourly": {
        "time": ["2026-03-01T06:00", "2026-03-01T12:00", "2026-03-02T06:00", "2026-03-02T12:00"],
        "temperature_2m_previous_day1_gfs_seamless": [40.0, 45.0, 32.0, 36.0],
        "temperature_2m_previous_day1_ecmwf_ifs025": [38.0, 44.0, 30.0, 35.0],
    }
}


def test_run_backfill_tmin_pairs_min_forecast_with_tmin_actual(httpx_mock):
    # KLGA (Polymarket) -> ASOS. Two readings per day; TMIN uses the minimum.
    asos_csv = (
        "station,valid,tmpc\n"
        "LGA,2026-03-01 06:00,2.0\n"
        "LGA,2026-03-01 12:00,3.9\n"  # 3.9C -> 38.82F min is 2.0C -> 35.6F
        "LGA,2026-03-02 06:00,-0.556\n"
        "LGA,2026-03-02 12:00,1.0\n"  # min is -0.556C -> 30.999F ~ 31F
    )
    httpx_mock.add_response(url=re.compile(re.escape(PREVIOUS_RUNS_URL)), json=_PREVIOUS_RUNS_MIN)
    httpx_mock.add_response(url=re.compile(re.escape(MESONET_ASOS_URL)), text=asos_csv)
    with httpx.Client() as client:
        results = run_backfill(KLGA, "TMIN", (1,), date(2026, 3, 1), date(2026, 3, 2), client)
    cal, acc = results[1]
    assert cal.variable == "TMIN"
    assert cal.n_samples == 2
    assert acc.n == 2


def test_run_backfill_dispatches_tmin_cells_to_student_t(httpx_mock):
    """A TMIN cell fits through fit_student_t_free_df: df is set on the result."""
    asos_csv = (
        "station,valid,tmpc\n"
        "LGA,2026-03-01 06:00,2.0\n"
        "LGA,2026-03-01 12:00,3.9\n"
        "LGA,2026-03-02 06:00,-0.556\n"
        "LGA,2026-03-02 12:00,1.0\n"
    )
    httpx_mock.add_response(url=re.compile(re.escape(PREVIOUS_RUNS_URL)), json=_PREVIOUS_RUNS_MIN)
    httpx_mock.add_response(url=re.compile(re.escape(MESONET_ASOS_URL)), text=asos_csv)
    with httpx.Client() as client:
        results = run_backfill(KLGA, "TMIN", (1,), date(2026, 3, 1), date(2026, 3, 2), client)
    cal, _ = results[1]
    assert cal.variable == "TMIN"
    assert cal.df is not None
    assert 2.0 < cal.df <= 62.0


def test_run_backfill_keeps_tmax_cells_gaussian(httpx_mock):
    """A TMAX cell stays on the untouched Gaussian fit_calibration: df is None."""
    httpx_mock.add_response(
        url=re.compile(re.escape(PREVIOUS_RUNS_URL)), json=_previous_runs_fixture()
    )
    asos_csv = (
        "station,valid,tmpc\n"
        "LGA,2026-03-01 06:00,5.0\n"
        "LGA,2026-03-01 12:00,7.0\n"
        "LGA,2026-03-02 06:00,-1.0\n"
        "LGA,2026-03-02 12:00,2.0\n"
    )
    httpx_mock.add_response(url=re.compile(re.escape(MESONET_ASOS_URL)), text=asos_csv)
    with httpx.Client() as client:
        results = run_backfill(KLGA, "TMAX", (1, 2), date(2026, 3, 1), date(2026, 3, 2), client)
    for lead in (1, 2):
        cal, _ = results[lead]
        assert cal.variable == "TMAX"
        assert cal.df is None


def test_fetch_historical_lead_forecasts_builds_gaussian_per_lead(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(re.escape(PREVIOUS_RUNS_URL)), json=_previous_runs_fixture()
    )
    with httpx.Client() as client:
        by_lead = fetch_historical_lead_forecasts(
            KLGA, (0, 1, 2, 3), date(2026, 3, 1), date(2026, 3, 2), client
        )
    assert set(by_lead) == {0, 1, 2, 3}
    # lead 0 (day-0 archive, unsuffixed keys): 55/53 -> 54.0, 43/41 -> 42.0
    assert by_lead[0][date(2026, 3, 1)].mu == pytest.approx(54.0)
    assert by_lead[0][date(2026, 3, 1)].sigma == pytest.approx(1.414, abs=1e-3)
    assert by_lead[0][date(2026, 3, 2)].mu == pytest.approx(42.0)
    # lead 1: 51/49 -> 50.0, 39/37 -> 38.0
    assert by_lead[1][date(2026, 3, 1)].mu == pytest.approx(50.0)
    assert by_lead[1][date(2026, 3, 2)].mu == pytest.approx(38.0)
    # lead 2: 50/48 -> 49.0, 38/36 -> 37.0 (matches the values the old point-forecast
    # path used to compute for this fixture)
    assert by_lead[2][date(2026, 3, 1)].mu == pytest.approx(49.0)
    assert by_lead[2][date(2026, 3, 2)].mu == pytest.approx(37.0)
    # lead 3: 46/44 -> 45.0, 33.5/33.5 -> 33.5
    assert by_lead[3][date(2026, 3, 1)].mu == pytest.approx(45.0)
    assert by_lead[3][date(2026, 3, 2)].mu == pytest.approx(33.5)
    req = httpx_mock.get_requests()[0]
    # one request carries every requested lead's field
    assert "hourly=temperature_2m_previous_day0" in str(req.url)
    assert "previous_day1" in str(req.url)
    assert "previous_day2" in str(req.url)
    assert "previous_day3" in str(req.url)
    assert "models=" in str(req.url)


def test_fetch_historical_lead_forecasts_maps_lead_zero_to_unsuffixed_response_key(httpx_mock):
    # Lead 0 is requested as temperature_2m_previous_day0, but Open-Meteo normalizes
    # the response key to the un-suffixed temperature_2m_<model>.
    data = {
        "hourly": {
            "time": ["2026-03-01T00:00", "2026-03-01T12:00"],
            "temperature_2m_gfs_seamless": [40.0, 50.0],
            "temperature_2m_ecmwf_ifs025": [42.0, 48.0],
        }
    }
    httpx_mock.add_response(url=re.compile(re.escape(PREVIOUS_RUNS_URL)), json=data)
    with httpx.Client() as client:
        by_lead = fetch_historical_lead_forecasts(
            KLGA, (0,), date(2026, 3, 1), date(2026, 3, 1), client
        )
    assert by_lead[0][date(2026, 3, 1)].mu == pytest.approx(49.0)  # (50 + 48) / 2 daily max


def test_fetch_historical_lead_forecasts_uses_min_for_tmin(httpx_mock):
    data = {
        "hourly": {
            "time": ["2026-03-01T06:00", "2026-03-01T12:00"],
            "temperature_2m_previous_day2_gfs_seamless": [30.0, 41.0],
            "temperature_2m_previous_day2_ecmwf_ifs025": [32.0, 39.0],
        }
    }
    httpx_mock.add_response(url=re.compile(re.escape(PREVIOUS_RUNS_URL)), json=data)
    with httpx.Client() as client:
        by_lead = fetch_historical_lead_forecasts(
            KLGA, (2,), date(2026, 3, 1), date(2026, 3, 1), client, "TMIN"
        )
    # min reduction: gfs min 30, ecmwf min 32 -> mean 31.0
    assert by_lead[2][date(2026, 3, 1)].mu == pytest.approx(31.0)
    assert "daily=" not in str(httpx_mock.get_requests()[0].url)


def test_fetch_historical_lead_forecasts_skips_dates_with_one_model(httpx_mock):
    data = {
        "hourly": {
            "time": ["2026-03-01T00:00"],
            "temperature_2m_previous_day2_gfs_seamless": [40.0],
        }
    }
    httpx_mock.add_response(url=re.compile(re.escape(PREVIOUS_RUNS_URL)), json=data)
    with httpx.Client() as client:
        by_lead = fetch_historical_lead_forecasts(
            KLGA, (2,), date(2026, 3, 1), date(2026, 3, 1), client
        )
    assert by_lead[2] == {}  # one model is not enough to estimate a spread


def test_fetch_historical_lead_forecasts_raises_value_error_on_open_meteo_error_body(httpx_mock):
    # Open-Meteo returns 200 with {"error": true, "reason": "..."} for bad params.
    error_body = {"error": True, "reason": "Parameter out of range"}
    httpx_mock.add_response(url=re.compile(re.escape(PREVIOUS_RUNS_URL)), json=error_body)
    with httpx.Client() as client:
        with pytest.raises(ValueError, match="hourly"):
            fetch_historical_lead_forecasts(KLGA, (2,), date(2026, 3, 1), date(2026, 3, 1), client)


def test_fetch_historical_forecasts_raises_value_error_on_open_meteo_error_body(httpx_mock):
    # Same guard for the historical-forecast archive: 200 error body raises ValueError.
    error_body = {"error": True, "reason": "Parameter out of range"}
    httpx_mock.add_response(url=re.compile(re.escape(HISTORICAL_FORECAST_URL)), json=error_body)
    with httpx.Client() as client:
        with pytest.raises(ValueError, match="daily"):
            fetch_historical_forecasts(KLGA, date(2026, 3, 1), date(2026, 3, 5), client)


# 'NYC' resolves to two settlement stations (LaGuardia KLGA and Kalshi's Central
# Park KNYC). KLGA is a Polymarket station -> ASOS. KNYC is Kalshi-only -> NCEI.
# Each station makes one Previous Runs request (covering every requested lead)
# plus one actuals request.
@pytest.mark.httpx_mock(can_send_already_matched_responses=True)
def test_backfill_cli_saves_a_row_per_lead_and_station(httpx_mock, tmp_path, monkeypatch):
    import rainmaker.cli as cli

    httpx_mock.add_response(
        url=re.compile(re.escape(PREVIOUS_RUNS_URL)), json=_previous_runs_fixture()
    )
    asos_fixture = (FIXTURES / "mesonet_asos_klga_2026-03-01_05.csv").read_text()
    httpx_mock.add_response(url=re.compile(re.escape(MESONET_ASOS_URL)), text=asos_fixture)
    httpx_mock.add_response(url=re.compile(re.escape(NCEI_URL)), json=_actuals_fixture())
    monkeypatch.setattr(cli, "_today", lambda: date(2026, 3, 6))
    db = str(tmp_path / "t.db")

    _backfill("NYC", "TMAX", 5, (1, 2, 3), db)

    from rainmaker.store.db import connect

    conn = connect(db)
    rows = conn.execute(
        "SELECT lead_time, kind FROM forecast_accuracy "
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


# season_window tests


def test_season_window_mid_winter_returns_full_days_window():
    # 2026-01-20 is mid-winter (DJF). Season started 2025-12-01.
    # today - 45 days = 2025-12-06. Season start = 2025-12-01.
    # max(2025-12-06, 2025-12-01) = 2025-12-06 (not clipped by season start).
    result = season_window(date(2026, 1, 20), days=45)
    assert result is not None
    start, end = result
    assert end == date(2026, 1, 19)
    assert start == date(2025, 12, 6)  # today - 45 days; season start is earlier


def test_season_window_clipped_at_season_start_early_spring():
    # 2026-03-10 is 9 days into meteorological spring (MAM starts Mar 1).
    # end = 2026-03-09. 45 days back would be 2026-01-23 (winter).
    # Season start = 2026-03-01. Clip: start = 2026-03-01.
    result = season_window(date(2026, 3, 10), days=45)
    assert result is not None
    start, end = result
    assert end == date(2026, 3, 9)
    assert start == date(2026, 3, 1)  # clipped to spring start, not 45 days back


def test_season_window_day_one_of_season_returns_none():
    # 2026-03-01 is exactly the first day of spring (MAM).
    # end = 2026-02-28 (yesterday). Season start = 2026-03-01. start > end -> None.
    result = season_window(date(2026, 3, 1), days=45)
    assert result is None


def test_season_window_day_two_of_season_yields_one_day_window():
    # 2026-03-02. end = 2026-03-01. season_start = 2026-03-01. start = max(2026-02-15, 2026-03-01).
    # start = 2026-03-01, end = 2026-03-01 -> valid 1-day window.
    result = season_window(date(2026, 3, 2), days=45)
    assert result is not None
    start, end = result
    assert start == date(2026, 3, 1)
    assert end == date(2026, 3, 1)


def test_season_window_mid_summer_uses_full_days():
    # 2026-08-01 is 61 days into summer (JJA starts Jun 1). 45-day window fits within season.
    # end = 2026-07-31. today - 45 days = 2026-06-17. Season start = 2026-06-01.
    # max(2026-06-17, 2026-06-01) = 2026-06-17 (not clipped).
    result = season_window(date(2026, 8, 1), days=45)
    assert result is not None
    start, end = result
    assert end == date(2026, 7, 31)
    assert start == date(2026, 6, 17)


def test_season_window_first_day_of_winter_returns_none():
    # Winter (DJF) starts Dec 1. On 2025-12-01, end = 2025-11-30 (autumn). start > end -> None.
    result = season_window(date(2025, 12, 1), days=45)
    assert result is None


def test_season_window_respects_custom_days():
    # 2026-06-20 is 19 days into summer (JJA). days=30: end = 2026-06-19.
    # 30 days back = 2026-05-20 (spring). Season start = 2026-06-01.
    # Clip: start = 2026-06-01.
    result = season_window(date(2026, 6, 20), days=30)
    assert result is not None
    start, end = result
    assert start == date(2026, 6, 1)
    assert end == date(2026, 6, 19)


def test_season_window_clips_to_season_boundary_not_days_back():
    """Window start is the season boundary when days would reach into a prior season."""
    # 2026-03-30 is 29 days into spring (MAM starts Mar 1).
    # 60 days back would reach 2026-01-29 (winter).
    # season_window must clip to 2026-03-01, not go back 60 days.
    result = season_window(date(2026, 3, 30), days=60)
    assert result is not None
    start, end = result
    assert start == date(2026, 3, 1)  # season boundary, not today - 60 days
    assert end == date(2026, 3, 29)


def test_seasonal_fit_avoids_cross_season_bias():
    """Demonstrate that the seasonal clip prevents a winter bias from contaminating spring.

    Setup: synthetic pairs where winter data has a +5F bias and spring data is unbiased.
    A flat window spanning both seasons would fit ~+2.5F bias (wrong for spring).
    The season-clipped window fits ~0 (correct for spring).
    """
    from rainmaker.probability.calibration import CalibrationPair, fit_calibration

    sigma = 2.0
    # 30 winter pairs with +5F bias (mu runs warm vs actual)
    winter_pairs = [
        CalibrationPair(mu=70.0 + i, sigma=sigma, ensemble_var=sigma**2, actual=(70.0 + i) - 5.0)
        for i in range(30)
    ]
    # 30 spring pairs with 0 bias
    spring_pairs = [
        CalibrationPair(mu=60.0 + i, sigma=sigma, ensemble_var=sigma**2, actual=60.0 + i)
        for i in range(30)
    ]

    # Flat window: includes winter + spring -> bias near +2.5
    flat_cal = fit_calibration("KLGA", "TMAX", 1, winter_pairs + spring_pairs)
    assert flat_cal.bias == pytest.approx(2.5, abs=0.1)  # halfway between +5 and 0

    # Season-clipped window: spring only -> bias near 0
    seasonal_cal = fit_calibration("KLGA", "TMAX", 1, spring_pairs)
    assert seasonal_cal.bias == pytest.approx(0.0, abs=0.1)
