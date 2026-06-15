"""ASOS daily extreme actuals from Iowa State Mesonet.

Fetches hourly ASOS (Automated Surface Observing System) observations from the
Iowa State Mesonet API, reduces them to daily TMAX or TMIN, and returns a
{date: float} mapping in degrees Fahrenheit.

Used for Polymarket settlement only. Kalshi temperature markets settle on the
NOAA daily climate report (NCEI GHCND); Kalshi uses fetch_actuals from backfill.
PRCP (monthly total) has no ASOS path; Polymarket and Kalshi PRCP both use NCEI GSOM.
"""

from __future__ import annotations

import csv
import io
import time
from datetime import date

import httpx

# Iowa State Mesonet ASOS API - near-real-time hourly ASOS observations.
# The same source used in the settlement_divergence spike (#101a).
MESONET_ASOS_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"

# 429 rate-limit handling: retry up to ASOS_MAX_RETRIES times.
# Sleep for Retry-After seconds (capped at ASOS_429_MAX_WAIT_S) between attempts.
# Default backoff when Retry-After header is absent.
ASOS_MAX_RETRIES = 4
ASOS_429_MAX_WAIT_S = 60.0
ASOS_429_DEFAULT_WAIT_S = 5.0

# Map ICAO station id (as stored on Station.icao) to the 3-letter FAA code
# used by Iowa State Mesonet. US stations drop the K prefix.
# All 11 Polymarket cities must be present.
ICAO_TO_ASOS_STATION: dict[str, str] = {
    "KLGA": "LGA",  # NYC LaGuardia
    "KMIA": "MIA",  # Miami Intl
    "KORD": "ORD",  # Chicago O'Hare
    "KDAL": "DAL",  # Dallas Love Field
    "KHOU": "HOU",  # Houston Hobby
    "KLAX": "LAX",  # Los Angeles Intl
    "KSFO": "SFO",  # San Francisco Intl
    "KSEA": "SEA",  # Seattle-Tacoma Intl
    "KAUS": "AUS",  # Austin-Bergstrom Intl
    "KATL": "ATL",  # Atlanta Hartsfield-Jackson
    "KBKF": "BKF",  # Denver / Buckley Space Force Base
}


def fetch_asos_daily_extreme(
    asos_station: str,
    start: date,
    end: date,
    client: httpx.Client,
    variable: str = "TMAX",
) -> dict[date, float]:
    """Daily TMAX or TMIN (degrees F) from Iowa State Mesonet ASOS.

    Fetches hourly tmpc (Celsius) observations, skips missing values ('M' or
    empty), and reduces each UTC day to its daily maximum (TMAX) or minimum
    (TMIN). Raises httpx.HTTPStatusError on HTTP errors (including 429 after
    retries are exhausted).

    On HTTP 429 the function backs off (Retry-After header or default) and
    retries up to ASOS_MAX_RETRIES times total. A huge Retry-After is capped
    at ASOS_429_MAX_WAIT_S to avoid hanging the run.

    Note: timestamps are UTC. UTC-vs-local alignment is the same as the spike
    (#101a): the same measurement window that produced the calibrated flip rates.
    """
    params: dict[str, str | int] = {
        "station": asos_station,
        "data": "tmpc",
        "year1": start.year,
        "month1": start.month,
        "day1": start.day,
        "hour1": 0,
        "minute1": 0,
        "year2": end.year,
        "month2": end.month,
        "day2": end.day,
        "hour2": 23,
        "minute2": 59,
        "tz": "UTC",
        "format": "onlycomma",
        "latlon": "no",
        "elev": "no",
        "report_type": "3",  # routine hourly METAR only (excludes specials)
    }

    resp: httpx.Response | None = None
    for attempt in range(ASOS_MAX_RETRIES):
        resp = client.get(MESONET_ASOS_URL, params=params)
        if resp.status_code == 429:
            if attempt + 1 == ASOS_MAX_RETRIES:
                resp.raise_for_status()
            retry_after_str = resp.headers.get("Retry-After", "")
            try:
                wait = min(float(retry_after_str), ASOS_429_MAX_WAIT_S)
            except ValueError:
                wait = ASOS_429_DEFAULT_WAIT_S
            time.sleep(wait)
            continue
        resp.raise_for_status()
        break

    assert resp is not None  # loop always runs at least once
    reduce = max if variable == "TMAX" else min
    by_day: dict[date, list[float]] = {}

    reader = csv.DictReader(
        line for line in io.StringIO(resp.text).readlines() if not line.startswith("#")
    )
    for row in reader:
        valid_str = row.get("valid", "")
        tmpc_str = row.get("tmpc", "M")
        if not valid_str or tmpc_str in ("M", ""):
            continue
        try:
            day = date.fromisoformat(valid_str[:10])
            celsius = float(tmpc_str)
        except ValueError:
            continue
        fahrenheit = celsius * 9.0 / 5.0 + 32.0
        by_day.setdefault(day, []).append(fahrenheit)

    return {d: reduce(readings) for d, readings in by_day.items() if readings}
