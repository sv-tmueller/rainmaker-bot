"""ASOS daily extreme actuals from Iowa State Mesonet.

Fetches hourly ASOS (Automated Surface Observing System) observations from the
Iowa State Mesonet API, reduces them to daily TMAX or TMIN, and returns a
{date: float} mapping.

US path (local_tz=None): returns Fahrenheit, UTC day bucketing, report_type=3.
Intl path (local_tz set): returns Celsius, local-day bucketing, no report_type filter.

Used for Polymarket settlement only. Kalshi temperature markets settle on the
NOAA daily climate report (NCEI GHCND); Kalshi uses fetch_actuals from backfill.
PRCP (monthly total) has no ASOS path; Polymarket and Kalshi PRCP both use NCEI GSOM.
"""

from __future__ import annotations

import csv
import io
import time
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

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

# Map ICAO station id (as stored on Station.icao) to the IEM station code.
# US stations drop the K prefix; intl stations pass the 4-letter ICAO unchanged.
# All 11 US Polymarket cities must be present; intl cities added for settlement (#190).
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
    # International stations: 4-letter ICAO passed unchanged, all-obs mode (#190)
    "EGLC": "EGLC",  # London City Airport
    "LFPB": "LFPB",  # Paris-Le Bourget
    "EFHK": "EFHK",  # Helsinki Vantaa
    "SBGR": "SBGR",  # Sao Paulo-Guarulhos
}


def fetch_asos_daily_extreme(
    asos_station: str,
    start: date,
    end: date,
    client: httpx.Client,
    variable: str = "TMAX",
    local_tz: str | None = None,
    target_date: date | None = None,
) -> dict[date, float]:
    """Daily TMAX or TMIN from Iowa State Mesonet ASOS.

    US path (local_tz=None):
      Returns Fahrenheit. Buckets observations by UTC day. Sends report_type=3
      (routine hourly METAR only). Raises httpx.HTTPStatusError on HTTP errors.

    Intl path (local_tz set, target_date set):
      Returns Celsius. Buckets observations by local calendar day using ZoneInfo.
      Omits report_type (all obs types including SPECI). Only the target_date key
      is returned; the caller must pass a UTC window that fully covers the local day
      (padded by ±1 UTC day is sufficient for any timezone offset).

    On HTTP 429 the function backs off (Retry-After header or default) and
    retries up to ASOS_MAX_RETRIES times total. A huge Retry-After is capped
    at ASOS_429_MAX_WAIT_S to avoid hanging the run.
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
    }
    if local_tz is None:
        # US path: routine hourly METAR only (excludes SPECI).
        params["report_type"] = "3"
    # Intl path: no report_type - returns all obs types (includes SPECI).

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

    if local_tz is not None:
        # Intl path: convert UTC timestamps to local, return Celsius for target_date only.
        # local_tz and target_date are always set together; assert narrows the type for mypy.
        assert target_date is not None, "target_date required when local_tz is set"
        tz = ZoneInfo(local_tz)
        readings: list[float] = []
        reader = csv.DictReader(
            line for line in io.StringIO(resp.text).readlines() if not line.startswith("#")
        )
        for row in reader:
            valid_str = row.get("valid", "")
            tmpc_str = row.get("tmpc", "M")
            if not valid_str or tmpc_str in ("M", ""):
                continue
            try:
                # IEM timestamps are "YYYY-MM-DD HH:MM" in UTC.
                utc_dt = datetime.strptime(valid_str[:16], "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
                celsius = float(tmpc_str)
            except ValueError:
                continue
            local_date = utc_dt.astimezone(tz).date()
            if local_date == target_date:
                readings.append(celsius)
        if not readings:
            return {}
        return {target_date: reduce(readings)}

    # US path: UTC day bucketing, return Fahrenheit.
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
