"""Settle recorded markets against NOAA actuals (a proxy for Weather Underground).

For each recorded market whose settlement date has passed and that has no outcome
yet, fetch the NOAA daily extreme for its station/date/variable and record it.
Idempotent: already-settled markets are skipped, and a market whose NOAA data is
not published yet is left for a later run.
"""

import sys
from datetime import date

import httpx

from rainmaker.backfill import fetch_actuals, fetch_monthly_precip
from rainmaker.config import PRECIP_STATIONS, STATIONS
from rainmaker.store.db import Conn
from rainmaker.store.query import unsettled_markets
from rainmaker.store.record import record_outcome


def _legacy_ghcnd(market: dict[str, str]) -> str | None:
    """The settlement GHCND from the city registry, for rows recorded before the
    markets.settlement_ghcnd column existed (it is NULL on those rows)."""
    if market["variable"] == "PRCP":
        precip = PRECIP_STATIONS.get(market["city"])
        return precip.ghcnd_id if precip is not None else None
    station = STATIONS.get(market["city"])
    return station.ghcnd_id if station is not None else None


def run_settlement(
    conn: Conn, client: httpx.Client, today: date, settled_at: str
) -> tuple[int, int]:
    """Settle every unsettled past market that has NOAA data. Returns (settled, waiting)."""
    settled = 0
    waiting = 0
    for m in unsettled_markets(conn, today):
        day = date.fromisoformat(m["settlement_date"])
        # Use the market's exact settlement station (Kalshi NYC = Central Park, not
        # LaGuardia); fall back to the city registry for legacy rows.
        ghcnd = m.get("settlement_ghcnd") or _legacy_ghcnd(m)
        if ghcnd is None:
            print(f"skipping {m['market_id']}: no station for {m['city']!r}", file=sys.stderr)
            continue
        if m["variable"] == "PRCP":
            value = fetch_monthly_precip(ghcnd, day.year, day.month, client)
        else:
            value = fetch_actuals(ghcnd, day, day, client, m["variable"]).get(day)
        if value is None:
            waiting += 1
            continue
        record_outcome(conn, m["market_id"], value, settled_at)
        settled += 1
    return settled, waiting
