import re
from typing import Any, cast

import httpx

from rainmaker.config import (
    OPENMETEO_ENSEMBLE_MODELS,
    OPENMETEO_FORECAST_DAYS,
    OPENMETEO_MODELS,
    Target,
)
from rainmaker.forecasts.base import ForecastSample

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"


_DAILY_FIELD = {"TMAX": "temperature_2m_max", "TMIN": "temperature_2m_min"}


def _daily_field(variable: str) -> str:
    try:
        return _DAILY_FIELD[variable]
    except KeyError:
        raise NotImplementedError(f"unsupported variable {variable}") from None


def _check_fahrenheit(data: dict[str, Any], field: str) -> None:
    units = data["daily_units"]
    temp_units = [v for k, v in units.items() if k.startswith(field)]
    if not temp_units or not all(str(u).endswith("F") for u in temp_units):
        raise ValueError(f"expected Fahrenheit from Open-Meteo, got {temp_units}")


def _target_index(daily: dict[str, Any], target: Target) -> int | None:
    iso = target.local_date.isoformat()
    times: list[str] = daily["time"]
    return times.index(iso) if iso in times else None


def parse_multimodel(data: dict[str, Any], target: Target) -> list[ForecastSample]:
    daily: dict[str, Any] = data["daily"]
    idx = _target_index(daily, target)
    if idx is None:
        return []
    field = _daily_field(target.variable)
    _check_fahrenheit(data, field)
    out: list[ForecastSample] = []
    for model in OPENMETEO_MODELS:
        values = daily.get(f"{field}_{model}")
        if not values:
            continue
        val = values[idx]
        if val is None:
            continue
        out.append(
            ForecastSample(
                source="open-meteo",
                model=model,
                member=None,
                station=target.station.icao,
                variable=target.variable,
                target_date=target.local_date,
                lead_time_days=idx,
                value_f=float(val),
                issued_at=None,
            )
        )
    return out


def parse_ensemble(data: dict[str, Any], target: Target, ens_model: str) -> list[ForecastSample]:
    field = _daily_field(target.variable)
    _check_fahrenheit(data, field)
    # Match only perturbed members (temperature_2m_max_memberNN); the bare
    # temperature_2m_max control run is intentionally excluded from the sample set.
    member_re = re.compile(rf"{re.escape(field)}_member(\d+)")
    daily: dict[str, Any] = data["daily"]
    idx = _target_index(daily, target)
    if idx is None:
        return []
    out: list[ForecastSample] = []
    for key, values in daily.items():
        match = member_re.fullmatch(key)
        if match is None or not values:
            continue
        val = values[idx]
        if val is None:
            continue
        out.append(
            ForecastSample(
                source="open-meteo",
                model=f"{ens_model}_ens",
                member=int(match.group(1)),
                station=target.station.icao,
                variable=target.variable,
                target_date=target.local_date,
                lead_time_days=idx,
                value_f=float(val),
                issued_at=None,
            )
        )
    return out


def _common_params(target: Target) -> dict[str, str]:
    return {
        "latitude": str(target.station.lat),
        "longitude": str(target.station.lon),
        "daily": _daily_field(target.variable),
        "temperature_unit": "fahrenheit",
        "timezone": target.station.timezone,
        "forecast_days": str(OPENMETEO_FORECAST_DAYS),
    }


def fetch_raw_multimodel(target: Target, client: httpx.Client) -> dict[str, Any]:
    params = _common_params(target) | {"models": ",".join(OPENMETEO_MODELS)}
    resp = client.get(FORECAST_URL, params=params)
    resp.raise_for_status()
    return cast(dict[str, Any], resp.json())


def fetch_raw_ensemble(target: Target, client: httpx.Client, ens_model: str) -> dict[str, Any]:
    params = _common_params(target) | {"models": ens_model}
    resp = client.get(ENSEMBLE_URL, params=params)
    resp.raise_for_status()
    return cast(dict[str, Any], resp.json())


class OpenMeteoSource:
    name = "open-meteo"

    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def fetch(self, target: Target) -> list[ForecastSample]:
        samples = parse_multimodel(fetch_raw_multimodel(target, self.client), target)
        for ens_model in OPENMETEO_ENSEMBLE_MODELS:
            data = fetch_raw_ensemble(target, self.client, ens_model)
            samples.extend(parse_ensemble(data, target, ens_model))
        return samples
