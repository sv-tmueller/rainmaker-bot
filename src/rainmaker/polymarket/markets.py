import json
import re
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from rainmaker.config import INTL_STATIONS, STATIONS, Station, Target, Variable
from rainmaker.domain import Bucket, Market, parse_bucket_label

_ALL_TEMP_STATIONS = {**STATIONS, **INTL_STATIONS}

_TITLE_RE = re.compile(r"(highest|lowest) temperature in (.+?) on .+", re.IGNORECASE)


def _names_resolution_station(description: str, station: Station) -> bool:
    """Confirm the market rules resolve on ``station`` before trusting it.

    Polymarket switched description formats around 2026-08-23: the old text
    embedded the uppercase ICAO in parentheses (e.g. "(KLGA)"), while the new
    text drops the parens and names the station (e.g. "Dallas Love Field
    Station") with a lowercase ICAO tucked into a weather.gov URL
    (e.g. "...?site=kdal"). Match either way, case-insensitively, so both
    formats satisfy the guard while an unrelated station still fails.
    """
    lowered = description.lower()
    return station.icao.lower() in lowered or station.name.lower() in lowered


def parse_bucket(market: dict[str, Any]) -> Bucket:
    label = market["groupItemTitle"]
    kind, lo, hi, threshold = parse_bucket_label(label)
    token_ids = json.loads(market["clobTokenIds"])
    yes_price = float(json.loads(market["outcomePrices"])[0])
    best_ask = market.get("bestAsk")
    best_bid = market.get("bestBid")
    return Bucket(
        label=label,
        kind=kind,
        lo=lo,
        hi=hi,
        threshold=threshold,
        yes_token_id=token_ids[0],
        best_ask=best_ask,
        best_bid=best_bid,
        yes_price=yes_price,
        no_token_id=token_ids[1],
        no_ask=None if best_bid is None else 1 - best_bid,
    )


def parse_variable(title: str) -> Variable:
    match = _TITLE_RE.match(title)
    if match is None:
        raise ValueError(f"not a temperature market title: {title!r}")
    return "TMAX" if match.group(1).lower() == "highest" else "TMIN"


def parse_city(title: str) -> str:
    match = _TITLE_RE.match(title)
    if match is None:
        raise ValueError(f"not a temperature market title: {title!r}")
    return match.group(2).strip()


def parse_market(event: dict[str, Any]) -> Market:
    title = event["title"]
    match = _TITLE_RE.match(title)
    if match is None:
        raise ValueError(f"not a temperature market title: {title!r}")
    variable: Variable = "TMAX" if match.group(1).lower() == "highest" else "TMIN"
    city = match.group(2).strip()
    station = _ALL_TEMP_STATIONS[city]  # KeyError for an unknown city is intended
    if not _names_resolution_station(event["description"], station):
        raise ValueError(
            f"resolution station {station.icao}/{station.name} not named in "
            f"market {event['id']} rules"
        )
    end = datetime.fromisoformat(event["endDate"])
    # These daily markets publish endDate at ~12:00 UTC, which is mid-morning in
    # every US timezone, so the local settlement date is unambiguous. Fail loud if
    # that convention ever changes rather than silently resolving the wrong day.
    if not 6 <= end.astimezone(UTC).hour <= 18:
        raise ValueError(
            f"market {event['id']} endDate {event['endDate']} is not midday UTC; "
            "cannot safely derive the settlement date"
        )
    local_date = end.astimezone(ZoneInfo(station.timezone)).date()
    target = Target(station=station, variable=variable, local_date=local_date)
    buckets = [parse_bucket(m) for m in event["markets"]]
    return Market(id=event["id"], slug=event["slug"], title=title, target=target, buckets=buckets)
