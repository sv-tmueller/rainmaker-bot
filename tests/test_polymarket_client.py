import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from rainmaker.polymarket.client import (
    GAMMA_EVENTS_URL,
    discover_markets,
    discover_precip_markets,
    fetch_weather_events,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _events_body() -> list[dict[str, Any]]:
    return json.loads((FIXTURES / "polymarket_weather_events.json").read_text())


def test_discover_markets_includes_us_and_intl_temp_markets(httpx_mock):
    # The fixture has NYC (KLGA, F) and London (EGLC, C with single-degree buckets).
    # discover_markets must parse both.
    httpx_mock.add_response(url=re.compile(re.escape(GAMMA_EVENTS_URL)), json=_events_body())
    with httpx.Client() as client:
        markets = discover_markets(client)
    assert len(markets) == 2
    icaos = sorted(m.target.station.icao for m in markets)
    assert icaos == ["EGLC", "KLGA"]
    nyc = next(m for m in markets if m.target.station.icao == "KLGA")
    assert nyc.id == "533147"
    assert len(nyc.buckets) == 11
    london = next(m for m in markets if m.target.station.icao == "EGLC")
    assert london.target.station.unit == "C"
    assert len(london.buckets) == 11


def test_fetch_weather_events_raises_when_gamma_down(httpx_mock):
    httpx_mock.add_response(url=re.compile(re.escape(GAMMA_EVENTS_URL)), status_code=500)
    with httpx.Client() as client:
        with pytest.raises(httpx.HTTPStatusError):
            fetch_weather_events(client)


def test_fetch_weather_events_paginates(httpx_mock):
    full_page = [{"title": "filler", "markets": []} for _ in range(100)]
    httpx_mock.add_response(url=re.compile(re.escape(GAMMA_EVENTS_URL)), json=full_page)
    httpx_mock.add_response(url=re.compile(re.escape(GAMMA_EVENTS_URL)), json=_events_body())
    with httpx.Client() as client:
        events = fetch_weather_events(client)
    assert len(events) == 100 + 3  # full first page + short second page stops paging


def _multicity_body() -> list[dict[str, Any]]:
    return json.loads((FIXTURES / "polymarket_weather_multicity.json").read_text())


def test_discover_skips_unparseable_and_drops_wrong_station_event(httpx_mock, capsys):
    # The multicity fixture has a London event whose description names EGLL, not EGLC.
    # London is registered as EGLC, so the ICAO guard fails and the event is skipped.
    httpx_mock.add_response(url=re.compile(re.escape(GAMMA_EVENTS_URL)), json=_multicity_body())
    with httpx.Client() as client:
        markets = discover_markets(client)
    # Los Angeles (multi-word) and Dallas (trap KDAL) are kept; NYC is skipped
    # because its description omits KLGA; London is skipped because EGLL != EGLC.
    assert sorted(m.target.station.icao for m in markets) == ["KDAL", "KLAX"]
    err = capsys.readouterr().err
    assert "900003" in err and "skip" in err.lower()


def _precip_events_body() -> list[dict[str, Any]]:
    nyc = json.loads((FIXTURES / "polymarket_precip_monthly_nyc.json").read_text())
    sea = json.loads((FIXTURES / "polymarket_precip_monthly_seattle.json").read_text())
    broken = dict(nyc)  # city is in the registry but the rules omit the station
    broken["id"] = "999999"
    broken["description"] = "no resolution station named here"
    temp = {"title": "Highest temperature in NYC on June 3?", "markets": []}
    return [nyc, sea, broken, temp]


def test_discover_precip_markets_filters_and_skips_unparseable(httpx_mock, capsys):
    httpx_mock.add_response(url=re.compile(re.escape(GAMMA_EVENTS_URL)), json=_precip_events_body())
    with httpx.Client() as client:
        markets = discover_precip_markets(client)
    # NYC and Seattle parse; the broken precip event is skipped; the temperature
    # event is filtered out (not a precip title).
    assert sorted(m.id for m in markets) == ["531291", "531299"]
    err = capsys.readouterr().err
    assert "999999" in err and "skip" in err.lower()


def test_discover_markets_skips_event_missing_required_key(httpx_mock, capsys):
    # A title-matched event that passes _is_us_temp_event but is missing a key
    # that parse_market indexes directly (e.g. 'description') should be skipped
    # with a warning, not crash the whole discovery run.
    # Pre-fix: KeyError propagates past except ValueError and aborts discovery.
    nyc_event = next(e for e in _events_body() if e.get("id") == "533147")
    keyless = {
        "id": "888001",
        "title": "Highest temperature in NYC on May 30?",
        "slug": "highest-temperature-nyc-may-30",
        "endDate": "2026-05-30T12:00:00Z",
        "markets": [],
        # 'description' is intentionally absent so parse_market raises KeyError
    }
    httpx_mock.add_response(url=re.compile(re.escape(GAMMA_EVENTS_URL)), json=[keyless, nyc_event])
    with httpx.Client() as client:
        markets = discover_markets(client)
    assert len(markets) == 1
    assert markets[0].id == "533147"
    err = capsys.readouterr().err
    assert "888001" in err and "skip" in err.lower()


def test_discover_precip_markets_skips_event_missing_required_key(httpx_mock, capsys):
    # A title-matched precip event missing the 'markets' key should be skipped
    # with a warning, not crash the whole precip discovery run.
    # Pre-fix: KeyError propagates past except ValueError and aborts discovery.
    nyc = json.loads((FIXTURES / "polymarket_precip_monthly_nyc.json").read_text())
    keyless = {
        "id": "888002",
        "title": nyc["title"],
        "slug": "nyc-precip-missing-markets",
        "description": nyc["description"],
        "endDate": nyc["endDate"],
        # 'markets' is intentionally absent so parse_precip_event raises KeyError
    }
    httpx_mock.add_response(url=re.compile(re.escape(GAMMA_EVENTS_URL)), json=[keyless, nyc])
    with httpx.Client() as client:
        markets = discover_precip_markets(client)
    assert len(markets) == 1
    assert markets[0].id == "531291"
    err = capsys.readouterr().err
    assert "888002" in err and "skip" in err.lower()


def _london_fixture_event() -> dict[str, Any]:
    return json.loads((FIXTURES / "polymarket_intl_london.json").read_text())


def test_discover_markets_parses_real_london_fixture(httpx_mock):
    # London (EGLC, C) from a saved live fixture: single-degree buckets must parse.
    event = _london_fixture_event()
    httpx_mock.add_response(url=re.compile(re.escape(GAMMA_EVENTS_URL)), json=[event])
    with httpx.Client() as client:
        markets = discover_markets(client)
    assert len(markets) == 1
    m = markets[0]
    assert m.target.station.icao == "EGLC"
    assert m.target.station.unit == "C"
    assert len(m.buckets) == 11
    # First bucket is the below-tail, last is the above-tail.
    assert m.buckets[0].kind == "below"
    assert m.buckets[-1].kind == "above"
    # Interior buckets are single-degree ranges (lo=hi).
    interior = [b for b in m.buckets if b.kind == "range"]
    assert all(b.lo == b.hi for b in interior)
