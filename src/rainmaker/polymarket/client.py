import sys
from typing import Any, cast

import httpx

from rainmaker.config import STATIONS
from rainmaker.polymarket.markets import Market, parse_city, parse_market

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"


def fetch_weather_events(
    client: httpx.Client, *, page_size: int = 100, max_pages: int = 6
) -> list[dict[str, Any]]:
    """Page through Gamma's active weather events. Raises on any HTTP error."""
    events: list[dict[str, Any]] = []
    for page in range(max_pages):
        resp = client.get(
            GAMMA_EVENTS_URL,
            params={
                "closed": "false",
                "active": "true",
                "tag_slug": "weather",
                "limit": str(page_size),
                "offset": str(page * page_size),
            },
        )
        resp.raise_for_status()
        batch = cast(list[dict[str, Any]], resp.json())
        events.extend(batch)
        if len(batch) < page_size:
            break
    return events


def _is_us_temp_event(event: dict[str, Any]) -> bool:
    try:
        city = parse_city(event.get("title", ""))
    except ValueError:
        return False
    return city in STATIONS


def discover_markets(client: httpx.Client) -> list[Market]:
    """Fetch live weather events and parse the US-city temperature markets.

    A market that fails to parse (for example its description does not name the
    resolution station) is skipped with a warning so one bad market does not
    abort the whole run. Polymarket being down still aborts upstream.
    """
    markets: list[Market] = []
    for ev in fetch_weather_events(client):
        if not _is_us_temp_event(ev):
            continue
        try:
            markets.append(parse_market(ev))
        except ValueError as exc:
            print(f"skipping market {ev.get('id')}: {exc}", file=sys.stderr)
    return markets
