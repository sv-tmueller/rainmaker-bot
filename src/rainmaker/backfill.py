"""Build forecast-vs-actual pairs from history and fit a calibration cell.

Actuals come from NOAA NCEI's token-free daily-summaries service (the GHCND
station daily max, the closest free proxy to the Weather Underground settling
value). Historical forecasts come from Open-Meteo's historical-forecast API; the
ensemble archive does not retain members for past dates, so the predictive
spread is taken from the multi-model disagreement (mean and std across the
deterministic models). That is an approximation of the live pooled distribution;
tighter calibration grows from the bot's own persisted runs over time.
"""

import calendar
import statistics
from datetime import date
from typing import Any

import httpx

from rainmaker.config import OPENMETEO_MODELS, Station
from rainmaker.forecasts.base import ForecastSample
from rainmaker.forecasts.openmeteo import _daily_field
from rainmaker.probability.calibration import (
    Accuracy,
    Calibration,
    CalibrationPair,
    compute_accuracy,
    fit_calibration,
)
from rainmaker.probability.distribution import Gaussian

NCEI_URL = "https://www.ncei.noaa.gov/access/services/data/v1"
HISTORICAL_FORECAST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
PREVIOUS_RUNS_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"


def fetch_actuals(
    ghcnd_id: str,
    start: date,
    end: date,
    client: httpx.Client,
    variable: str = "TMAX",
) -> dict[date, float]:
    """Daily extreme (degrees F) per date from NCEI daily-summaries. Raises on HTTP error.

    `variable` is the GHCND element to read: TMAX (daily high) or TMIN (daily low).
    """
    resp = client.get(
        NCEI_URL,
        params={
            "dataset": "daily-summaries",
            "stations": ghcnd_id,
            "dataTypes": variable,
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "units": "standard",
            "format": "json",
        },
    )
    resp.raise_for_status()
    rows: list[dict[str, Any]] = resp.json()
    return {
        date.fromisoformat(r["DATE"]): float(r[variable])
        for r in rows
        if r.get(variable) not in (None, "")
    }


def fetch_monthly_precip(
    ghcnd_id: str, year: int, month: int, client: httpx.Client
) -> float | None:
    """Monthly total precipitation (inches) from NCEI global-summary-of-the-month.

    Returns None when the month is not yet published, so the settle loop waits.
    Raises on HTTP error. GSOM rejects a YYYY-MM range, so the request is bounded
    by the month's first and last calendar day.
    """
    last = calendar.monthrange(year, month)[1]
    resp = client.get(
        NCEI_URL,
        params={
            "dataset": "global-summary-of-the-month",
            "stations": ghcnd_id,
            "dataTypes": "PRCP",
            "startDate": f"{year:04d}-{month:02d}-01",
            "endDate": f"{year:04d}-{month:02d}-{last:02d}",
            "units": "standard",
            "format": "json",
        },
    )
    resp.raise_for_status()
    for r in resp.json():
        if r.get("PRCP") not in (None, ""):
            return float(r["PRCP"])
    return None


def _fetch_archive_daily(
    station: Station, start: date, end: date, client: httpx.Client, field: str
) -> dict[str, Any]:
    """Raw daily multi-model archive block for the window. Raises on HTTP error."""
    resp = client.get(
        HISTORICAL_FORECAST_URL,
        params={
            "latitude": str(station.lat),
            "longitude": str(station.lon),
            "daily": field,
            "temperature_unit": "fahrenheit",
            "timezone": station.timezone,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "models": ",".join(OPENMETEO_MODELS),
        },
    )
    resp.raise_for_status()
    daily: dict[str, Any] = resp.json()["daily"]
    return daily


def fetch_historical_forecasts(
    station: Station, start: date, end: date, client: httpx.Client, variable: str = "TMAX"
) -> dict[date, Gaussian]:
    """Per-date Gaussian from the multi-model spread. Raises on HTTP error."""
    field = _daily_field(variable)
    daily = _fetch_archive_daily(station, start, end, client, field)
    model_keys = [f"{field}_{m}" for m in OPENMETEO_MODELS]
    out: dict[date, Gaussian] = {}
    for i, iso in enumerate(daily["time"]):
        values = [daily[k][i] for k in model_keys if daily.get(k) and daily[k][i] is not None]
        if len(values) < 2:
            continue  # need at least two models to estimate a spread
        out[date.fromisoformat(iso)] = Gaussian(
            mu=statistics.fmean(values), sigma=max(statistics.stdev(values), 1e-6)
        )
    return out


def fetch_historical_point_forecasts(
    station: Station,
    leads: tuple[int, ...],
    start: date,
    end: date,
    client: httpx.Client,
    variable: str = "TMAX",
) -> dict[int, dict[date, float]]:
    """Per-lead, per-date multi-model-mean daily extreme from the Previous Runs API.

    Each value is the daily max (TMAX) or min (TMIN) of the hourly temperature the
    models forecast `lead` days before the valid day, averaged across the models
    that reported it. `previous_dayN` is an hourly-only suffix, so the daily extreme
    is reduced here. Raises on HTTP error.
    """
    fields = [f"temperature_2m_previous_day{lead}" for lead in leads]
    resp = client.get(
        PREVIOUS_RUNS_URL,
        params={
            "latitude": str(station.lat),
            "longitude": str(station.lon),
            "hourly": ",".join(fields),
            "temperature_unit": "fahrenheit",
            "timezone": station.timezone,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "models": ",".join(OPENMETEO_MODELS),
        },
    )
    resp.raise_for_status()
    hourly: dict[str, Any] = resp.json()["hourly"]
    times = hourly["time"]
    reduce = max if variable == "TMAX" else min
    out: dict[int, dict[date, float]] = {}
    for lead in leads:
        per_model_daily: dict[date, list[float]] = {}
        for model in OPENMETEO_MODELS:
            values = hourly.get(f"temperature_2m_previous_day{lead}_{model}")
            if values is None:
                continue  # key absent: this model did not report at this lead
            by_day: dict[date, list[float]] = {}
            for iso, value in zip(times, values, strict=True):
                if value is None:
                    continue
                by_day.setdefault(date.fromisoformat(iso[:10]), []).append(value)
            for day, hours in by_day.items():
                per_model_daily.setdefault(day, []).append(reduce(hours))
        out[lead] = {
            day: statistics.fmean(extremes) for day, extremes in per_model_daily.items()
        }
    return out


def fetch_historical_samples(
    station: Station, start: date, end: date, client: httpx.Client
) -> dict[date, list[ForecastSample]]:
    """Per-date Open-Meteo archive samples, one per model. Raises on HTTP error.

    The P/L backtest pools these into a ForecastSet so it can reuse the live
    edge-ranking path. The archive is a single source at roughly lead 1, so every
    sample is tagged source="open-meteo" with a nominal lead of 1.
    """
    field = _daily_field("TMAX")
    daily = _fetch_archive_daily(station, start, end, client, field)
    out: dict[date, list[ForecastSample]] = {}
    for i, iso in enumerate(daily["time"]):
        target_date = date.fromisoformat(iso)
        samples: list[ForecastSample] = []
        for model in OPENMETEO_MODELS:
            values = daily.get(f"{field}_{model}")
            if not values or values[i] is None:
                continue
            samples.append(
                ForecastSample(
                    source="open-meteo",
                    model=model,
                    member=None,
                    station=station.icao,
                    variable="TMAX",
                    target_date=target_date,
                    lead_time_days=1,
                    value_f=float(values[i]),
                    issued_at=None,
                )
            )
        if samples:
            out[target_date] = samples
    return out


def build_pairs(
    forecasts: dict[date, Gaussian], actuals: dict[date, float]
) -> list[CalibrationPair]:
    """Join forecasts and actuals on date into calibration pairs."""
    return [
        CalibrationPair(mu=g.mu, sigma=g.sigma, actual=actuals[d])
        for d, g in sorted(forecasts.items())
        if d in actuals
    ]


def run_backfill(
    station: Station,
    variable: str,
    lead_time: int,
    start: date,
    end: date,
    client: httpx.Client,
) -> tuple[Calibration, Accuracy]:
    """Fetch history, build pairs, fit one calibration cell, measure accuracy."""
    forecasts = fetch_historical_forecasts(station, start, end, client, variable)
    actuals = fetch_actuals(station.ghcnd_id, start, end, client, variable)
    pairs = build_pairs(forecasts, actuals)
    return fit_calibration(station.icao, variable, lead_time, pairs), compute_accuracy(pairs)
