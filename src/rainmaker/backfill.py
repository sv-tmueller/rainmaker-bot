"""Build forecast-vs-actual pairs from history and fit a calibration cell per lead.

Actuals source is routed per venue (mirrors settle.py):
  Polymarket stations (ICAO in ICAO_TO_ASOS_STATION) -> ASOS (Iowa State Mesonet).
  Kalshi-only stations (KNYC Central Park, KMDW Midway) -> NCEI GHCND daily-summaries.

run_backfill fits every requested (station, variable, lead) cell from one source,
the Previous Runs API (fetch_historical_lead_forecasts): one request per
(station, variable) covers every lead the live run can bet, 0 through 3. As with
the historical-forecast archive, the ensemble archive does not retain members for
past dates, so the predictive spread is the multi-model disagreement (mean and
std across the deterministic models) rather than a true ensemble spread. That is
an approximation of the live pooled distribution; tighter calibration grows from
the bot's own persisted runs over time.

Lead-0 caveat: it is requested as `temperature_2m_previous_day0`, but Open-Meteo
normalizes the response to the un-suffixed `temperature_2m_<model>` key, which is
the most recent model run for each archived hour. That is slightly fresher than
what the live morning run actually sees, so the lead-0 fit is mildly optimistic.

fetch_historical_forecasts (the historical-forecast archive, lead ~1 only) stays
in place for backtest.py, which depends on its calendar-date framing; it is a
separate source from the Previous Runs path above.
"""

import calendar
import statistics
from datetime import date, timedelta
from typing import Any

import httpx

from rainmaker.config import BACKFILL_DAYS, OPENMETEO_MODELS, Station, season_start_month
from rainmaker.forecasts.asos import ICAO_TO_ASOS_STATION, fetch_asos_daily_extreme
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


def season_window(today: date, days: int = BACKFILL_DAYS) -> tuple[date, date] | None:
    """Return (start, end) for the calibration window anchored at today.

    end = today - 1 (actuals lag real-time).
    start = max(today - days, first day of today's meteorological season).

    Returns None when start > end, which happens on the first day of a new
    season (yesterday was still the prior season). Callers should skip the
    calibration fit and fall back to the uncalibrated widening path.
    """
    end = today - timedelta(days=1)
    year, month = season_start_month(today)
    season_start = date(year, month, 1)
    start = max(today - timedelta(days=days), season_start)
    if start > end:
        return None
    return start, end


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
    body = resp.json()
    if "daily" not in body:
        raise ValueError(
            f"'daily' key missing from Open-Meteo response: {body.get('reason', body)!r}"
        )
    daily: dict[str, Any] = body["daily"]
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


def fetch_historical_lead_forecasts(
    station: Station,
    leads: tuple[int, ...],
    start: date,
    end: date,
    client: httpx.Client,
    variable: str = "TMAX",
) -> dict[int, dict[date, Gaussian]]:
    """Per-lead, per-date Gaussian from the Previous Runs API's multi-model spread.

    One request covers every requested lead. Each Gaussian is built the same way
    fetch_historical_forecasts builds one: the daily max (TMAX) or min (TMIN) of the
    hourly temperature the models forecast `lead` days before the valid day, mu =
    mean across models, sigma = stdev across models (at least two models required
    per date; dates with fewer are dropped). `previous_dayN` is an hourly-only
    suffix, so the daily extreme is reduced here.

    Lead 0 is requested as `temperature_2m_previous_day0`, but Open-Meteo
    normalizes the response key to the un-suffixed `temperature_2m_<model>`
    (distinct from `previous_day1`). Caveat: the day-0 archive value is the most
    recent model run per hour, slightly fresher than what the live morning run
    sees, so the lead-0 fit is mildly optimistic. Raises on HTTP error.
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
    body = resp.json()
    if "hourly" not in body:
        raise ValueError(
            f"'hourly' key missing from Open-Meteo response: {body.get('reason', body)!r}"
        )
    hourly: dict[str, Any] = body["hourly"]
    times = hourly["time"]
    reduce = max if variable == "TMAX" else min
    out: dict[int, dict[date, Gaussian]] = {}
    for lead in leads:
        suffix = "" if lead == 0 else f"_previous_day{lead}"
        per_model_daily: dict[date, list[float]] = {}
        for model in OPENMETEO_MODELS:
            values = hourly.get(f"temperature_2m{suffix}_{model}")
            if values is None:
                continue  # key absent: this model did not report at this lead
            by_day: dict[date, list[float]] = {}
            for iso, value in zip(times, values, strict=True):
                if value is None:
                    continue
                by_day.setdefault(date.fromisoformat(iso[:10]), []).append(value)
            for day, hours in by_day.items():
                per_model_daily.setdefault(day, []).append(reduce(hours))
        gaussians: dict[date, Gaussian] = {}
        for day, extremes in per_model_daily.items():
            if len(extremes) < 2:
                continue  # need at least two models to estimate a spread
            gaussians[day] = Gaussian(
                mu=statistics.fmean(extremes), sigma=max(statistics.stdev(extremes), 1e-6)
            )
        out[lead] = gaussians
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


def venue_actuals(
    station: Station,
    start: date,
    end: date,
    client: httpx.Client,
    variable: str = "TMAX",
) -> dict[date, float]:
    """Daily extreme (degrees F) for the window, routed by venue.

    Polymarket stations (ICAO in ICAO_TO_ASOS_STATION) -> ASOS (Iowa State Mesonet),
    batched over the full window (one request). Mirrors settle.py's ASOS path.
    Kalshi-only stations (KNYC, KMDW) -> NCEI GHCND daily-summaries (unchanged).
    Shared by calibration backfill and backtest grading, so both fit and grade
    against the same source that actually settles the market.
    """
    asos_code = ICAO_TO_ASOS_STATION.get(station.icao)
    # Guard the US ASOS path (Fahrenheit, UTC bucketing) to F-unit stations. Intl
    # (Celsius) stations share ICAO_TO_ASOS_STATION for settlement but are
    # uncalibrated by design and have no calibration-actuals path, so refuse them
    # loudly rather than return silently-wrong Fahrenheit. Unreachable today
    # (_distinct_stations never yields INTL_STATIONS); defensive against a future caller.
    if asos_code is not None and station.unit == "F":
        return fetch_asos_daily_extreme(asos_code, start, end, client, variable)
    if station.ghcnd_id is None:
        raise ValueError(f"no calibration-actuals path for intl station {station.icao}")
    return fetch_actuals(station.ghcnd_id, start, end, client, variable)


def build_pairs(
    forecasts: dict[date, Gaussian], actuals: dict[date, float]
) -> list[CalibrationPair]:
    """Join forecasts and actuals on date into calibration pairs."""
    return [
        CalibrationPair(mu=g.mu, sigma=g.sigma, ensemble_var=g.sigma**2, actual=actuals[d])
        for d, g in sorted(forecasts.items())
        if d in actuals
    ]


def run_backfill(
    station: Station,
    variable: str,
    leads: tuple[int, ...],
    start: date,
    end: date,
    client: httpx.Client,
) -> dict[int, tuple[Calibration, Accuracy]]:
    """Fetch history, build pairs, fit one calibration cell per lead, measure accuracy.

    One Previous Runs request covers every requested lead. Leads with no
    overlapping actual are omitted rather than erroring (not every lead has
    enough season-window history to fit).
    """
    by_lead = fetch_historical_lead_forecasts(station, leads, start, end, client, variable)
    actuals = venue_actuals(station, start, end, client, variable)
    out: dict[int, tuple[Calibration, Accuracy]] = {}
    for lead in leads:
        pairs = build_pairs(by_lead[lead], actuals)
        if pairs:
            out[lead] = (
                fit_calibration(station.icao, variable, lead, pairs),
                compute_accuracy(pairs),
            )
    return out
