import json
import re
from pathlib import Path

import httpx

from rainmaker.config import (
    KALSHI_API_BASE,
    KALSHI_HIGH_SERIES,
    KALSHI_LOW_SERIES,
    KALSHI_RAIN_SERIES,
)
from rainmaker.kalshi.client import discover_kalshi_markets, discover_kalshi_precip_markets

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE = json.loads((FIXTURES / "kalshi_high_temp_nyc.json").read_text())
RAIN_FIXTURE = json.loads((FIXTURES / "kalshi_rain_nyc.json").read_text())
_URL = re.compile(re.escape(KALSHI_API_BASE))
_EMPTY = {"cursor": "", "markets": []}


def _low_fixture():
    rule = (
        "If the minimum temperature recorded at New York City for Jun 8, 2026, is "
        "greater than 67 fahrenheit according to the National Weather Service's "
        "Climatological Report (Daily), then Yes."
    )
    return {
        "cursor": "",
        "markets": [
            {
                "event_ticker": "KXLOWTNYC-26JUN08",
                "ticker": "KXLOWTNYC-26JUN08-T67",
                "strike_type": "greater",
                "floor_strike": 67,
                "subtitle": "above 67",
                "yes_bid_dollars": "0.4000",
                "yes_ask_dollars": "0.4200",
                "no_ask_dollars": "0.6000",
                "last_price_dollars": "0.4100",
                "rules_primary": rule,
            }
        ],
    }


def test_discover_parses_high_and_low(httpx_mock):
    # one response per series, in request order: high series first, then low. NYC
    # returns the ladder for each; every other city returns an empty page.
    for city in KALSHI_HIGH_SERIES:
        httpx_mock.add_response(url=_URL, json=FIXTURE if city == "NYC" else _EMPTY)
    for city in KALSHI_LOW_SERIES:
        httpx_mock.add_response(url=_URL, json=_low_fixture() if city == "NYC" else _EMPTY)
    with httpx.Client() as client:
        markets = discover_kalshi_markets(client)
    high = [m for m in markets if m.id == "KXHIGHNY-26JUN08"]
    low = [m for m in markets if m.id == "KXLOWTNYC-26JUN08"]
    assert len(high) == 1 and high[0].target.variable == "TMAX"
    assert high[0].target.station.icao == "KNYC" and len(high[0].buckets) == 2
    assert len(low) == 1 and low[0].target.variable == "TMIN"
    assert low[0].target.station.icao == "KNYC"  # reuses the high-temp CLI station


def test_discover_kalshi_outage_is_non_fatal(httpx_mock):
    # every series request fails; discovery swallows it and returns no markets
    # rather than aborting the run (Kalshi is the secondary venue).
    for _ in list(KALSHI_HIGH_SERIES) + list(KALSHI_LOW_SERIES):
        httpx_mock.add_exception(httpx.ConnectError("kalshi down"))
    with httpx.Client() as client:
        markets = discover_kalshi_markets(client)
    assert markets == []


def test_discover_precip_parses_nyc(httpx_mock):
    for city in KALSHI_RAIN_SERIES:
        httpx_mock.add_response(url=_URL, json=RAIN_FIXTURE if city == "NYC" else _EMPTY)
    with httpx.Client() as client:
        markets = discover_kalshi_precip_markets(client)
    nyc = [m for m in markets if m.id == "KXRAINNYCM-26JUN"]
    assert len(nyc) == 1
    assert nyc[0].target.variable == "PRCP"
    assert nyc[0].target.station.ghcnd_id == "USW00094728"
    assert len(nyc[0].buckets) == 2
