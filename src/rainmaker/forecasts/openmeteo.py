import re
from typing import Any

from rainmaker.config import (
    OPENMETEO_MODELS,
    Target,
)
from rainmaker.forecasts.base import ForecastSample

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
_MEMBER_RE = re.compile(r"temperature_2m_max_member(\d+)")


def _daily_field(variable: str) -> str:
    if variable != "TMAX":
        raise NotImplementedError("Phase 1 supports TMAX only")
    return "temperature_2m_max"


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
    out: list[ForecastSample] = []
    for model in OPENMETEO_MODELS:
        values: list[float | None] | None = daily.get(f"{field}_{model}")
        if not values or values[idx] is None:
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
                value_f=float(values[idx]),  # type: ignore[arg-type]
                issued_at=None,
            )
        )
    return out
