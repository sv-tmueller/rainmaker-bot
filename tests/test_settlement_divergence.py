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

from rainmaker.backfill import NCEI_URL as NCEI_GHCND_URL
from rainmaker.settlement_divergence import (
    ICAO_TO_ASOS_STATION,
    MESONET_ASOS_URL,
    NCEI_ISD_URL,
    DivergenceRow,
    GhcndToIsdMapping,
    fetch_asos_actuals_mesonet,
    fetch_isd_actuals,
    isd_station_for,
    render_divergence_report,
    resolved_bucket_label,
    run_spike,
    summarise,
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
    # NYC LaGuardia - 11-char format (USAF=725030, WBAN=14732, no dash)
    result = isd_station_for("USW00014732", mapping)
    assert result == "72503014732"


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
            isd_station="72503014732",
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
            isd_station="72503014732",
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
            isd_station="72503014732",
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
            isd_station="72503014732",
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
                isd_station="72503014732",
                start=date(2026, 3, 2),
                end=date(2026, 3, 2),
                client=client,
                variable="TMAX",
            )


# ---------------------------------------------------------------------------
# Iowa State Mesonet ASOS fetch -> daily extreme
# ---------------------------------------------------------------------------


def _mesonet_fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def test_fetch_asos_actuals_mesonet_returns_daily_max(httpx_mock):
    """fetch_asos_actuals_mesonet reduces hourly tmpc to the daily TMAX correctly."""
    fixture_csv = _mesonet_fixture("mesonet_asos_klga_2026-03-02.csv")
    httpx_mock.add_response(url=re.compile(re.escape(MESONET_ASOS_URL)), text=fixture_csv)
    with httpx.Client() as client:
        result = fetch_asos_actuals_mesonet(
            asos_station="LGA",
            start=date(2026, 3, 2),
            end=date(2026, 3, 2),
            client=client,
            variable="TMAX",
        )
    assert date(2026, 3, 2) in result
    # Max should be 3.33C = 37.994F ~ 38F
    assert abs(result[date(2026, 3, 2)] - 38.0) < 1.0


def test_fetch_asos_actuals_mesonet_returns_daily_min(httpx_mock):
    """fetch_asos_actuals_mesonet reduces hourly tmpc to the daily TMIN correctly."""
    fixture_csv = _mesonet_fixture("mesonet_asos_klga_2026-03-02.csv")
    httpx_mock.add_response(url=re.compile(re.escape(MESONET_ASOS_URL)), text=fixture_csv)
    with httpx.Client() as client:
        result = fetch_asos_actuals_mesonet(
            asos_station="LGA",
            start=date(2026, 3, 2),
            end=date(2026, 3, 2),
            client=client,
            variable="TMIN",
        )
    assert date(2026, 3, 2) in result
    # Min should be -5.56C = 21.992F ~ 22F
    assert abs(result[date(2026, 3, 2)] - 22.0) < 1.0


def test_fetch_asos_actuals_mesonet_skips_missing(httpx_mock):
    """Rows with tmpc='M' (missing) are excluded."""
    fixture_csv = "station,valid,tmpc\nLGA,2026-03-02 12:00,15.0\nLGA,2026-03-02 14:00,M\n"
    httpx_mock.add_response(url=re.compile(re.escape(MESONET_ASOS_URL)), text=fixture_csv)
    with httpx.Client() as client:
        result = fetch_asos_actuals_mesonet(
            asos_station="LGA",
            start=date(2026, 3, 2),
            end=date(2026, 3, 2),
            client=client,
            variable="TMAX",
        )
    assert date(2026, 3, 2) in result
    # Only the 15.0C = 59F reading counts
    assert abs(result[date(2026, 3, 2)] - 59.0) < 1.0


def test_fetch_asos_actuals_mesonet_skips_debug_lines(httpx_mock):
    """Lines starting with '#' (debug/comment) are skipped."""
    fixture_csv = "#DEBUG: some debug line\nstation,valid,tmpc\nLGA,2026-03-02 15:00,10.0\n"
    httpx_mock.add_response(url=re.compile(re.escape(MESONET_ASOS_URL)), text=fixture_csv)
    with httpx.Client() as client:
        result = fetch_asos_actuals_mesonet(
            asos_station="LGA",
            start=date(2026, 3, 2),
            end=date(2026, 3, 2),
            client=client,
            variable="TMAX",
        )
    assert date(2026, 3, 2) in result
    # 10.0C = 50F
    assert abs(result[date(2026, 3, 2)] - 50.0) < 0.5


def test_fetch_asos_actuals_mesonet_raises_on_http_error(httpx_mock):
    httpx_mock.add_response(url=re.compile(re.escape(MESONET_ASOS_URL)), status_code=503)
    with httpx.Client() as client:
        with pytest.raises(httpx.HTTPStatusError):
            fetch_asos_actuals_mesonet(
                asos_station="LGA",
                start=date(2026, 3, 2),
                end=date(2026, 3, 2),
                client=client,
                variable="TMAX",
            )


def test_icao_to_asos_station_covers_all_stations():
    """Every ICAO code in STATIONS must have an ASOS code in ICAO_TO_ASOS_STATION."""
    from rainmaker.config import STATIONS

    missing = [
        f"{s.city}/{s.icao}" for s in STATIONS.values() if s.icao not in ICAO_TO_ASOS_STATION
    ]
    assert missing == [], f"Missing ASOS station codes: {missing}"


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


# ---------------------------------------------------------------------------
# ISD quality-flag and sentinel exclusion
# ---------------------------------------------------------------------------


def test_fetch_isd_actuals_skips_bad_quality_flag(httpx_mock):
    """A reading with a bad quality flag (flag 1) is excluded."""
    fixture = {
        "results": [
            {"DATE": "2026-03-02T12:00:00", "TMP": "0150,5"},  # 15.0C good
            {"DATE": "2026-03-02T14:00:00", "TMP": "0250,1"},  # 25.0C but flag 1 = suspect
        ]
    }
    httpx_mock.add_response(url=re.compile(re.escape(NCEI_ISD_URL)), json=fixture)
    with httpx.Client() as client:
        result = fetch_isd_actuals(
            isd_station="72503014732",
            start=date(2026, 3, 2),
            end=date(2026, 3, 2),
            client=client,
            variable="TMAX",
        )
    assert date(2026, 3, 2) in result
    # Only the good 15.0C = 59F reading should count; the 25.0C suspect is excluded
    assert abs(result[date(2026, 3, 2)] - 59.0) < 1.0


def test_fetch_isd_actuals_skips_sentinel_9999_with_good_flag(httpx_mock):
    """A reading with value 9999 (missing sentinel) is excluded even with a good flag."""
    fixture = {
        "results": [
            {"DATE": "2026-03-02T12:00:00", "TMP": "0150,5"},  # 15.0C good
            {"DATE": "2026-03-02T14:00:00", "TMP": "9999,5"},  # sentinel, good flag -> exclude
        ]
    }
    httpx_mock.add_response(url=re.compile(re.escape(NCEI_ISD_URL)), json=fixture)
    with httpx.Client() as client:
        result = fetch_isd_actuals(
            isd_station="72503014732",
            start=date(2026, 3, 2),
            end=date(2026, 3, 2),
            client=client,
            variable="TMAX",
        )
    assert date(2026, 3, 2) in result
    # Only the valid 15.0C = 59F reading counts; sentinel is excluded
    assert abs(result[date(2026, 3, 2)] - 59.0) < 1.0


# ---------------------------------------------------------------------------
# _bucket_edge_gap
# ---------------------------------------------------------------------------


def test_gap_range_bucket_below_lo():
    """Value below lo of a range bucket: gap = lo - rounded_value."""
    from rainmaker.settlement_divergence import _bucket_edge_gap

    # resolved "72-73°F", value 70.8F -> round(70.8) = 71, gap = 72 - 71 = 1.0
    gap = _bucket_edge_gap(70.8, "72-73°F")
    assert gap is not None
    assert abs(gap - 1.0) < 0.01


def test_gap_range_bucket_above_hi():
    """Value above hi of a range bucket: gap = rounded_value - hi."""
    from rainmaker.settlement_divergence import _bucket_edge_gap

    # resolved "72-73°F", value 74.3F -> round(74.3) = 74, gap = 74 - 73 = 1.0
    gap = _bucket_edge_gap(74.3, "72-73°F")
    assert gap is not None
    assert abs(gap - 1.0) < 0.01


def test_gap_below_bucket_above_threshold():
    """Value above threshold of a 'below' bucket: gap = rounded_value - threshold."""
    from rainmaker.settlement_divergence import _bucket_edge_gap

    # resolved "59°F or below", value 61.0F -> round(61) = 61, gap = 61 - 59 = 2.0
    gap = _bucket_edge_gap(61.0, "59°F or below")
    assert gap is not None
    assert abs(gap - 2.0) < 0.01


def test_gap_above_bucket_below_threshold():
    """Value below threshold of an 'above' bucket: gap = threshold - rounded_value."""
    from rainmaker.settlement_divergence import _bucket_edge_gap

    # resolved "78°F or higher", value 76.4F -> round(76.4) = 76, gap = 78 - 76 = 2.0
    gap = _bucket_edge_gap(76.4, "78°F or higher")
    assert gap is not None
    assert abs(gap - 2.0) < 0.01


def test_gap_returns_none_when_in_bucket():
    """_bucket_edge_gap returns None when value is inside the bucket."""
    from rainmaker.settlement_divergence import _bucket_edge_gap

    assert _bucket_edge_gap(72.3, "72-73°F") is None
    assert _bucket_edge_gap(59.0, "59°F or below") is None
    assert _bucket_edge_gap(78.0, "78°F or higher") is None


# ---------------------------------------------------------------------------
# summarise
# ---------------------------------------------------------------------------


def test_summarise_groups_by_city_and_variable():
    """A city with both TMAX and TMIN rows must produce two separate CityResult entries."""
    rows = [
        DivergenceRow(
            city="NYC",
            local_date=date(2026, 6, 1),
            variable="TMAX",
            resolved_label="68-69°F",
            ncei_value=67.0,
            ncei_in_bucket=False,
            asos_value=68.5,
            asos_in_bucket=True,
            ncei_gap=1.0,
            asos_gap=None,
        ),
        DivergenceRow(
            city="NYC",
            local_date=date(2026, 6, 1),
            variable="TMIN",
            resolved_label="57-58°F",
            ncei_value=55.0,
            ncei_in_bucket=False,
            asos_value=57.2,
            asos_in_bucket=True,
            ncei_gap=2.0,
            asos_gap=None,
        ),
        DivergenceRow(
            city="NYC",
            local_date=date(2026, 6, 2),
            variable="TMAX",
            resolved_label="72-73°F",
            ncei_value=72.3,
            ncei_in_bucket=True,
            asos_value=72.8,
            asos_in_bucket=True,
            ncei_gap=None,
            asos_gap=None,
        ),
    ]
    results = summarise(rows)
    # Must have two keys, one per (city, variable) combination
    assert set(results.keys()) == {"NYC/TMAX", "NYC/TMIN"}
    tmax = results["NYC/TMAX"]
    tmin = results["NYC/TMIN"]
    assert tmax.variable == "TMAX"
    assert tmax.n == 2  # 2 TMAX rows with both arms
    assert tmax.ncei_flips == 1  # 1 NCEI flip in TMAX
    assert tmax.asos_flips == 0
    assert tmin.variable == "TMIN"
    assert tmin.n == 1  # 1 TMIN row with both arms
    assert tmin.ncei_flips == 1  # 1 NCEI flip in TMIN
    assert tmin.asos_flips == 0


def test_summarise_single_variable_city_key_unchanged():
    """A city with only TMAX rows uses 'city/TMAX' as the key."""
    rows = [
        DivergenceRow(
            city="Miami",
            local_date=date(2026, 6, 1),
            variable="TMAX",
            resolved_label="90-91°F",
            ncei_value=90.5,
            ncei_in_bucket=True,
            asos_value=91.0,
            asos_in_bucket=True,
            ncei_gap=None,
            asos_gap=None,
        )
    ]
    results = summarise(rows)
    assert "Miami/TMAX" in results
    assert results["Miami/TMAX"].n == 1


# ---------------------------------------------------------------------------
# end-to-end
# ---------------------------------------------------------------------------


def test_run_spike_end_to_end(httpx_mock):
    """End-to-end: resolved fixture events -> run_spike -> summarise -> render.

    Events:
    - 800001: NYC TMAX 2026-06-01, resolved "68-69°F"
        NCEI: 67.0F (flip; gap 1.0F from lo=68)
        ASOS: 68.54F (in bucket; no flip)
    - 800002: NYC TMAX 2026-06-02, resolved "72-73°F"
        NCEI: 72.3F (in bucket; no flip)
        ASOS: 72.32F (in bucket; no flip)
    - 800003: NYC TMIN 2026-06-01, resolved "57-58°F"
        NCEI: 55.0F (flip; gap 2.0F from lo=57)
        ASOS: 57.2F (in bucket; no flip)

    Expected: NCEI flip rate TMAX = 1/2 = 50%; TMIN = 1/1 = 100%. ASOS 0%.
    """
    resolved_fixture = FIXTURES / "polymarket_closed_weather_events_resolved.json"
    resolved_events = json.loads(resolved_fixture.read_text())
    asos_csv = (FIXTURES / "mesonet_asos_klga_2026-06.csv").read_text()
    ncei_tmax_june1 = json.loads((FIXTURES / "ncei_ghcnd_klga_2026-06-01_tmax.json").read_text())
    ncei_tmax_june2 = json.loads((FIXTURES / "ncei_ghcnd_klga_2026-06-02_tmax.json").read_text())
    ncei_tmin_june1 = json.loads((FIXTURES / "ncei_ghcnd_klga_2026-06-01_tmin.json").read_text())

    # run_spike processes events in order; within each event: Arm A (NCEI) then Arm B (ASOS)
    httpx_mock.add_response(url=re.compile(re.escape(NCEI_GHCND_URL)), json=ncei_tmax_june1)
    httpx_mock.add_response(url=re.compile(re.escape(MESONET_ASOS_URL)), text=asos_csv)
    httpx_mock.add_response(url=re.compile(re.escape(NCEI_GHCND_URL)), json=ncei_tmax_june2)
    httpx_mock.add_response(url=re.compile(re.escape(MESONET_ASOS_URL)), text=asos_csv)
    httpx_mock.add_response(url=re.compile(re.escape(NCEI_GHCND_URL)), json=ncei_tmin_june1)
    httpx_mock.add_response(url=re.compile(re.escape(MESONET_ASOS_URL)), text=asos_csv)

    mapping = GhcndToIsdMapping.default()
    with httpx.Client() as client:
        rows = run_spike(resolved_events, client, mapping)

    # 3 events, all resolved and parseable -> 3 rows
    assert len(rows) == 3

    # Row 1: NYC TMAX 2026-06-01
    row1 = next(r for r in rows if r.local_date == date(2026, 6, 1) and r.variable == "TMAX")
    assert row1.ncei_value is not None
    assert abs(row1.ncei_value - 67.0) < 0.1
    assert row1.ncei_in_bucket is False
    assert row1.ncei_gap is not None
    assert abs(row1.ncei_gap - 1.0) < 0.01  # gap from lo=68
    assert row1.asos_in_bucket is True
    assert row1.asos_gap is None

    # Row 2: NYC TMAX 2026-06-02
    row2 = next(r for r in rows if r.local_date == date(2026, 6, 2) and r.variable == "TMAX")
    assert row2.ncei_in_bucket is True
    assert row2.ncei_gap is None
    assert row2.asos_in_bucket is True

    # Row 3: NYC TMIN 2026-06-01
    row3 = next(r for r in rows if r.local_date == date(2026, 6, 1) and r.variable == "TMIN")
    assert row3.ncei_in_bucket is False
    assert row3.ncei_gap is not None
    assert abs(row3.ncei_gap - 2.0) < 0.01  # gap from lo=57
    assert row3.asos_in_bucket is True

    # summarise groups by (city, variable)
    city_results = summarise(rows)
    assert set(city_results.keys()) == {"NYC/TMAX", "NYC/TMIN"}

    tmax = city_results["NYC/TMAX"]
    assert tmax.ncei_flips == 1
    assert tmax.asos_flips == 0
    assert tmax.n == 2
    assert abs(tmax.ncei_flip_rate - 0.5) < 0.01

    tmin = city_results["NYC/TMIN"]
    assert tmin.ncei_flips == 1
    assert tmin.asos_flips == 0
    assert tmin.n == 1
    assert abs(tmin.ncei_flip_rate - 1.0) < 0.01

    # render produces a markdown report with the per-city table
    report = render_divergence_report(rows, city_results, "2026-06-15")
    assert "NYC" in report
    assert "TMAX" in report
    assert "TMIN" in report
    # NCEI flip for TMAX is 50%, ASOS is 0%
    assert "50%" in report
    assert "0%" in report
    # degree-gap distribution section
    assert "gap" in report.lower()
    # row-level detail includes the flip marker
    assert "NO" in report
