"""TDD tests for the ASOS production actuals source.

Tests drive the new src/rainmaker/forecasts/asos.py module.
All tests use fixtures, no live endpoints.
"""

import re
from datetime import date
from pathlib import Path

import httpx
import pytest

from rainmaker.forecasts.asos import (
    ICAO_TO_ASOS_STATION,
    MESONET_ASOS_URL,
    fetch_asos_daily_extreme,
)

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# ICAO_TO_ASOS_STATION mapping
# ---------------------------------------------------------------------------


def test_icao_to_asos_station_has_all_11_cities():
    """All 11 Polymarket cities must be mapped."""
    expected_icao = {
        "KLGA",
        "KMIA",
        "KORD",
        "KDAL",
        "KHOU",
        "KLAX",
        "KSFO",
        "KSEA",
        "KAUS",
        "KATL",
        "KBKF",
    }
    assert set(ICAO_TO_ASOS_STATION.keys()) == expected_icao


def test_icao_to_asos_station_drops_k_prefix():
    """ASOS codes are 3-letter FAA codes (no K prefix for US stations)."""
    for _icao, asos in ICAO_TO_ASOS_STATION.items():
        assert len(asos) == 3
        assert not asos.startswith("K")


# ---------------------------------------------------------------------------
# fetch_asos_daily_extreme: parse CSV fixture -> daily max/min
# ---------------------------------------------------------------------------


def test_fetch_asos_tmax_from_fixture(httpx_mock):
    """TMAX: parse the March 2026 KLGA fixture, get the daily high."""
    fixture = (FIXTURES / "mesonet_asos_klga_2026-03-02.csv").read_text()
    httpx_mock.add_response(
        url=re.compile(re.escape(MESONET_ASOS_URL)),
        text=fixture,
    )
    with httpx.Client() as client:
        result = fetch_asos_daily_extreme(
            "LGA",
            date(2026, 3, 2),
            date(2026, 3, 2),
            client,
            "TMAX",
        )
    d = date(2026, 3, 2)
    assert d in result
    # From the fixture: max tmpc = 3.33C -> 3.33*9/5+32 = 37.994F
    assert result[d] == pytest.approx(3.33 * 9 / 5 + 32, abs=0.01)


def test_fetch_asos_tmin_from_fixture(httpx_mock):
    """TMIN: same fixture, get the daily low."""
    fixture = (FIXTURES / "mesonet_asos_klga_2026-03-02.csv").read_text()
    httpx_mock.add_response(
        url=re.compile(re.escape(MESONET_ASOS_URL)),
        text=fixture,
    )
    with httpx.Client() as client:
        result = fetch_asos_daily_extreme(
            "LGA",
            date(2026, 3, 2),
            date(2026, 3, 2),
            client,
            "TMIN",
        )
    d = date(2026, 3, 2)
    assert d in result
    # From the fixture: min tmpc = -5.56C -> -5.56*9/5+32 = 21.992F
    assert result[d] == pytest.approx(-5.56 * 9 / 5 + 32, abs=0.01)


def test_fetch_asos_multi_day_from_fixture(httpx_mock):
    """Multi-day fixture: two days returned, each with correct extremes."""
    fixture = (FIXTURES / "mesonet_asos_klga_2026-06.csv").read_text()
    httpx_mock.add_response(
        url=re.compile(re.escape(MESONET_ASOS_URL)),
        text=fixture,
    )
    with httpx.Client() as client:
        result = fetch_asos_daily_extreme(
            "LGA",
            date(2026, 6, 1),
            date(2026, 6, 2),
            client,
            "TMAX",
        )
    # June-01 max = 20.3C, June-02 max = 22.4C
    assert date(2026, 6, 1) in result
    assert date(2026, 6, 2) in result
    assert result[date(2026, 6, 1)] == pytest.approx(20.3 * 9 / 5 + 32, abs=0.01)
    assert result[date(2026, 6, 2)] == pytest.approx(22.4 * 9 / 5 + 32, abs=0.01)


def test_fetch_asos_skips_missing_values(httpx_mock):
    """Rows with tmpc == 'M' are silently skipped."""
    csv_text = "station,valid,tmpc\nLGA,2026-03-02 12:00,M\nLGA,2026-03-02 13:00,10.0\n"
    httpx_mock.add_response(
        url=re.compile(re.escape(MESONET_ASOS_URL)),
        text=csv_text,
    )
    with httpx.Client() as client:
        result = fetch_asos_daily_extreme(
            "LGA",
            date(2026, 3, 2),
            date(2026, 3, 2),
            client,
            "TMAX",
        )
    d = date(2026, 3, 2)
    assert d in result
    assert result[d] == pytest.approx(10.0 * 9 / 5 + 32, abs=0.01)


def test_fetch_asos_returns_empty_when_all_missing(httpx_mock):
    """Returns empty dict when all rows are missing."""
    csv_text = "station,valid,tmpc\nLGA,2026-03-02 12:00,M\n"
    httpx_mock.add_response(
        url=re.compile(re.escape(MESONET_ASOS_URL)),
        text=csv_text,
    )
    with httpx.Client() as client:
        result = fetch_asos_daily_extreme(
            "LGA",
            date(2026, 3, 2),
            date(2026, 3, 2),
            client,
            "TMAX",
        )
    assert result == {}


def test_fetch_asos_skips_comment_lines(httpx_mock):
    """Lines starting with '#' are comment/debug lines and must be skipped."""
    csv_text = "# This is a comment\nstation,valid,tmpc\nLGA,2026-03-02 12:00,15.0\n"
    httpx_mock.add_response(
        url=re.compile(re.escape(MESONET_ASOS_URL)),
        text=csv_text,
    )
    with httpx.Client() as client:
        result = fetch_asos_daily_extreme(
            "LGA",
            date(2026, 3, 2),
            date(2026, 3, 2),
            client,
            "TMAX",
        )
    assert date(2026, 3, 2) in result


def test_fetch_asos_raises_on_http_error(httpx_mock):
    """HTTP errors are propagated as httpx.HTTPStatusError."""
    httpx_mock.add_response(
        url=re.compile(re.escape(MESONET_ASOS_URL)),
        status_code=503,
    )
    with httpx.Client() as client:
        with pytest.raises(httpx.HTTPStatusError):
            fetch_asos_daily_extreme(
                "LGA",
                date(2026, 3, 2),
                date(2026, 3, 2),
                client,
                "TMAX",
            )


def test_fetch_asos_sends_correct_params(httpx_mock):
    """Verify the Mesonet request parameters are sent correctly."""
    fixture = (FIXTURES / "mesonet_asos_klga_2026-03-02.csv").read_text()
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, text=fixture)

    httpx_mock.add_callback(handler)
    with httpx.Client() as client:
        fetch_asos_daily_extreme(
            "LGA",
            date(2026, 3, 2),
            date(2026, 3, 2),
            client,
            "TMAX",
        )
    assert captured["params"]["station"] == "LGA"
    assert captured["params"]["data"] == "tmpc"
    assert captured["params"]["tz"] == "UTC"
    assert captured["params"]["format"] == "onlycomma"
