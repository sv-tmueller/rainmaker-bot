import json
import re
from pathlib import Path

import httpx

from rainmaker.config import KALSHI_API_BASE, KALSHI_HIGH_SERIES
from rainmaker.kalshi.client import discover_kalshi_markets

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "kalshi_high_temp_nyc.json").read_text()
)
_URL = re.compile(re.escape(KALSHI_API_BASE))
_EMPTY = {"cursor": "", "markets": []}


def test_discover_parses_nyc_ladder(httpx_mock):
    # one response per configured series, in registry (request) order: NYC returns
    # the fixture ladder, every other city returns an empty page.
    for city in KALSHI_HIGH_SERIES:
        body = FIXTURE if city == "NYC" else _EMPTY
        httpx_mock.add_response(url=_URL, json=body)
    with httpx.Client() as client:
        markets = discover_kalshi_markets(client)
    nyc = [m for m in markets if m.id == "KXHIGHNY-26JUN08"]
    assert len(nyc) == 1
    assert nyc[0].target.station.icao == "KNYC"
    assert nyc[0].target.variable == "TMAX"
    assert len(nyc[0].buckets) == 2


def test_discover_kalshi_outage_is_non_fatal(httpx_mock):
    # every series request fails; discovery swallows it and returns no markets
    # rather than aborting the run (Kalshi is the secondary venue).
    for _ in KALSHI_HIGH_SERIES:
        httpx_mock.add_exception(httpx.ConnectError("kalshi down"))
    with httpx.Client() as client:
        markets = discover_kalshi_markets(client)
    assert markets == []
