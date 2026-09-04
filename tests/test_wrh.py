"""Tests for the wrh/timeseries (Synoptic Data API) hourly fetcher."""

from __future__ import annotations

import json
import re
import time
from datetime import date
from pathlib import Path

import httpx
import pytest

from rainmaker.forecasts.wrh import (
    SYNOPTIC_API_URL,
    WRH_REFERER,
    fetch_wrh_hourly_extreme,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# ---------------------------------------------------------------------------
# Parser: max/min extraction and day bucketing
# ---------------------------------------------------------------------------


def test_tmax_extracts_daily_maximum_from_normal_fixture(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(re.escape(SYNOPTIC_API_URL)),
        json=_load("wrh_klga_normal.json"),
    )
    with httpx.Client() as client:
        result = fetch_wrh_hourly_extreme(
            "KLGA",
            date(2026, 8, 20),
            date(2026, 8, 21),
            client,
            "TMAX",
            timezone="America/New_York",
        )
    # Day 1: max of [72, 68, 85, 89, 82, 74] = 89.0
    # Day 2: max of [70, 66, 88, 91, 80, 72] = 91.0
    assert result == {date(2026, 8, 20): 89.0, date(2026, 8, 21): 91.0}


def test_tmin_extracts_daily_minimum_from_normal_fixture(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(re.escape(SYNOPTIC_API_URL)),
        json=_load("wrh_klga_normal.json"),
    )
    with httpx.Client() as client:
        result = fetch_wrh_hourly_extreme(
            "KLGA",
            date(2026, 8, 20),
            date(2026, 8, 21),
            client,
            "TMIN",
            timezone="America/New_York",
        )
    # Day 1: min of [72, 68, 85, 89, 82, 74] = 68.0
    # Day 2: min of [70, 66, 88, 91, 80, 72] = 66.0
    assert result == {date(2026, 8, 20): 68.0, date(2026, 8, 21): 66.0}


def test_default_variable_is_tmax(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(re.escape(SYNOPTIC_API_URL)),
        json=_load("wrh_klga_normal.json"),
    )
    with httpx.Client() as client:
        result = fetch_wrh_hourly_extreme(
            "KLGA",
            date(2026, 8, 20),
            date(2026, 8, 21),
            client,
            timezone="America/New_York",
        )
    assert result == {date(2026, 8, 20): 89.0, date(2026, 8, 21): 91.0}


# ---------------------------------------------------------------------------
# Missing data: null temperatures are skipped
# ---------------------------------------------------------------------------


def test_null_temperatures_are_skipped(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(re.escape(SYNOPTIC_API_URL)),
        json=_load("wrh_klga_missing.json"),
    )
    with httpx.Client() as client:
        result = fetch_wrh_hourly_extreme(
            "KLGA",
            date(2026, 8, 20),
            date(2026, 8, 21),
            client,
            "TMAX",
            timezone="America/New_York",
        )
    # With nulls removed, day 1: [68, 89, 74] -> max 89.0
    #                  day 2: [66, 91, 72] -> max 91.0
    assert result == {date(2026, 8, 20): 89.0, date(2026, 8, 21): 91.0}


def test_empty_station_list_returns_empty_dict(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(re.escape(SYNOPTIC_API_URL)),
        json=_load("wrh_empty_station.json"),
    )
    with httpx.Client() as client:
        result = fetch_wrh_hourly_extreme(
            "KLGA",
            date(2026, 8, 20),
            date(2026, 8, 21),
            client,
            timezone="America/New_York",
        )
    assert result == {}


# ---------------------------------------------------------------------------
# API error: RESPONSE_CODE != 1 raises ValueError
# ---------------------------------------------------------------------------


def test_api_error_raises_value_error(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(re.escape(SYNOPTIC_API_URL)),
        json=_load("wrh_klga_api_error.json"),
    )
    with httpx.Client() as client:
        with pytest.raises(ValueError, match="Synoptic API error"):
            fetch_wrh_hourly_extreme(
                "BADSTATION",
                date(2026, 8, 20),
                date(2026, 8, 21),
                client,
                timezone="America/New_York",
            )


# ---------------------------------------------------------------------------
# Local-day bucketing: midnight boundary observations
# ---------------------------------------------------------------------------


def test_local_day_bucketing_across_midnight_boundary(httpx_mock):
    """Observations near midnight in Pacific time must bucket to the correct
    local day, not the UTC day. KLAX is UTC-7 in summer."""
    httpx_mock.add_response(
        url=re.compile(re.escape(SYNOPTIC_API_URL)),
        json=_load("wrh_klax_midnight_boundary.json"),
    )
    with httpx.Client() as client:
        result = fetch_wrh_hourly_extreme(
            "KLAX",
            date(2026, 8, 20),
            date(2026, 8, 22),
            client,
            "TMAX",
            timezone="America/Los_Angeles",
        )
    # Aug 20: [70.0] (23:55 PDT) -> max 70.0
    # Aug 21: [65.0, 85.0] (00:30 + 14:55 PDT) -> max 85.0
    # Aug 22: [63.0, 87.0] (00:30 + 14:55 PDT) -> max 87.0
    assert result == {
        date(2026, 8, 20): 70.0,
        date(2026, 8, 21): 85.0,
        date(2026, 8, 22): 87.0,
    }


def test_local_day_bucketing_tmin_across_midnight_boundary(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(re.escape(SYNOPTIC_API_URL)),
        json=_load("wrh_klax_midnight_boundary.json"),
    )
    with httpx.Client() as client:
        result = fetch_wrh_hourly_extreme(
            "KLAX",
            date(2026, 8, 20),
            date(2026, 8, 22),
            client,
            "TMIN",
            timezone="America/Los_Angeles",
        )
    # Aug 20: [70.0] -> min 70.0
    # Aug 21: [65.0, 85.0] -> min 65.0
    # Aug 22: [63.0, 87.0] -> min 63.0
    assert result == {
        date(2026, 8, 20): 70.0,
        date(2026, 8, 21): 65.0,
        date(2026, 8, 22): 63.0,
    }


# ---------------------------------------------------------------------------
# Request parameters: Referer header, STID, units, obtimezone
# ---------------------------------------------------------------------------


def test_referer_header_sent(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(re.escape(SYNOPTIC_API_URL)),
        json=_load("wrh_klga_normal.json"),
    )
    with httpx.Client() as client:
        fetch_wrh_hourly_extreme(
            "KLGA",
            date(2026, 8, 20),
            date(2026, 8, 21),
            client,
            timezone="America/New_York",
        )
    req = httpx_mock.get_requests()[0]
    assert req.headers["Referer"].startswith(WRH_REFERER)


def test_referer_includes_lowercase_site_param(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(re.escape(SYNOPTIC_API_URL)),
        json=_load("wrh_klga_normal.json"),
    )
    with httpx.Client() as client:
        fetch_wrh_hourly_extreme(
            "KLGA",
            date(2026, 8, 20),
            date(2026, 8, 21),
            client,
            timezone="America/New_York",
        )
    req = httpx_mock.get_requests()[0]
    assert "site=klga" in req.headers["Referer"]


def test_origin_header_sent(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(re.escape(SYNOPTIC_API_URL)),
        json=_load("wrh_klga_normal.json"),
    )
    with httpx.Client() as client:
        fetch_wrh_hourly_extreme(
            "KLGA",
            date(2026, 8, 20),
            date(2026, 8, 21),
            client,
            timezone="America/New_York",
        )
    req = httpx_mock.get_requests()[0]
    assert req.headers["Origin"] == "https://www.weather.gov"


def test_stid_param_uppercase(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(re.escape(SYNOPTIC_API_URL)),
        json=_load("wrh_klga_normal.json"),
    )
    with httpx.Client() as client:
        fetch_wrh_hourly_extreme(
            "klga",
            date(2026, 8, 20),
            date(2026, 8, 21),
            client,
            timezone="America/New_York",
        )
    req = httpx_mock.get_requests()[0]
    assert req.url.params["STID"] == "KLGA"


def test_units_param_requests_fahrenheit(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(re.escape(SYNOPTIC_API_URL)),
        json=_load("wrh_klga_normal.json"),
    )
    with httpx.Client() as client:
        fetch_wrh_hourly_extreme(
            "KLGA",
            date(2026, 8, 20),
            date(2026, 8, 21),
            client,
            timezone="America/New_York",
        )
    req = httpx_mock.get_requests()[0]
    assert "temp|F" in req.url.params["units"]


def test_obtimezone_param_is_local(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(re.escape(SYNOPTIC_API_URL)),
        json=_load("wrh_klga_normal.json"),
    )
    with httpx.Client() as client:
        fetch_wrh_hourly_extreme(
            "KLGA",
            date(2026, 8, 20),
            date(2026, 8, 21),
            client,
            timezone="America/New_York",
        )
    req = httpx_mock.get_requests()[0]
    assert req.url.params["obtimezone"] == "local"


def test_start_end_params_formatted_correctly(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(re.escape(SYNOPTIC_API_URL)),
        json=_load("wrh_klga_normal.json"),
    )
    with httpx.Client() as client:
        fetch_wrh_hourly_extreme(
            "KLGA",
            date(2026, 8, 20),
            date(2026, 8, 21),
            client,
            timezone="America/New_York",
        )
    req = httpx_mock.get_requests()[0]
    assert req.url.params["start"] == "202608200000"
    assert req.url.params["end"] == "202608212359"


# ---------------------------------------------------------------------------
# 429 backoff handling
# ---------------------------------------------------------------------------


@pytest.fixture
def _fast_sleep(monkeypatch):
    """Make time.sleep instant so 429 retry tests run fast."""
    monkeypatch.setattr(time, "sleep", lambda _s: None)


def test_429_retries_then_succeeds(httpx_mock, _fast_sleep):
    httpx_mock.add_response(
        url=re.compile(re.escape(SYNOPTIC_API_URL)),
        status_code=429,
        headers={"Retry-After": "0"},
    )
    httpx_mock.add_response(
        url=re.compile(re.escape(SYNOPTIC_API_URL)),
        json=_load("wrh_klga_normal.json"),
    )
    with httpx.Client() as client:
        result = fetch_wrh_hourly_extreme(
            "KLGA",
            date(2026, 8, 20),
            date(2026, 8, 21),
            client,
            timezone="America/New_York",
        )
    assert len(httpx_mock.get_requests()) == 2
    assert result == {date(2026, 8, 20): 89.0, date(2026, 8, 21): 91.0}


def test_429_uses_retry_after_header(httpx_mock, monkeypatch):
    """Verify the function reads Retry-After and calls time.sleep with it."""
    slept: list[float] = []

    def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(time, "sleep", fake_sleep)

    httpx_mock.add_response(
        url=re.compile(re.escape(SYNOPTIC_API_URL)),
        status_code=429,
        headers={"Retry-After": "3"},
    )
    httpx_mock.add_response(
        url=re.compile(re.escape(SYNOPTIC_API_URL)),
        json=_load("wrh_klga_normal.json"),
    )
    with httpx.Client() as client:
        fetch_wrh_hourly_extreme(
            "KLGA",
            date(2026, 8, 20),
            date(2026, 8, 21),
            client,
            timezone="America/New_York",
        )
    assert slept == [3.0]


def test_429_uses_default_wait_when_no_retry_after(httpx_mock, monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))

    httpx_mock.add_response(
        url=re.compile(re.escape(SYNOPTIC_API_URL)),
        status_code=429,
    )
    httpx_mock.add_response(
        url=re.compile(re.escape(SYNOPTIC_API_URL)),
        json=_load("wrh_klga_normal.json"),
    )
    with httpx.Client() as client:
        fetch_wrh_hourly_extreme(
            "KLGA",
            date(2026, 8, 20),
            date(2026, 8, 21),
            client,
            timezone="America/New_York",
        )
    from rainmaker.forecasts.wrh import WRH_429_DEFAULT_WAIT_S

    assert slept == [WRH_429_DEFAULT_WAIT_S]


def test_429_caps_huge_retry_after(httpx_mock, monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))

    httpx_mock.add_response(
        url=re.compile(re.escape(SYNOPTIC_API_URL)),
        status_code=429,
        headers={"Retry-After": "9999"},
    )
    httpx_mock.add_response(
        url=re.compile(re.escape(SYNOPTIC_API_URL)),
        json=_load("wrh_klga_normal.json"),
    )
    with httpx.Client() as client:
        fetch_wrh_hourly_extreme(
            "KLGA",
            date(2026, 8, 20),
            date(2026, 8, 21),
            client,
            timezone="America/New_York",
        )
    from rainmaker.forecasts.wrh import WRH_429_MAX_WAIT_S

    assert slept == [WRH_429_MAX_WAIT_S]


def test_429_exhausting_retries_raises(httpx_mock, _fast_sleep):
    # All WRH_MAX_RETRIES (4) attempts return 429.
    for _ in range(4):
        httpx_mock.add_response(
            url=re.compile(re.escape(SYNOPTIC_API_URL)),
            status_code=429,
            headers={"Retry-After": "0"},
        )
    with httpx.Client() as client:
        with pytest.raises(httpx.HTTPStatusError):
            fetch_wrh_hourly_extreme(
                "KLGA",
                date(2026, 8, 20),
                date(2026, 8, 21),
                client,
                timezone="America/New_York",
            )
    assert len(httpx_mock.get_requests()) == 4


def test_non_429_http_error_raises_immediately(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(re.escape(SYNOPTIC_API_URL)),
        status_code=500,
    )
    with httpx.Client() as client:
        with pytest.raises(httpx.HTTPStatusError):
            fetch_wrh_hourly_extreme(
                "KLGA",
                date(2026, 8, 20),
                date(2026, 8, 21),
                client,
                timezone="America/New_York",
            )
    assert len(httpx_mock.get_requests()) == 1
