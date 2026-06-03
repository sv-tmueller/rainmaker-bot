import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from rainmaker.polymarket.client import GAMMA_EVENTS_URL, discover_markets, fetch_weather_events

FIXTURES = Path(__file__).parent / "fixtures"


def _events_body() -> list[dict[str, Any]]:
    return json.loads((FIXTURES / "polymarket_weather_events.json").read_text())


def test_discover_markets_filters_to_us_temp_markets(httpx_mock):
    httpx_mock.add_response(url=re.compile(re.escape(GAMMA_EVENTS_URL)), json=_events_body())
    with httpx.Client() as client:
        markets = discover_markets(client)
    assert len(markets) == 1
    assert markets[0].id == "533147"
    assert markets[0].target.station.icao == "KLGA"
    assert len(markets[0].buckets) == 11


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


def test_discover_skips_unparseable_and_drops_international(httpx_mock, capsys):
    httpx_mock.add_response(url=re.compile(re.escape(GAMMA_EVENTS_URL)), json=_multicity_body())
    with httpx.Client() as client:
        markets = discover_markets(client)
    # Los Angeles (multi-word) and Dallas (trap KDAL) are kept; NYC is skipped
    # because its description omits KLGA; London is filtered (not US registry).
    assert sorted(m.target.station.icao for m in markets) == ["KDAL", "KLAX"]
    err = capsys.readouterr().err
    assert "900003" in err and "skip" in err.lower()
