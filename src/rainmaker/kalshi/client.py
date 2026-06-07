import sys
from collections import defaultdict
from typing import Any

import httpx

from rainmaker.config import (
    KALSHI_API_BASE,
    KALSHI_HIGH_SERIES,
    KALSHI_LOW_SERIES,
    KALSHI_STATIONS,
    Variable,
)
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


def _discover_temp(
    client: httpx.Client, series_map: dict[str, str], variable: Variable
) -> list[Market]:
    """Discover the temperature markets for one variable across the configured cities.

    A per-series HTTP error logs a warning and skips that series (Kalshi is the
    secondary venue); a single event that fails to parse is skipped with a warning.
    """
    markets: list[Market] = []
    for city, series in series_map.items():
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
                markets.append(parse_kalshi_event(city, station, event_markets, variable=variable))
            except ValueError as exc:
                print(f"skipping Kalshi event {event_ticker}: {exc}", file=sys.stderr)
    return markets


def discover_kalshi_markets(client: httpx.Client) -> list[Market]:
    """Discover live Kalshi daily high- and low-temperature markets (read-only).

    Both settle on the per-city NWS Climatological Report (Daily). Kalshi is the
    secondary venue, so any outage degrades to fewer markets, never an aborted run.
    """
    return _discover_temp(client, KALSHI_HIGH_SERIES, "TMAX") + _discover_temp(
        client, KALSHI_LOW_SERIES, "TMIN"
    )
