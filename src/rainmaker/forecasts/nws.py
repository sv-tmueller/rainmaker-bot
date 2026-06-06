from datetime import datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

import httpx

from rainmaker.config import Target
from rainmaker.forecasts.base import ForecastSample

NWS_BASE = "https://api.weather.gov"


def parse(forecast_json: dict[str, Any], target: Target) -> list[ForecastSample]:
    props = forecast_json["properties"]
    issued_at = datetime.fromisoformat(props["updateTime"])
    tz = ZoneInfo(target.station.timezone)
    issued_local = issued_at.astimezone(tz).date()
    # TMAX is the daytime high on the target day. TMIN is the overnight low that
    # GHCND settles to, which NWS reports in the night period starting the evening
    # before (isDaytime false, local start date == target - 1 day).
    want_daytime = target.variable == "TMAX"
    match_date = target.local_date if want_daytime else target.local_date - timedelta(days=1)
    for period in props["periods"]:
        start_local = datetime.fromisoformat(period["startTime"]).astimezone(tz)
        if period["isDaytime"] == want_daytime and start_local.date() == match_date:
            if period["temperatureUnit"] != "F":
                raise ValueError(f"expected Fahrenheit, got {period['temperatureUnit']}")
            return [
                ForecastSample(
                    source="nws",
                    model="nws",
                    member=None,
                    station=target.station.icao,
                    variable=target.variable,
                    target_date=target.local_date,
                    lead_time_days=(target.local_date - issued_local).days,
                    value_f=float(period["temperature"]),
                    issued_at=issued_at,
                )
            ]
    return []


def fetch_raw(target: Target, client: httpx.Client) -> dict[str, Any]:
    points = client.get(f"{NWS_BASE}/points/{target.station.lat},{target.station.lon}")
    points.raise_for_status()
    forecast_url = points.json()["properties"]["forecast"]
    forecast = client.get(forecast_url)
    forecast.raise_for_status()
    return cast(dict[str, Any], forecast.json())


class NwsSource:
    name = "nws"

    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def fetch(self, target: Target) -> list[ForecastSample]:
        return parse(fetch_raw(target, self.client), target)
