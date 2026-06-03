"""Build forecast-vs-actual pairs from history and fit a calibration cell.

Actuals come from NOAA NCEI's token-free daily-summaries service (the GHCND
station daily max, the closest free proxy to the Weather Underground settling
value). Historical forecasts come from Open-Meteo's historical-forecast API; the
ensemble archive does not retain members for past dates, so the predictive
spread is taken from the multi-model disagreement (mean and std across the
deterministic models). That is an approximation of the live pooled distribution;
tighter calibration grows from the bot's own persisted runs over time.
"""

import statistics
from datetime import date
from typing import Any

import httpx

from rainmaker.config import OPENMETEO_MODELS, Station
from rainmaker.probability.calibration import Calibration, CalibrationPair, fit_calibration
from rainmaker.probability.distribution import Gaussian

NCEI_URL = "https://www.ncei.noaa.gov/access/services/data/v1"
HISTORICAL_FORECAST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"


def fetch_actuals(ghcnd_id: str, start: date, end: date, client: httpx.Client) -> dict[date, float]:
    """Daily max (degrees F) per date from NCEI daily-summaries. Raises on HTTP error."""
    resp = client.get(
        NCEI_URL,
        params={
            "dataset": "daily-summaries",
            "stations": ghcnd_id,
            "dataTypes": "TMAX",
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "units": "standard",
            "format": "json",
        },
    )
    resp.raise_for_status()
    rows: list[dict[str, Any]] = resp.json()
    return {date.fromisoformat(r["DATE"]): float(r["TMAX"]) for r in rows if r.get("TMAX") != ""}


def fetch_historical_forecasts(
    station: Station, start: date, end: date, client: httpx.Client
) -> dict[date, Gaussian]:
    """Per-date Gaussian from the multi-model spread. Raises on HTTP error."""
    resp = client.get(
        HISTORICAL_FORECAST_URL,
        params={
            "latitude": str(station.lat),
            "longitude": str(station.lon),
            "daily": "temperature_2m_max",
            "temperature_unit": "fahrenheit",
            "timezone": station.timezone,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "models": ",".join(OPENMETEO_MODELS),
        },
    )
    resp.raise_for_status()
    daily = resp.json()["daily"]
    model_keys = [f"temperature_2m_max_{m}" for m in OPENMETEO_MODELS]
    out: dict[date, Gaussian] = {}
    for i, iso in enumerate(daily["time"]):
        values = [daily[k][i] for k in model_keys if daily.get(k) and daily[k][i] is not None]
        if len(values) < 2:
            continue  # need at least two models to estimate a spread
        out[date.fromisoformat(iso)] = Gaussian(
            mu=statistics.fmean(values), sigma=max(statistics.stdev(values), 1e-6)
        )
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
) -> Calibration:
    """Fetch history, build pairs, and fit one calibration cell."""
    forecasts = fetch_historical_forecasts(station, start, end, client)
    actuals = fetch_actuals(station.ghcnd_id, start, end, client)
    pairs = build_pairs(forecasts, actuals)
    return fit_calibration(station.icao, variable, lead_time, pairs)
