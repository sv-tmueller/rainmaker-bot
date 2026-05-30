from typing import Any, cast

import httpx

from rainmaker.config import STATIONS
from rainmaker.polymarket.markets import _TITLE_RE, Market, parse_market

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
    match = _TITLE_RE.match(event.get("title", ""))
    if match is None:
        return False
    return match.group(2).strip() in STATIONS


def discover_markets(client: httpx.Client) -> list[Market]:
    """Fetch live weather events and parse the US-city temperature markets."""
    return [parse_market(ev) for ev in fetch_weather_events(client) if _is_us_temp_event(ev)]
