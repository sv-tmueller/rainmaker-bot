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


def run_settlement(
    conn: Conn, client: httpx.Client, today: date, settled_at: str
) -> tuple[int, int]:
    """Settle every unsettled past market that has NOAA data. Returns (settled, waiting)."""
    settled = 0
    waiting = 0
    for m in unsettled_markets(conn, today):
        day = date.fromisoformat(m["settlement_date"])
        if m["variable"] == "PRCP":
            precip_station = PRECIP_STATIONS.get(m["city"])
            if precip_station is None:
                print(f"skipping {m['market_id']}: unknown city {m['city']!r}", file=sys.stderr)
                continue
            value = fetch_monthly_precip(precip_station.ghcnd_id, day.year, day.month, client)
        else:
            station = STATIONS.get(m["city"])
            if station is None:
                print(f"skipping {m['market_id']}: unknown city {m['city']!r}", file=sys.stderr)
                continue
            value = fetch_actuals(station.ghcnd_id, day, day, client, m["variable"]).get(day)
        if value is None:
            waiting += 1
            continue
        record_outcome(conn, m["market_id"], value, settled_at)
        settled += 1
    return settled, waiting
