"""Forecast sourcing and monthly-total moments for the precipitation path.

Parallel to the temperature forecast modules: Open-Meteo (multi-model and
ensemble) and NWS QPF supply the daily forecast horizon in inches, NCEI daily
summaries supply observed-to-date and a per-month climatology, and the three
combine into a single (mean, variance) for the monthly total.
"""

import calendar
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel

from rainmaker.backfill import fetch_actuals
from rainmaker.config import OPENMETEO_ENSEMBLE_MODELS, OPENMETEO_FORECAST_DAYS, OPENMETEO_MODELS
from rainmaker.domain import PrecipTarget
from rainmaker.forecasts.base import SourceCoverage
from rainmaker.forecasts.nws import NWS_BASE
from rainmaker.forecasts.openmeteo import ENSEMBLE_URL, FORECAST_URL

_PRECIP_FIELD = "precipitation_sum"
_MM_PER_INCH = 25.4


class PrecipForecastSet(BaseModel):
    target: PrecipTarget
    mean: float
    var: float
    coverage: list[SourceCoverage]
    n_observed_days: int
    n_forecast_days: int
    n_clim_days: int


def monthly_total_moments(
    *,
    observed_total: float,
    forecast_daily: list[list[float]],
    clim_daily_mean: float,
    clim_daily_var: float,
    n_tail_days: int,
    floor: float,
) -> tuple[float, float]:
    """Mean and variance of the monthly total: observed-to-date (deterministic) +
    pooled forecast horizon + climatology tail. Daily precip is treated as
    independent across days (a stated approximation that understates variance)."""
    m_f = sum(statistics.fmean(day) for day in forecast_daily if day)
    v_f = sum(statistics.variance(day) for day in forecast_daily if len(day) >= 2)
    m = observed_total + m_f + n_tail_days * clim_daily_mean
    v = v_f + n_tail_days * clim_daily_var
    return m, max(v, floor)


def _check_inches(data: dict[str, Any]) -> None:
    units = data["daily_units"]
    precip_units = [v for k, v in units.items() if k.startswith(_PRECIP_FIELD)]
    if not precip_units or not all("inch" in str(u) for u in precip_units):
        raise ValueError(f"expected inches from Open-Meteo, got {precip_units}")


def parse_precip_open_meteo(data: dict[str, Any]) -> dict[date, list[float]]:
    """Pool every precipitation_sum series into a per-day list of inch values.

    Multimodel and ensemble responses are pooled identically: any daily key
    starting with precipitation_sum is one member (each model, the ensemble
    control run, and each perturbed member all count once)."""
    _check_inches(data)
    daily: dict[str, Any] = data["daily"]
    times = [date.fromisoformat(t) for t in daily["time"]]
    out: dict[date, list[float]] = {d: [] for d in times}
    for key, values in daily.items():
        if not key.startswith(_PRECIP_FIELD) or not values:
            continue
        for d, v in zip(times, values, strict=False):
            if v is not None:
                out[d].append(float(v))
    return out


def _common_precip_params(target: PrecipTarget) -> dict[str, str]:
    return {
        "latitude": str(target.station.lat),
        "longitude": str(target.station.lon),
        "daily": _PRECIP_FIELD,
        "precipitation_unit": "inch",
        "timezone": target.station.timezone,
        "forecast_days": str(OPENMETEO_FORECAST_DAYS),
    }


def fetch_raw_precip_multimodel(target: PrecipTarget, client: httpx.Client) -> dict[str, Any]:
    params = _common_precip_params(target) | {"models": ",".join(OPENMETEO_MODELS)}
    resp = client.get(FORECAST_URL, params=params)
    resp.raise_for_status()
    return cast(dict[str, Any], resp.json())


def fetch_raw_precip_ensemble(
    target: PrecipTarget, client: httpx.Client, ens_model: str
) -> dict[str, Any]:
    params = _common_precip_params(target) | {"models": ens_model}
    resp = client.get(ENSEMBLE_URL, params=params)
    resp.raise_for_status()
    return cast(dict[str, Any], resp.json())


def parse_nws_qpf(grid_json: dict[str, Any], tz: str) -> dict[date, float]:
    """Daily QPF (inches) from the NWS gridpoint product.

    quantitativePrecipitation is a list of 6-hourly mm amounts over ISO8601
    intervals; each interval is binned to its local start date and summed, then
    converted mm -> inch. The mm unit code is guarded like _check_fahrenheit."""
    qpf = grid_json["properties"]["quantitativePrecipitation"]
    if qpf.get("uom") != "wmoUnit:mm":
        raise ValueError(f"expected NWS QPF in mm, got {qpf.get('uom')!r}")
    zone = ZoneInfo(tz)
    totals: dict[date, float] = defaultdict(float)
    for entry in qpf["values"]:
        if entry["value"] is None:
            continue
        start = datetime.fromisoformat(entry["validTime"].split("/")[0])
        local_date = start.astimezone(zone).date()
        totals[local_date] += float(entry["value"]) / _MM_PER_INCH
    return dict(totals)


def fetch_nws_qpf(target: PrecipTarget, client: httpx.Client) -> dict[date, float]:
    points = client.get(f"{NWS_BASE}/points/{target.station.lat},{target.station.lon}")
    points.raise_for_status()
    grid_url = points.json()["properties"]["forecastGridData"]
    grid = client.get(grid_url)
    grid.raise_for_status()
    return parse_nws_qpf(cast(dict[str, Any], grid.json()), target.station.timezone)


def fetch_precip_climatology(
    ghcnd_id: str, month: int, year: int, client: httpx.Client, *, lookback_years: int
) -> tuple[float, float]:
    """Climatological daily PRCP mean and variance for the target calendar month.

    Reads the prior `lookback_years` of NCEI daily summaries and keeps only the
    target month. Returns (0.0, 0.0) when no history is available."""
    start = date(year - lookback_years, 1, 1)
    end = date(year - 1, 12, 31)
    daily = fetch_actuals(ghcnd_id, start, end, client, "PRCP")
    month_vals = [v for d, v in daily.items() if d.month == month]
    if not month_vals:
        return (0.0, 0.0)
    var = statistics.variance(month_vals) if len(month_vals) >= 2 else 0.0
    return (statistics.fmean(month_vals), var)


def build_precip_forecast_set(
    target: PrecipTarget,
    *,
    today: date,
    client: httpx.Client,
    var_floor: float,
    lookback_years: int,
) -> PrecipForecastSet:
    """Assemble the monthly-total (mean, var) and coverage for one precip market.

    observed-to-date covers the elapsed in-month days, the pooled Open-Meteo and
    NWS forecast covers the horizon days from today, and climatology fills the
    remaining out-of-horizon tail. Only the live forecast sources gate coverage;
    NCEI baselines degrade to neutral values rather than aborting the run."""
    year, month = target.year, target.month
    days_in_month = calendar.monthrange(year, month)[1]
    month_start = date(year, month, 1)
    month_end = date(year, month, days_in_month)

    last_observed = min(today - timedelta(days=1), month_end)
    n_observed_days = (last_observed - month_start).days + 1 if last_observed >= month_start else 0
    observed_total = 0.0
    if n_observed_days:
        actuals = fetch_actuals(target.station.ghcnd_id, month_start, last_observed, client, "PRCP")
        observed_total = sum(v for d, v in actuals.items() if month_start <= d <= last_observed)

    clim_mean, clim_var = fetch_precip_climatology(
        target.station.ghcnd_id, month, year, client, lookback_years=lookback_years
    )

    pooled: dict[date, list[float]] = defaultdict(list)
    coverage: list[SourceCoverage] = []

    om_ok, om_n, om_err = True, 0, None
    try:
        for day, vals in parse_precip_open_meteo(
            fetch_raw_precip_multimodel(target, client)
        ).items():
            pooled[day].extend(vals)
            om_n += len(vals)
        for ens_model in OPENMETEO_ENSEMBLE_MODELS:
            ens = parse_precip_open_meteo(fetch_raw_precip_ensemble(target, client, ens_model))
            for day, vals in ens.items():
                pooled[day].extend(vals)
                om_n += len(vals)
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        om_ok, om_err = False, str(exc)
    coverage.append(SourceCoverage(source="open-meteo", ok=om_ok, n_samples=om_n, error=om_err))

    nws_ok, nws_n, nws_err = True, 0, None
    try:
        for day, inch in fetch_nws_qpf(target, client).items():
            pooled[day].append(inch)
            nws_n += 1
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        nws_ok, nws_err = False, str(exc)
    coverage.append(SourceCoverage(source="nws", ok=nws_ok, n_samples=nws_n, error=nws_err))

    forecast_dates = sorted(d for d in pooled if today <= d <= month_end)
    forecast_daily = [pooled[d] for d in forecast_dates]
    n_forecast_days = len(forecast_dates)
    n_clim_days = max(days_in_month - n_observed_days - n_forecast_days, 0)

    mean, var = monthly_total_moments(
        observed_total=observed_total,
        forecast_daily=forecast_daily,
        clim_daily_mean=clim_mean,
        clim_daily_var=clim_var,
        n_tail_days=n_clim_days,
        floor=var_floor,
    )
    return PrecipForecastSet(
        target=target,
        mean=mean,
        var=var,
        coverage=coverage,
        n_observed_days=n_observed_days,
        n_forecast_days=n_forecast_days,
        n_clim_days=n_clim_days,
    )
