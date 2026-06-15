"""TDD tests for the settlement-divergence spike.

Tests use synthetic inputs (bucket logic) and saved JSON fixtures (API I/O).
No live endpoint calls.
"""

import json
import re
from datetime import date
from pathlib import Path

import httpx
import pytest

from rainmaker.settlement_divergence import (
    NCEI_ISD_URL,
    DivergenceRow,
    GhcndToIsdMapping,
    fetch_isd_actuals,
    isd_station_for,
    resolved_bucket_label,
    temperature_in_bucket,
)

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# resolved_bucket_label: detect the winning outcome from outcomePrices
# ---------------------------------------------------------------------------


def test_resolved_bucket_label_picks_market_with_yes_price_one():
    markets = [
        {"groupItemTitle": "70-71°F", "outcomePrices": '["0.05", "0.95"]'},
        {"groupItemTitle": "72-73°F", "outcomePrices": '["1.0", "0.0"]'},
        {"groupItemTitle": "74°F or higher", "outcomePrices": '["0.0", "1.0"]'},
    ]
    assert resolved_bucket_label(markets) == "72-73°F"


def test_resolved_bucket_label_returns_none_when_no_winner():
    markets = [
        {"groupItemTitle": "70-71°F", "outcomePrices": '["0.05", "0.95"]'},
        {"groupItemTitle": "72-73°F", "outcomePrices": '["0.90", "0.10"]'},
    ]
    assert resolved_bucket_label(markets) is None


def test_resolved_bucket_label_handles_integer_one():
    # Gamma sometimes returns "1" instead of "1.0"
    markets = [
        {"groupItemTitle": "75-76°F", "outcomePrices": '["1", "0"]'},
    ]
    assert resolved_bucket_label(markets) == "75-76°F"


def test_resolved_bucket_label_empty_markets():
    assert resolved_bucket_label([]) is None


# ---------------------------------------------------------------------------
# temperature_in_bucket: check whether a temp value settles in a bucket label
# ---------------------------------------------------------------------------


def test_temperature_in_bucket_range_exact():
    assert temperature_in_bucket(72, "72-73°F") is True


def test_temperature_in_bucket_range_upper():
    assert temperature_in_bucket(73, "72-73°F") is True


def test_temperature_in_bucket_range_miss():
    assert temperature_in_bucket(74, "72-73°F") is False


def test_temperature_in_bucket_below():
    assert temperature_in_bucket(59, "59°F or below") is True
    assert temperature_in_bucket(60, "59°F or below") is False


def test_temperature_in_bucket_above():
    assert temperature_in_bucket(80, "78°F or higher") is True
    assert temperature_in_bucket(77, "78°F or higher") is False


def test_temperature_in_bucket_rounding():
    # 72.4 rounds to 72, 72.5 rounds to 72 (banker's rounding), 72.6 rounds to 73
    assert temperature_in_bucket(72.4, "72-73°F") is True
    assert temperature_in_bucket(72.6, "72-73°F") is True
    assert temperature_in_bucket(71.4, "72-73°F") is False


def test_temperature_in_bucket_negative():
    assert temperature_in_bucket(-5, "-10--5°F") is True
    assert temperature_in_bucket(-11, "-10--5°F") is False


# ---------------------------------------------------------------------------
# GhcndToIsdMapping: map GHCND station id -> ISD USAF-WBAN id
# ---------------------------------------------------------------------------


def test_isd_station_for_known_station():
    mapping = GhcndToIsdMapping.default()
    # NYC LaGuardia
    result = isd_station_for("USW00014732", mapping)
    assert result == "725030-14732"


def test_isd_station_for_unknown_station():
    mapping = GhcndToIsdMapping.default()
    result = isd_station_for("UNKNWN99999", mapping)
    assert result is None


def test_ghcnd_to_isd_mapping_covers_all_polymarket_stations():
    """Every GHCND id in STATIONS must have an ISD mapping."""
    from rainmaker.config import STATIONS

    mapping = GhcndToIsdMapping.default()
    missing = [
        f"{s.city}/{s.ghcnd_id}"
        for s in STATIONS.values()
        if isd_station_for(s.ghcnd_id, mapping) is None
    ]
    assert missing == [], f"Missing ISD mappings: {missing}"


# ---------------------------------------------------------------------------
# fetch_isd_actuals: ASOS daily extreme from NCEI ISD hourly
# ---------------------------------------------------------------------------


def _isd_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_fetch_isd_actuals_returns_daily_max(httpx_mock):
    """fetch_isd_actuals reduces hourly TMP to the daily max (TMAX) correctly."""
    fixture = _isd_fixture("ncei_isd_hourly_klga.json")
    httpx_mock.add_response(url=re.compile(re.escape(NCEI_ISD_URL)), json=fixture)
    with httpx.Client() as client:
        result = fetch_isd_actuals(
            isd_station="725030-14732",
            start=date(2026, 3, 2),
            end=date(2026, 3, 2),
            client=client,
            variable="TMAX",
        )
    # The fixture has known values: see tests/fixtures/ncei_isd_hourly_klga.json
    assert date(2026, 3, 2) in result
    assert isinstance(result[date(2026, 3, 2)], float)
    # Max should be 6.1C = 42.98F ~ 43F
    assert abs(result[date(2026, 3, 2)] - 43.0) < 1.0


def test_fetch_isd_actuals_returns_daily_min(httpx_mock):
    """fetch_isd_actuals reduces hourly TMP to the daily min (TMIN) correctly."""
    fixture = _isd_fixture("ncei_isd_hourly_klga.json")
    httpx_mock.add_response(url=re.compile(re.escape(NCEI_ISD_URL)), json=fixture)
    with httpx.Client() as client:
        result = fetch_isd_actuals(
            isd_station="725030-14732",
            start=date(2026, 3, 2),
            end=date(2026, 3, 2),
            client=client,
            variable="TMIN",
        )
    assert date(2026, 3, 2) in result
    # Min should be 1.1C = 33.98F ~ 34F
    assert abs(result[date(2026, 3, 2)] - 34.0) < 1.0


def test_fetch_isd_actuals_skips_missing_data(httpx_mock):
    """Rows with null TMP or quality flags 1/2/6/A/B/C/D are excluded."""
    # One valid reading (flagged as '5' = good), one bad (flag 1 = suspect)
    fixture = {
        "results": [
            {
                "DATE": "2026-03-02T12:00:00",
                "TMP": "0150,5",  # 15.0C, quality 5 (good) = 59F
            },
            {
                "DATE": "2026-03-02T14:00:00",
                "TMP": "9999,1",  # missing/suspect
            },
        ]
    }
    httpx_mock.add_response(url=re.compile(re.escape(NCEI_ISD_URL)), json=fixture)
    with httpx.Client() as client:
        result = fetch_isd_actuals(
            isd_station="725030-14732",
            start=date(2026, 3, 2),
            end=date(2026, 3, 2),
            client=client,
            variable="TMAX",
        )
    assert date(2026, 3, 2) in result
    # Only the valid 15.0C = 59F reading should count
    assert abs(result[date(2026, 3, 2)] - 59.0) < 1.0


def test_fetch_isd_actuals_celsius_conversion(httpx_mock):
    """ISD TMP is tenths of Celsius; verify conversion to Fahrenheit."""
    # TMP = "0300,5" => 30.0C => 86F
    fixture = {
        "results": [
            {"DATE": "2026-06-01T15:00:00", "TMP": "0300,5"},
            {"DATE": "2026-06-01T16:00:00", "TMP": "0310,5"},  # 31.0C => 87.8F
        ]
    }
    httpx_mock.add_response(url=re.compile(re.escape(NCEI_ISD_URL)), json=fixture)
    with httpx.Client() as client:
        result = fetch_isd_actuals(
            isd_station="725030-14732",
            start=date(2026, 6, 1),
            end=date(2026, 6, 1),
            client=client,
            variable="TMAX",
        )
    assert date(2026, 6, 1) in result
    # Max should be 31.0C = 87.8F
    assert abs(result[date(2026, 6, 1)] - 87.8) < 0.5


def test_fetch_isd_actuals_raises_on_http_error(httpx_mock):
    httpx_mock.add_response(url=re.compile(re.escape(NCEI_ISD_URL)), status_code=503)
    with httpx.Client() as client:
        with pytest.raises(httpx.HTTPStatusError):
            fetch_isd_actuals(
                isd_station="725030-14732",
                start=date(2026, 3, 2),
                end=date(2026, 3, 2),
                client=client,
                variable="TMAX",
            )


# ---------------------------------------------------------------------------
# DivergenceRow: the output data type
# ---------------------------------------------------------------------------


def test_divergence_row_fields():
    row = DivergenceRow(
        city="NYC",
        local_date=date(2026, 3, 2),
        variable="TMAX",
        resolved_label="72-73°F",
        ncei_value=71.5,
        ncei_in_bucket=False,
        asos_value=72.8,
        asos_in_bucket=True,
        ncei_gap=0.5,
        asos_gap=None,
    )
    assert row.city == "NYC"
    assert row.ncei_in_bucket is False
    assert row.asos_in_bucket is True
