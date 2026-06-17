"""TDD tests for the ASOS production actuals source.

Tests drive the new src/rainmaker/forecasts/asos.py module.
All tests use fixtures, no live endpoints.
"""

import re
from datetime import date
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from rainmaker.forecasts.asos import (
    ASOS_429_MAX_WAIT_S,
    ASOS_MAX_RETRIES,
    ICAO_TO_ASOS_STATION,
    MESONET_ASOS_URL,
    fetch_asos_daily_extreme,
)

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# ICAO_TO_ASOS_STATION mapping
# ---------------------------------------------------------------------------


def test_icao_to_asos_station_has_all_11_us_cities():
    """All 11 US Polymarket cities must be mapped."""
    expected_us_icao = {
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
    assert expected_us_icao.issubset(set(ICAO_TO_ASOS_STATION.keys()))


def test_icao_to_asos_station_has_all_4_intl_stations():
    """All 4 intl Polymarket stations must be mapped (#190)."""
    expected_intl = {"EGLC", "LFPB", "EFHK", "SBGR"}
    assert expected_intl.issubset(set(ICAO_TO_ASOS_STATION.keys()))


def test_icao_to_asos_station_us_drops_k_prefix():
    """US ASOS codes are 3-letter FAA codes (no K prefix)."""
    us_icao = {k: v for k, v in ICAO_TO_ASOS_STATION.items() if k.startswith("K")}
    for _icao, asos in us_icao.items():
        assert len(asos) == 3
        assert not asos.startswith("K")


def test_icao_to_asos_station_intl_passes_icao_unchanged():
    """Intl ASOS codes pass the 4-letter ICAO unchanged (no K-strip)."""
    intl_icao = {k: v for k, v in ICAO_TO_ASOS_STATION.items() if not k.startswith("K")}
    for icao, asos in intl_icao.items():
        assert asos == icao


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


# ---------------------------------------------------------------------------
# 429 rate-limit handling: backoff and retry
# ---------------------------------------------------------------------------


def test_fetch_asos_retries_on_429(httpx_mock):
    """A 429 response triggers a retry; the second request succeeds and data is returned."""
    fixture = (FIXTURES / "mesonet_asos_klga_2026-03-02.csv").read_text()
    httpx_mock.add_response(
        url=re.compile(re.escape(MESONET_ASOS_URL)),
        status_code=429,
        headers={"Retry-After": "1"},
    )
    httpx_mock.add_response(
        url=re.compile(re.escape(MESONET_ASOS_URL)),
        text=fixture,
    )
    slept: list[float] = []
    with patch("rainmaker.forecasts.asos.time.sleep", side_effect=slept.append):
        with httpx.Client() as client:
            result = fetch_asos_daily_extreme(
                "LGA",
                date(2026, 3, 2),
                date(2026, 3, 2),
                client,
                "TMAX",
            )
    assert date(2026, 3, 2) in result
    # Must have slept at least once
    assert len(slept) >= 1


def test_fetch_asos_respects_retry_after_header(httpx_mock):
    """Retry-After header value caps sleep duration (capped at ASOS_429_MAX_WAIT_S)."""
    fixture = (FIXTURES / "mesonet_asos_klga_2026-03-02.csv").read_text()
    httpx_mock.add_response(
        url=re.compile(re.escape(MESONET_ASOS_URL)),
        status_code=429,
        headers={"Retry-After": "2"},
    )
    httpx_mock.add_response(
        url=re.compile(re.escape(MESONET_ASOS_URL)),
        text=fixture,
    )
    slept: list[float] = []
    with patch("rainmaker.forecasts.asos.time.sleep", side_effect=slept.append):
        with httpx.Client() as client:
            fetch_asos_daily_extreme(
                "LGA",
                date(2026, 3, 2),
                date(2026, 3, 2),
                client,
                "TMAX",
            )
    # Sleep value should be min(2, ASOS_429_MAX_WAIT_S) = 2 since ASOS_429_MAX_WAIT_S >= 2
    assert slept[0] == pytest.approx(min(2.0, ASOS_429_MAX_WAIT_S), abs=0.01)


def test_fetch_asos_caps_retry_after_to_max(httpx_mock):
    """A huge Retry-After value is capped at ASOS_429_MAX_WAIT_S to avoid hanging."""
    fixture = (FIXTURES / "mesonet_asos_klga_2026-03-02.csv").read_text()
    httpx_mock.add_response(
        url=re.compile(re.escape(MESONET_ASOS_URL)),
        status_code=429,
        headers={"Retry-After": "9999"},
    )
    httpx_mock.add_response(
        url=re.compile(re.escape(MESONET_ASOS_URL)),
        text=fixture,
    )
    slept: list[float] = []
    with patch("rainmaker.forecasts.asos.time.sleep", side_effect=slept.append):
        with httpx.Client() as client:
            fetch_asos_daily_extreme(
                "LGA",
                date(2026, 3, 2),
                date(2026, 3, 2),
                client,
                "TMAX",
            )
    # Must not sleep longer than ASOS_429_MAX_WAIT_S
    assert slept[0] <= ASOS_429_MAX_WAIT_S + 0.01


def test_fetch_asos_raises_after_max_retries(httpx_mock):
    """After ASOS_MAX_RETRIES 429s, HTTPStatusError is raised."""
    for _ in range(ASOS_MAX_RETRIES):
        httpx_mock.add_response(
            url=re.compile(re.escape(MESONET_ASOS_URL)),
            status_code=429,
        )
    with patch("rainmaker.forecasts.asos.time.sleep"):
        with httpx.Client() as client:
            with pytest.raises(httpx.HTTPStatusError):
                fetch_asos_daily_extreme(
                    "LGA",
                    date(2026, 3, 2),
                    date(2026, 3, 2),
                    client,
                    "TMAX",
                )


def test_fetch_asos_uses_default_backoff_without_retry_after(httpx_mock):
    """When Retry-After header is absent, a default backoff is used (not zero)."""
    fixture = (FIXTURES / "mesonet_asos_klga_2026-03-02.csv").read_text()
    httpx_mock.add_response(
        url=re.compile(re.escape(MESONET_ASOS_URL)),
        status_code=429,
    )
    httpx_mock.add_response(
        url=re.compile(re.escape(MESONET_ASOS_URL)),
        text=fixture,
    )
    slept: list[float] = []
    with patch("rainmaker.forecasts.asos.time.sleep", side_effect=slept.append):
        with httpx.Client() as client:
            fetch_asos_daily_extreme(
                "LGA",
                date(2026, 3, 2),
                date(2026, 3, 2),
                client,
                "TMAX",
            )
    assert len(slept) == 1
    assert slept[0] > 0


# ---------------------------------------------------------------------------
# International (intl) path: local_tz set, no report_type filter, Celsius
# ---------------------------------------------------------------------------


def test_intl_tmax_returns_celsius_not_fahrenheit(httpx_mock):
    """Intl TMAX with local_tz set must return Celsius, not Fahrenheit."""
    fixture = (FIXTURES / "mesonet_asos_eglc_2026-06-09.csv").read_text()
    httpx_mock.add_response(
        url=re.compile(re.escape(MESONET_ASOS_URL)),
        text=fixture,
    )
    with httpx.Client() as client:
        result = fetch_asos_daily_extreme(
            "EGLC",
            date(2026, 6, 8),  # padded window start
            date(2026, 6, 10),  # padded window end
            client,
            "TMAX",
            local_tz="Europe/London",
            target_date=date(2026, 6, 9),
        )
    d = date(2026, 6, 9)
    assert d in result
    # TMAX for 2026-06-09 local is 19C (from SPECI at 11:20 UTC = 12:20 local)
    assert result[d] == pytest.approx(19.0, abs=0.01)


def test_intl_tmax_includes_speci_observations(httpx_mock):
    """Without report_type filter, SPECI obs are included; they may hold the true peak."""
    fixture = (FIXTURES / "mesonet_asos_eglc_2026-06-09.csv").read_text()
    httpx_mock.add_response(
        url=re.compile(re.escape(MESONET_ASOS_URL)),
        text=fixture,
    )
    with httpx.Client() as client:
        result = fetch_asos_daily_extreme(
            "EGLC",
            date(2026, 6, 8),
            date(2026, 6, 10),
            client,
            "TMAX",
            local_tz="Europe/London",
            target_date=date(2026, 6, 9),
        )
    # 19C comes from the 11:20 SPECI; if only routine (:50) were used, max would be 18C
    assert result[date(2026, 6, 9)] == pytest.approx(19.0, abs=0.01)


def test_intl_tmin_returns_celsius(httpx_mock):
    """Intl TMIN with local_tz set must return Celsius minimum."""
    fixture = (FIXTURES / "mesonet_asos_eglc_2026-06-09.csv").read_text()
    httpx_mock.add_response(
        url=re.compile(re.escape(MESONET_ASOS_URL)),
        text=fixture,
    )
    with httpx.Client() as client:
        result = fetch_asos_daily_extreme(
            "EGLC",
            date(2026, 6, 8),
            date(2026, 6, 10),
            client,
            "TMIN",
            local_tz="Europe/London",
            target_date=date(2026, 6, 9),
        )
    d = date(2026, 6, 9)
    assert d in result
    # TMIN for 2026-06-09 local: first obs UTC 23:20 on 2026-06-08 = 00:20 local 2026-06-09 = 14C
    # But the overnight lows in UTC+1 are the 01:xx-05:xx UTC obs (02:xx-06:xx local), which are 11C
    assert result[d] == pytest.approx(11.0, abs=0.01)


def test_intl_local_day_bucketing_excludes_next_day_obs(httpx_mock):
    """UTC 23:xx obs for EGLC (UTC+1) fall on the NEXT local day; must not contaminate today."""
    fixture = (FIXTURES / "mesonet_asos_eglc_2026-06-09.csv").read_text()
    httpx_mock.add_response(
        url=re.compile(re.escape(MESONET_ASOS_URL)),
        text=fixture,
    )
    with httpx.Client() as client:
        result = fetch_asos_daily_extreme(
            "EGLC",
            date(2026, 6, 8),
            date(2026, 6, 10),
            client,
            "TMIN",
            local_tz="Europe/London",
            target_date=date(2026, 6, 9),
        )
    # The 23:20 and 23:50 UTC obs on 2026-06-09 are 00:20 and 00:50 local on 2026-06-10.
    # They have tmpc=10.0 which is colder than the true 2026-06-09 local minimum of 11.0.
    # If next-day obs leaked in, TMIN would be 10.0 instead of 11.0.
    assert result[date(2026, 6, 9)] == pytest.approx(11.0, abs=0.01)


def test_intl_local_day_bucketing_includes_prior_utc_day_obs(httpx_mock):
    """UTC 23:xx on the prior calendar day that fall in local day must be included."""
    fixture = (FIXTURES / "mesonet_asos_eglc_2026-06-09.csv").read_text()
    httpx_mock.add_response(
        url=re.compile(re.escape(MESONET_ASOS_URL)),
        text=fixture,
    )
    with httpx.Client() as client:
        result = fetch_asos_daily_extreme(
            "EGLC",
            date(2026, 6, 8),
            date(2026, 6, 10),
            client,
            "TMAX",
            local_tz="Europe/London",
            target_date=date(2026, 6, 9),
        )
    # 2026-06-08 23:20 and 23:50 UTC = 00:20 and 00:50 local on 2026-06-09 -> included
    # Their value is 14C which is not the max (19C), but they must be in the pool.
    # TMAX should still be 19C (the SPECI), confirming all obs are in the pool.
    assert result[date(2026, 6, 9)] == pytest.approx(19.0, abs=0.01)


def test_intl_local_day_bucketing_negative_offset_sao_paulo(httpx_mock):
    """SBGR (Sao Paulo, UTC-3): a NEXT-UTC-day obs falls on the target local day
    and must be included; a same-UTC-day obs that is the PRIOR local day must be
    excluded. Negative-offset mirror of the EGLC (UTC+1) boundary tests."""
    # Local day 2026-06-09 in America/Sao_Paulo (UTC-3) = UTC 2026-06-09 03:00 to 2026-06-10 02:59.
    csv = (
        "station,valid,tmpc\n"
        # UTC 2026-06-09 02:30 = local 2026-06-08 23:30 (PRIOR local day) -> EXCLUDED.
        # Hottest reading; if it leaked in, TMAX would be 30.0.
        "SBGR,2026-06-09 02:30,30.0\n"
        # Within local 2026-06-09:
        "SBGR,2026-06-09 12:30,25.0\n"
        "SBGR,2026-06-09 18:30,24.0\n"
        # UTC 2026-06-10 02:30 = local 2026-06-09 23:30 (NEXT UTC day, target day) -> INCLUDED.
        # This is the true daily max.
        "SBGR,2026-06-10 02:30,26.0\n"
        # UTC 2026-06-10 03:30 = local 2026-06-10 00:30 (NEXT local day) -> EXCLUDED.
        "SBGR,2026-06-10 03:30,35.0\n"
    )
    httpx_mock.add_response(url=re.compile(re.escape(MESONET_ASOS_URL)), text=csv)
    with httpx.Client() as client:
        result = fetch_asos_daily_extreme(
            "SBGR",
            date(2026, 6, 8),
            date(2026, 6, 10),
            client,
            "TMAX",
            local_tz="America/Sao_Paulo",
            target_date=date(2026, 6, 9),
        )
    # 26.0 (next UTC day, local 06-09) is the max; 30.0 (prior local day) and
    # 35.0 (next local day) must be excluded.
    assert result[date(2026, 6, 9)] == pytest.approx(26.0, abs=0.01)


def test_intl_omits_report_type_param(httpx_mock):
    """Intl requests must NOT send report_type (to include SPECI obs)."""
    fixture = (FIXTURES / "mesonet_asos_eglc_2026-06-09.csv").read_text()
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, text=fixture)

    httpx_mock.add_callback(handler)
    with httpx.Client() as client:
        fetch_asos_daily_extreme(
            "EGLC",
            date(2026, 6, 8),
            date(2026, 6, 10),
            client,
            "TMAX",
            local_tz="Europe/London",
            target_date=date(2026, 6, 9),
        )
    assert "report_type" not in captured["params"]


def test_us_path_still_sends_report_type_3(httpx_mock):
    """US path (local_tz=None) must still send report_type=3 (no regression)."""
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
    assert captured["params"].get("report_type") == "3"


def test_us_path_returns_fahrenheit_unchanged(httpx_mock):
    """US path (local_tz=None) must still return Fahrenheit (no regression)."""
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
    # max tmpc = 3.33C -> 3.33*9/5+32 = 37.994F (not 3.33)
    assert result[d] == pytest.approx(3.33 * 9 / 5 + 32, abs=0.01)
    assert result[d] > 10  # clearly Fahrenheit, not Celsius
