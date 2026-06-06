"""Parse monthly US precipitation events from Gamma into a PrecipMonthlyMarket.

A parallel path to markets.py: the temperature parser reads whole-degree
buckets keyed on an ICAO station, this reads inch brackets keyed on a NOAA
monthly station. The types mirror Market/Bucket field-for-field (with float
bracket bounds) so the recorder can duck-type over them later, but the two
paths share no parsing code beyond the BucketKind literal.
"""

import calendar
import json
import re
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from rainmaker.config import PRECIP_STATIONS, PrecipStation, Variable
from rainmaker.polymarket.markets import BucketKind

# Monthly precip resolution rule, confirmed verbatim from both Gamma market
# descriptions. The deferred settlement task reads these; settling logic is not
# part of this build.
SETTLEMENT_DECIMALS = 2  # the NOAA monthly figure is reported to 2 decimals
ROUND_BETWEEN_BRACKETS_UP = True  # a value exactly between two brackets resolves up

_RANGE_RE = re.compile(r"([\d.]+)\s*-\s*([\d.]+)")
_NUM_RE = re.compile(r"([\d.]+)")

_MONTHS = {name: i for i, name in enumerate(calendar.month_name) if name}


def parse_precip_bracket_label(
    label: str,
) -> tuple[BucketKind, float | None, float | None, float | None]:
    """Parse a precip bracket title into (kind, lo, hi, threshold) with inch bounds.

    '<2"'    -> ("below", None, None, 2.0)   open-low tail
    '2-3"'   -> ("range", 2.0, 3.0, None)    interior range
    '>6"'    -> ("above", None, None, 6.0)   open-high tail
    """
    s = label.strip()
    if s.startswith("<"):
        match = _NUM_RE.search(s)
        if match is None:
            raise ValueError(f"no threshold in open-low precip label: {label!r}")
        return ("below", None, None, float(match.group(1)))
    if s.startswith(">"):
        match = _NUM_RE.search(s)
        if match is None:
            raise ValueError(f"no threshold in open-high precip label: {label!r}")
        return ("above", None, None, float(match.group(1)))
    match = _RANGE_RE.search(s)
    if match is None:
        raise ValueError(f"unrecognized precip bracket label: {label!r}")
    lo, hi = float(match.group(1)), float(match.group(2))
    if lo >= hi:
        raise ValueError(f"inverted range precip bracket label: {label!r}")
    return ("range", lo, hi, None)


class PrecipBracket(BaseModel):
    model_config = ConfigDict(frozen=True)

    label: str
    kind: BucketKind
    lo: float | None
    hi: float | None
    threshold: float | None
    yes_token_id: str
    best_ask: float | None
    best_bid: float | None
    yes_price: float
    # NO side, derived exactly as in markets.py: Gamma exposes only the YES book,
    # so the NO ask is the complement of the YES bid (buying NO == selling YES).
    no_token_id: str = ""
    no_ask: float | None = None


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


class PrecipTarget(BaseModel):
    model_config = ConfigDict(frozen=True)

    station: PrecipStation
    variable: Variable
    year: int
    month: int
    settlement_date: date  # last calendar day of the month


class PrecipMonthlyMarket(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    slug: str
    title: str
    target: PrecipTarget
    buckets: list[PrecipBracket]


_TITLE_RE = re.compile(r"precipitation in (.+?) in (\w+)\??$", re.IGNORECASE)


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
