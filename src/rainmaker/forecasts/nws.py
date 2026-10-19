from datetime import date, datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

import httpx

from rainmaker.config import Target
from rainmaker.forecasts.base import ForecastSample

NWS_BASE = "https://api.weather.gov"


def _make_sample(
    target: Target,
    issued_at: datetime,
    issued_local_date: date,
    value_f: float,
) -> ForecastSample:
    return ForecastSample(
        source="nws",
        model="nws",
        member=None,
        station=target.station.icao,
        variable=target.variable,
        target_date=target.local_date,
        lead_time_days=(target.local_date - issued_local_date).days,
        value_f=value_f,
        issued_at=issued_at,
    )


def _select_period(
    periods: list[dict[str, Any]],
    *,
    want_daytime: bool,
    match_date: date,
    target_date: date,
    tz: ZoneInfo,
) -> dict[str, Any] | None:
    """Period matcher for the periods product (#450, implements #434).

    Matches on the period COVERING the wanted window, not the first one that
    merely begins on the match date. Preference order:

    1. A period of the wanted kind whose END date is the target date - it
       covers the window the target settles on. Among several, one starting
       in the afternoon/evening (start hour >= 12) is the full period rather
       than a post-midnight residual fragment.
    2. Fallback: the legacy first-hit on start date == match_date.

    Why not first-hit alone: for TMIN any night period BEGINNING on the
    match date qualifies, so a post-midnight "Overnight" fragment listed
    first shadows the wanted "Tonight" period (wrong night's low), and a
    fragment REPLACING the evening period matches nothing at all (silent
    zero-sample loss with ok=True). Measured blast radius: zero observed
    events in stored history
    (docs/architecture/tmin-night-fragment-blast-radius.md); this is
    defense-in-depth for a legal NWS product shape.
    """
    covering: list[dict[str, Any]] = []
    first_hit: dict[str, Any] | None = None
    for period in periods:
        if period["isDaytime"] != want_daytime:
            continue
        start_local = datetime.fromisoformat(period["startTime"]).astimezone(tz)
        end_local = datetime.fromisoformat(period["endTime"]).astimezone(tz)
        if end_local.date() == target_date:
            covering.append(period)
        if first_hit is None and start_local.date() == match_date:
            first_hit = period
    for period in covering:
        start_local = datetime.fromisoformat(period["startTime"]).astimezone(tz)
        if start_local.hour >= 12:
            return period
    if covering:
        return covering[0]
    return first_hit


def parse(forecast_json: dict[str, Any], target: Target) -> list[ForecastSample]:
    if target.variable not in ("TMAX", "TMIN"):
        raise NotImplementedError(f"unsupported variable {target.variable!r}")
    props = forecast_json["properties"]
    issued_at = datetime.fromisoformat(props["updateTime"])
    tz = ZoneInfo(target.station.timezone)
    issued_local = issued_at.astimezone(tz).date()

    # TMAX is the daytime high on the target day, taken from the periods
    # product. TMIN is the CALENDAR-DAY low Polymarket settles on, taken from
    # the hourly product restricted to the target local day (#426, per the
    # #404 alternatives analysis). The former night-period sampling anchored
    # TMIN on a colder quantity: evening cold plunges before midnight belong to
    # the previous calendar day but sat inside the NWS night period, wedging
    # the forecast low against settlement (measured wrh proxy, 6 stations x 14
    # days: mean 1.0-1.7F, peaks to 7F, night-period lows systematically
    # LOWER; see the #404 appendix in
    # docs/architecture/noaa-wrh-vs-asos-comparison.md). The backfill TMIN
    # pairs always used the local calendar-day min, so this retarget also
    # aligns the live NWS source with what the calibration cells were fitted
    # against.
    if target.variable == "TMIN":
        return parse_hourly_low(forecast_json, target)

    match_date = target.local_date
    # TMAX equivalence: for daytime periods the covering-window preference
    # coincides with first-hit on every realistic NWS shape (a daytime period
    # ending on the target date starts on it), so TMAX selection is unchanged.
    period = _select_period(
        props["periods"],
        want_daytime=True,
        match_date=match_date,
        target_date=target.local_date,
        tz=tz,
    )
    if period is None:
        return []
    if period["temperatureUnit"] != "F":
        raise ValueError(f"expected Fahrenheit, got {period['temperatureUnit']}")
    return [_make_sample(target, issued_at, issued_local, float(period["temperature"]))]


def parse_hourly_low(forecast_json: dict[str, Any], target: Target) -> list[ForecastSample]:
    """Calendar-day low from the hourly product, restricted to the target local day.

    A sample is emitted ONLY when the target day is fully covered: all 24 local
    hours present, first 00:00 and last 23:00. Lead-0 targets fetched in the
    afternoon routinely get a product covering only the remainder of the day;
    the already-observed morning min is absent, so the min over the surviving
    hours is biased HIGH and fabricates edge on higher buckets. Partial
    coverage yields no sample; the pool proceeds on the other sources.
    """
    props = forecast_json["properties"]
    issued_at = datetime.fromisoformat(props["updateTime"])
    tz = ZoneInfo(target.station.timezone)
    issued_local = issued_at.astimezone(tz).date()
    temps_by_hour: dict[int, float] = {}
    for period in props["periods"]:
        start_local = datetime.fromisoformat(period["startTime"]).astimezone(tz)
        if start_local.date() != target.local_date:
            continue
        if period["temperatureUnit"] != "F":
            raise ValueError(f"expected Fahrenheit, got {period['temperatureUnit']}")
        temps_by_hour[start_local.hour] = float(period["temperature"])
    if len(temps_by_hour) != 24 or min(temps_by_hour) != 0 or max(temps_by_hour) != 23:
        return []
    return [_make_sample(target, issued_at, issued_local, min(temps_by_hour.values()))]


def fetch_raw(target: Target, client: httpx.Client) -> dict[str, Any]:
    points = client.get(f"{NWS_BASE}/points/{target.station.lat},{target.station.lon}")
    points.raise_for_status()
    point_props = points.json()["properties"]
    if target.variable == "TMIN":
        forecast_url = point_props["forecastHourly"]
    else:
        forecast_url = point_props["forecast"]
    forecast = client.get(forecast_url)
    forecast.raise_for_status()
    return cast(dict[str, Any], forecast.json())


class NwsSource:
    name = "nws"

    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def fetch(self, target: Target) -> list[ForecastSample]:
        return parse(fetch_raw(target, self.client), target)
