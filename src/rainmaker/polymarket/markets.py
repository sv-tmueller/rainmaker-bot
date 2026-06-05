import json
import re
from datetime import UTC, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

from rainmaker.config import STATIONS, Target, Variable, build_target

BucketKind = Literal["below", "range", "above"]

_RANGE_RE = re.compile(r"(-?\d+)\s*-\s*(-?\d+)")
_THRESHOLD_RE = re.compile(r"(-?\d+)")


def parse_bucket_label(label: str) -> tuple[BucketKind, int | None, int | None, int | None]:
    """Parse a Polymarket bucket title into (kind, lo, hi, threshold).

    "59°F or below" -> ("below", None, None, 59)
    "70-71°F"       -> ("range", 70, 71, None)
    "78°F or higher" -> ("above", None, None, 78)
    """
    lowered = label.lower()
    if "below" in lowered:
        match = _THRESHOLD_RE.search(label)
        if match is None:
            raise ValueError(f"no threshold in below-bucket label: {label!r}")
        return ("below", None, None, int(match.group(1)))
    if "higher" in lowered or "above" in lowered:
        match = _THRESHOLD_RE.search(label)
        if match is None:
            raise ValueError(f"no threshold in above-bucket label: {label!r}")
        return ("above", None, None, int(match.group(1)))
    match = _RANGE_RE.search(label)
    if match is None:
        raise ValueError(f"unrecognized bucket label: {label!r}")
    lo, hi = int(match.group(1)), int(match.group(2))
    if lo > hi:
        raise ValueError(f"inverted range bucket label: {label!r}")
    return ("range", lo, hi, None)


class Bucket(BaseModel):
    model_config = ConfigDict(frozen=True)

    label: str
    kind: BucketKind
    lo: int | None
    hi: int | None
    threshold: int | None
    yes_token_id: str
    best_ask: float | None
    best_bid: float | None
    yes_price: float
    # NO side. Gamma exposes only the YES book, so the NO ask is the complement of
    # the YES bid (buying NO == selling YES). None when there is no YES bid to take.
    no_token_id: str = ""
    no_ask: float | None = None


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


_TITLE_RE = re.compile(r"(highest|lowest) temperature in (.+?) on .+", re.IGNORECASE)


class Market(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    slug: str
    title: str
    target: Target
    buckets: list[Bucket]


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
    station = STATIONS[city]  # KeyError for an unknown city is intended
    if station.icao not in event["description"]:
        raise ValueError(
            f"resolution station {station.icao} not named in market {event['id']} rules"
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
    target = build_target(city, variable, local_date)
    buckets = [parse_bucket(m) for m in event["markets"]]
    return Market(id=event["id"], slug=event["slug"], title=title, target=target, buckets=buckets)
