"""Parse monthly US precipitation events from Gamma into a PrecipMonthlyMarket.

A parallel path to markets.py: the temperature parser reads whole-degree
buckets keyed on an ICAO station, this reads inch brackets keyed on a NOAA
monthly station. The shared types (PrecipBracket, PrecipMonthlyMarket, etc.)
live in rainmaker.domain; this module owns only the Polymarket-specific parser.
"""

import calendar
import json
import re
from datetime import date, datetime
from typing import Any

from rainmaker.config import PRECIP_STATIONS
from rainmaker.domain import (
    PrecipBracket,
    PrecipMonthlyMarket,
    PrecipTarget,
    parse_precip_bracket_label,
)

ROUND_BETWEEN_BRACKETS_UP = True  # a value exactly between two brackets resolves up

_MONTHS = {name: i for i, name in enumerate(calendar.month_name) if name}

_TITLE_RE = re.compile(r"precipitation in (.+?) in (\w+)\??$", re.IGNORECASE)


def parse_precip_bracket(market: dict[str, Any]) -> PrecipBracket:
    label = market["groupItemTitle"]
    kind, lo, hi, threshold = parse_precip_bracket_label(label)
    token_ids = json.loads(market["clobTokenIds"])
    yes_price = float(json.loads(market["outcomePrices"])[0])
    best_ask = market.get("bestAsk")
    best_bid = market.get("bestBid")
    return PrecipBracket(
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


def parse_precip_city(title: str) -> str:
    match = _TITLE_RE.match(title)
    if match is None:
        raise ValueError(f"not a monthly precip market title: {title!r}")
    return match.group(1).strip()


def parse_precip_event(event: dict[str, Any]) -> PrecipMonthlyMarket:
    title = event["title"]
    match = _TITLE_RE.match(title)
    if match is None:
        raise ValueError(f"not a monthly precip market title: {title!r}")
    city = match.group(1).strip()
    month = _MONTHS.get(match.group(2).capitalize())
    if month is None:
        raise ValueError(f"unrecognized month in precip market title: {title!r}")
    station = PRECIP_STATIONS[city]  # KeyError for an unknown city is intended
    if station.resolution_name not in event["description"]:
        raise ValueError(
            f"resolution station {station.resolution_name!r} not named in "
            f"market {event['id']} rules"
        )
    end = datetime.fromisoformat(event["endDate"])
    # The endDate lands inside the resolution month (June 2026 markets carry
    # 2026-06-30). Fail loud if that ever disagrees with the title month rather
    # than silently settling the wrong period.
    if end.month != month:
        raise ValueError(
            f"market {event['id']} endDate {event['endDate']} month {end.month} "
            f"disagrees with title month {month}"
        )
    last_day = calendar.monthrange(end.year, month)[1]
    target = PrecipTarget(
        station=station,
        variable="PRCP",
        year=end.year,
        month=month,
        settlement_date=date(end.year, month, last_day),
    )
    buckets = [parse_precip_bracket(m) for m in event["markets"]]
    return PrecipMonthlyMarket(
        id=event["id"], slug=event["slug"], title=title, target=target, buckets=buckets
    )
