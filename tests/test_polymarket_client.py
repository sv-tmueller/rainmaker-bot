import json
import re
from pathlib import Path

import httpx
import pytest

from rainmaker.polymarket.client import GAMMA_EVENTS_URL, discover_markets, fetch_weather_events

FIXTURES = Path(__file__).parent / "fixtures"


def _events_body() -> list[dict]:
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
