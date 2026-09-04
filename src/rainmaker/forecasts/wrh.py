"""Hourly temperature actuals from weather.gov wrh/timeseries (Synoptic Data API).

Fetches all-available temperature observations (5-minute resolution) from the
Synoptic Data API backing weather.gov's wrh/timeseries page, reduces them to
daily TMAX or TMIN using local-calendar-day bucketing, and returns a
{date: float} mapping in Fahrenheit.

This is the primary settlement source for US Polymarket TMAX/TMIN markets,
matching the resolution rule documented in
docs/architecture/noaa-wrh-vs-asos-comparison.md. The ASOS fetcher (asos.py)
serves as a degraded fallback.

Local-day bucketing uses the station's timezone (obtimezone=local in the API
response). The API returns timestamps with numeric offsets like
"2026-08-20T14:55:00-0500"; we convert to the station's ZoneInfo to determine
the local calendar day.
"""

from __future__ import annotations

import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx

# Synoptic Data API endpoint backing weather.gov/wrh/timeseries.
SYNOPTIC_API_URL = "https://api.synopticdata.com/v2/stations/timeseries"

# Token embedded in weather.gov's apiKey.js. Public, not a secret.
SYNOPTIC_TOKEN = "7c76618b66c74aee913bdbae4b448bdd"

# Referer header required by the Synoptic API when using the weather.gov token.
WRH_REFERER = "https://www.weather.gov/wrh/timeseries"

# 429 rate-limit handling: retry up to WRH_MAX_RETRIES times.
# Sleep for Retry-After seconds (capped at WRH_429_MAX_WAIT_S) between attempts.
# Default backoff when Retry-After header is absent. Mirrors asos.py.
WRH_MAX_RETRIES = 4
WRH_429_MAX_WAIT_S = 60.0
WRH_429_DEFAULT_WAIT_S = 5.0


def fetch_wrh_hourly_extreme(
    station_icao: str,
    start: date,
    end: date,
    client: httpx.Client,
    variable: str = "TMAX",
    *,
    timezone: str = "America/New_York",
) -> dict[date, float]:
    """Daily TMAX or TMIN from the Synoptic Data API (weather.gov wrh/timeseries).

    Fetches all-available temperature observations for the given station and
    date range, buckets them by local calendar day (using the station's
    timezone), and returns the daily max (TMAX) or min (TMIN) in Fahrenheit.

    Args:
        station_icao: ICAO station identifier (e.g. "KLGA"). Passed to the API
            as-is (the API accepts uppercase STID).
        start: Start date (inclusive). Sent as YYYYmmddHHMM with 0000.
        end: End date (inclusive). Sent as YYYYmmddHHMM with 2359.
        client: An httpx.Client for making HTTP requests.
        variable: "TMAX" for daily high, "TMIN" for daily low.
        timezone: IANA timezone string for local-day bucketing (e.g.
            "America/New_York"). Must match the station's actual timezone.

    Returns:
        A {date: float} mapping where each date is a local calendar day within
        the [start, end] range and the float is the max (TMAX) or min (TMIN)
        temperature in Fahrenheit. Days with no valid observations are omitted.

    Raises:
        httpx.HTTPStatusError: on non-429 HTTP errors, or after exhausting
            429 retries.
        ValueError: if the Synoptic API returns an error response (RESPONSE_CODE
            != 1) after a successful HTTP 200.
    """
    params: dict[str, str] = {
        "STID": station_icao.upper(),
        "showemptystations": "1",
        "units": "temp|F,english",
        "start": start.strftime("%Y%m%d%H%M"),
        "end": end.strftime("%Y%m%d") + "2359",
        "complete": "1",
        "token": SYNOPTIC_TOKEN,
        "obtimezone": "local",
    }
    headers = {
        "Referer": f"{WRH_REFERER}?site={station_icao.lower()}",
        "Origin": "https://www.weather.gov",
    }

    resp: httpx.Response | None = None
    for attempt in range(WRH_MAX_RETRIES):
        resp = client.get(SYNOPTIC_API_URL, params=params, headers=headers)
        if resp.status_code == 429:
            if attempt + 1 == WRH_MAX_RETRIES:
                resp.raise_for_status()
            retry_after_str = resp.headers.get("Retry-After", "")
            try:
                wait = min(float(retry_after_str), WRH_429_MAX_WAIT_S)
            except ValueError:
                wait = WRH_429_DEFAULT_WAIT_S
            time.sleep(wait)
            continue
        resp.raise_for_status()
        break

    assert resp is not None  # loop always runs at least once

    data = resp.json()
    summary = data.get("SUMMARY", {})
    if summary.get("RESPONSE_CODE") != 1:
        raise ValueError(
            f"Synoptic API error for {station_icao}: "
            f"RESPONSE_CODE={summary.get('RESPONSE_CODE')} "
            f"RESPONSE_MESSAGE={summary.get('RESPONSE_MESSAGE', '')}"
        )

    stations = data.get("STATION", [])
    if not stations:
        return {}

    observations = stations[0].get("OBSERVATIONS", {})
    timestamps: list[str] = observations.get("date_time", [])
    temps_raw: list[float | None] = observations.get("air_temp_set_1", [])

    tz = ZoneInfo(timezone)
    reduce_fn = max if variable == "TMAX" else min

    by_day: dict[date, list[float]] = {}
    for ts_str, temp_val in zip(timestamps, temps_raw, strict=False):
        if temp_val is None:
            continue
        try:
            # Timestamps look like "2026-08-20T14:55:00-0500".
            # fromisoformat handles the numeric offset in Python 3.11+.
            dt = datetime.fromisoformat(ts_str)
        except (ValueError, TypeError):
            continue
        local_date = dt.astimezone(tz).date()
        by_day.setdefault(local_date, []).append(float(temp_val))

    return {d: reduce_fn(readings) for d, readings in by_day.items() if readings}
