from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from rainmaker.config import Target
from rainmaker.forecasts.base import ForecastSample

NWS_BASE = "https://api.weather.gov"


def parse(forecast_json: dict, target: Target) -> list[ForecastSample]:
    if target.variable != "TMAX":
        raise NotImplementedError("Phase 1 supports TMAX only")
    props = forecast_json["properties"]
    issued_at = datetime.fromisoformat(props["updateTime"])
    issued_local = issued_at.astimezone(ZoneInfo(target.station.timezone)).date()
    for period in props["periods"]:
        start = datetime.fromisoformat(period["startTime"])
        if period["isDaytime"] and start.date() == target.local_date:
            if period["temperatureUnit"] != "F":
                raise ValueError(f"expected Fahrenheit, got {period['temperatureUnit']}")
            return [
                ForecastSample(
                    source="nws",
                    model="nws",
                    member=None,
                    station=target.station.icao,
                    variable="TMAX",
                    target_date=target.local_date,
                    lead_time_days=(target.local_date - issued_local).days,
                    value_f=float(period["temperature"]),
                    issued_at=issued_at,
                )
            ]
    return []


def fetch_raw(target: Target, client: httpx.Client) -> dict:
    points = client.get(f"{NWS_BASE}/points/{target.station.lat},{target.station.lon}")
    points.raise_for_status()
    forecast_url = points.json()["properties"]["forecast"]
    forecast = client.get(forecast_url)
    forecast.raise_for_status()
    return forecast.json()


class NwsSource:
    name = "nws"

    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def fetch(self, target: Target) -> list[ForecastSample]:
        return parse(fetch_raw(target, self.client), target)
