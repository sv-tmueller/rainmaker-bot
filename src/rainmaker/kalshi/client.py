import sys
from collections import defaultdict
from typing import Any

import httpx

from rainmaker.config import (
    KALSHI_API_BASE,
    KALSHI_HIGH_SERIES,
    KALSHI_LOW_SERIES,
    KALSHI_PRECIP_STATIONS,
    KALSHI_RAIN_SERIES,
    KALSHI_STATIONS,
    Variable,
)
from rainmaker.kalshi.markets import parse_kalshi_event
from rainmaker.kalshi.precip_markets import parse_kalshi_precip_event
from rainmaker.polymarket.markets import Market
from rainmaker.polymarket.precip_markets import PrecipMonthlyMarket


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


def _open_events(client: httpx.Client, series: str) -> dict[str, list[dict[str, Any]]] | None:
    """One series' open markets grouped by event ticker; None on HTTP error.

    Kalshi is the secondary venue, so an outage degrades to fewer markets (None
    here, skipped by the caller) rather than aborting the run.
    """
    try:
        raw = _fetch_open_markets(client, series)
    except httpx.HTTPError as exc:
        print(f"Kalshi unavailable for {series}, skipping: {exc}", file=sys.stderr)
        return None
    by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for m in raw:
        ticker = m.get("event_ticker")
        if not ticker:
            continue
        by_event[ticker].append(m)
    return by_event


def _discover_temp(
    client: httpx.Client, series_map: dict[str, str], variable: Variable
) -> list[Market]:
    markets: list[Market] = []
    for city, series in series_map.items():
        events = _open_events(client, series)
        if events is None:
            continue
        station = KALSHI_STATIONS[city]
        for event_ticker, event_markets in events.items():
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


def discover_kalshi_precip_markets(client: httpx.Client) -> list[PrecipMonthlyMarket]:
    """Discover live Kalshi monthly rain markets (read-only).

    Strike ladders on total monthly precipitation, settled on the per-city NWS
    Climatological Report. The precip parallel of discover_kalshi_markets.
    """
    markets: list[PrecipMonthlyMarket] = []
    for city, series in KALSHI_RAIN_SERIES.items():
        events = _open_events(client, series)
        if events is None:
            continue
        station = KALSHI_PRECIP_STATIONS[city]
        for event_ticker, event_markets in events.items():
            try:
                markets.append(parse_kalshi_precip_event(city, station, event_markets))
            except ValueError as exc:
                print(f"skipping Kalshi precip event {event_ticker}: {exc}", file=sys.stderr)
    return markets
