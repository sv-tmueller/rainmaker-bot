"""TDD tests for per-venue calibration actuals routing in backfill.py.

Polymarket stations (in ICAO_TO_ASOS_STATION) -> ASOS (Iowa State Mesonet).
Kalshi-only stations (not in ICAO_TO_ASOS_STATION: KNYC, KMDW) -> NCEI (unchanged).

One batched ASOS request per station (not per day): the calibration window is ~45 days,
so per-day fan-out would cause 429s (fixed in #171 for settlement; same fix required here).
"""

import re
from datetime import date
from pathlib import Path

import httpx

from rainmaker.backfill import (
    HISTORICAL_FORECAST_URL,
    NCEI_URL,
    PREVIOUS_RUNS_URL,
    run_backfill,
    run_backfill_accuracy,
)
from rainmaker.config import KALSHI_STATIONS, STATIONS
from rainmaker.forecasts.asos import MESONET_ASOS_URL

FIXTURES = Path(__file__).parent / "fixtures"

# Polymarket station: LaGuardia (in ICAO_TO_ASOS_STATION -> ASOS path)
KLGA = STATIONS["NYC"]

# Kalshi-only stations: KNYC (Central Park) and KMDW (Midway) - NOT in ICAO_TO_ASOS_STATION
KNYC = KALSHI_STATIONS["NYC"]
KMDW = KALSHI_STATIONS["Chicago"]


def _hist_fixture():
    import json

    return json.loads((FIXTURES / "openmeteo_hist_multimodel_klga.json").read_text())


def _asos_fixture() -> str:
    return (FIXTURES / "mesonet_asos_klga_2026-03-01_05.csv").read_text()


def _ncei_fixture():
    import json

    return json.loads((FIXTURES / "ncei_actuals_klga.json").read_text())


def _previous_runs_fixture():
    import json

    return json.loads((FIXTURES / "openmeteo_previous_runs_klga.json").read_text())


# ---------------------------------------------------------------------------
# run_backfill: Polymarket station (KLGA) uses ASOS, not NCEI
# ---------------------------------------------------------------------------


def test_run_backfill_polymarket_station_fetches_asos_not_ncei(httpx_mock):
    """KLGA (in ICAO_TO_ASOS_STATION) -> ASOS path; NCEI must not be called."""
    httpx_mock.add_response(
        url=re.compile(re.escape(HISTORICAL_FORECAST_URL)),
        json=_hist_fixture(),
    )
    httpx_mock.add_response(
        url=re.compile(re.escape(MESONET_ASOS_URL)),
        text=_asos_fixture(),
    )
    with httpx.Client() as client:
        cal, acc = run_backfill(KLGA, "TMAX", 1, date(2026, 3, 1), date(2026, 3, 5), client)

    # Verify ASOS was called, NCEI was not
    all_requests = httpx_mock.get_requests()
    asos_requests = [r for r in all_requests if MESONET_ASOS_URL in str(r.url)]
    ncei_requests = [r for r in all_requests if NCEI_URL in str(r.url)]
    assert len(asos_requests) == 1, f"expected 1 ASOS request, got {len(asos_requests)}"
    assert len(ncei_requests) == 0, f"expected 0 NCEI requests, got {len(ncei_requests)}"

    # Calibration output is valid
    assert cal.station == "KLGA"
    assert cal.variable == "TMAX"
    assert cal.n_samples >= 1


def test_run_backfill_asos_request_spans_full_window_not_per_day(httpx_mock):
    """The ASOS request parameters span start->end, not one-per-day."""
    httpx_mock.add_response(
        url=re.compile(re.escape(HISTORICAL_FORECAST_URL)),
        json=_hist_fixture(),
    )
    httpx_mock.add_response(
        url=re.compile(re.escape(MESONET_ASOS_URL)),
        text=_asos_fixture(),
    )
    with httpx.Client() as client:
        run_backfill(KLGA, "TMAX", 1, date(2026, 3, 1), date(2026, 3, 5), client)

    asos_requests = [r for r in httpx_mock.get_requests() if MESONET_ASOS_URL in str(r.url)]
    assert len(asos_requests) == 1
    params = dict(asos_requests[0].url.params)
    # The request window must cover start and end, not be per-day
    assert params["year1"] == "2026"
    assert params["month1"] == "3"
    assert params["day1"] == "1"
    assert params["year2"] == "2026"
    assert params["month2"] == "3"
    assert params["day2"] == "5"


def test_run_backfill_kalshi_only_station_knyc_fetches_ncei_not_asos(httpx_mock):
    """KNYC (NOT in ICAO_TO_ASOS_STATION) -> NCEI path; ASOS must not be called."""
    hist_knyc = {
        "daily": {
            "time": ["2026-03-01", "2026-03-02"],
            "temperature_2m_max_gfs_seamless": [43.0, 34.0],
            "temperature_2m_max_ecmwf_ifs025": [41.0, 32.0],
        }
    }
    ncei_rows = [
        {"DATE": "2026-03-01", "STATION": KNYC.ghcnd_id, "TMAX": "45"},
        {"DATE": "2026-03-02", "STATION": KNYC.ghcnd_id, "TMAX": "36"},
    ]
    httpx_mock.add_response(
        url=re.compile(re.escape(HISTORICAL_FORECAST_URL)),
        json=hist_knyc,
    )
    httpx_mock.add_response(
        url=re.compile(re.escape(NCEI_URL)),
        json=ncei_rows,
    )
    with httpx.Client() as client:
        cal, acc = run_backfill(KNYC, "TMAX", 1, date(2026, 3, 1), date(2026, 3, 2), client)

    all_requests = httpx_mock.get_requests()
    asos_requests = [r for r in all_requests if MESONET_ASOS_URL in str(r.url)]
    ncei_requests = [r for r in all_requests if NCEI_URL in str(r.url)]
    assert len(asos_requests) == 0, f"expected 0 ASOS requests for KNYC, got {len(asos_requests)}"
    assert len(ncei_requests) == 1, f"expected 1 NCEI request for KNYC, got {len(ncei_requests)}"
    assert cal.station == "KNYC"
    assert cal.n_samples == 2


def test_run_backfill_kalshi_only_station_kmdw_fetches_ncei(httpx_mock):
    """KMDW (NOT in ICAO_TO_ASOS_STATION) -> NCEI path."""
    hist_kmdw = {
        "daily": {
            "time": ["2026-03-01"],
            "temperature_2m_max_gfs_seamless": [38.0],
            "temperature_2m_max_ecmwf_ifs025": [36.0],
        }
    }
    ncei_rows = [{"DATE": "2026-03-01", "STATION": KMDW.ghcnd_id, "TMAX": "40"}]
    httpx_mock.add_response(
        url=re.compile(re.escape(HISTORICAL_FORECAST_URL)),
        json=hist_kmdw,
    )
    httpx_mock.add_response(url=re.compile(re.escape(NCEI_URL)), json=ncei_rows)
    with httpx.Client() as client:
        cal, _ = run_backfill(KMDW, "TMAX", 1, date(2026, 3, 1), date(2026, 3, 1), client)

    all_requests = httpx_mock.get_requests()
    asos_requests = [r for r in all_requests if MESONET_ASOS_URL in str(r.url)]
    assert len(asos_requests) == 0
    assert cal.station == "KMDW"


# ---------------------------------------------------------------------------
# run_backfill_accuracy: same routing (higher leads, ASOS for Polymarket)
# ---------------------------------------------------------------------------


def test_run_backfill_accuracy_polymarket_station_fetches_asos(httpx_mock):
    """run_backfill_accuracy also routes KLGA to ASOS, not NCEI."""
    httpx_mock.add_response(
        url=re.compile(re.escape(PREVIOUS_RUNS_URL)),
        json=_previous_runs_fixture(),
    )
    httpx_mock.add_response(
        url=re.compile(re.escape(MESONET_ASOS_URL)),
        text=_asos_fixture(),
    )
    with httpx.Client() as client:
        accs = run_backfill_accuracy(
            KLGA, "TMAX", (2, 3), date(2026, 3, 1), date(2026, 3, 5), client
        )

    all_requests = httpx_mock.get_requests()
    asos_requests = [r for r in all_requests if MESONET_ASOS_URL in str(r.url)]
    ncei_requests = [r for r in all_requests if NCEI_URL in str(r.url)]
    assert len(asos_requests) == 1
    assert len(ncei_requests) == 0
    assert set(accs) == {2, 3}


def test_run_backfill_accuracy_kalshi_only_station_fetches_ncei(httpx_mock):
    """run_backfill_accuracy routes KNYC to NCEI, not ASOS."""
    prev_fixture = _previous_runs_fixture()
    ncei_rows = [
        {"DATE": "2026-03-01", "STATION": KNYC.ghcnd_id, "TMAX": "45"},
        {"DATE": "2026-03-02", "STATION": KNYC.ghcnd_id, "TMAX": "36"},
    ]
    httpx_mock.add_response(
        url=re.compile(re.escape(PREVIOUS_RUNS_URL)),
        json=prev_fixture,
    )
    httpx_mock.add_response(url=re.compile(re.escape(NCEI_URL)), json=ncei_rows)
    with httpx.Client() as client:
        run_backfill_accuracy(KNYC, "TMAX", (2,), date(2026, 3, 1), date(2026, 3, 2), client)

    all_requests = httpx_mock.get_requests()
    asos_requests = [r for r in all_requests if MESONET_ASOS_URL in str(r.url)]
    ncei_requests = [r for r in all_requests if NCEI_URL in str(r.url)]
    assert len(asos_requests) == 0
    assert len(ncei_requests) == 1
