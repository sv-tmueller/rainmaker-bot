import sys
from collections import defaultdict
from typing import Any

import httpx

from rainmaker.config import KALSHI_API_BASE, KALSHI_HIGH_SERIES, KALSHI_STATIONS
from rainmaker.kalshi.markets import parse_kalshi_event
from rainmaker.polymarket.markets import Market


def _fetch_open_markets(
    client: httpx.Client, series_ticker: str, *, max_pages: int = 6
) -> list[dict[str, Any]]:
    """Page through one series' open markets. Raises on any HTTP error."""
    out: list[dict[str, Any]] = []
    cursor = ""
    for _ in range(max_pages):
        params = {"series_ticker": series_ticker, "status": "open", "limit": "200"}
        if cursor:
            params["cursor"] = cursor
        resp = client.get(f"{KALSHI_API_BASE}/markets", params=params)
        resp.raise_for_status()
        body = resp.json()
        out.extend(body.get("markets", []))
        cursor = body.get("cursor") or ""
        if not cursor:
            break
    return out


def discover_kalshi_markets(client: httpx.Client) -> list[Market]:
    """Discover live Kalshi daily high-temp markets for the configured cities.

    Read-only. Kalshi is the secondary venue: an HTTP error on a series logs a
    warning and yields no markets for it so the run continues on Polymarket. A
    single event that fails to parse is skipped with a warning.
    """
    markets: list[Market] = []
    for city, series in KALSHI_HIGH_SERIES.items():
        station = KALSHI_STATIONS[city]
        try:
            raw = _fetch_open_markets(client, series)
        except httpx.HTTPError as exc:
            print(f"Kalshi unavailable for {series}, skipping: {exc}", file=sys.stderr)
            continue
        by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for m in raw:
            by_event[m["event_ticker"]].append(m)
        for event_ticker, event_markets in by_event.items():
            try:
                markets.append(parse_kalshi_event(city, station, event_markets))
            except ValueError as exc:
                print(f"skipping Kalshi event {event_ticker}: {exc}", file=sys.stderr)
    return markets
