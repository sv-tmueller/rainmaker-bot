"""Parse Kalshi daily temperature markets into the shared Market/Bucket types.

Kalshi exposes one binary strike per market (strike_type greater/less/between
with floor_strike/cap_strike), grouped under an event ladder. We map each strike
onto the existing Bucket (above/below/range) so the Polymarket evaluate/record
path is reused unchanged. A parallel of polymarket/markets.py for a different
wire format, sharing the Market/Bucket types.
"""

import re
from datetime import date
from typing import Any

from rainmaker.config import Station, Target, Variable
from rainmaker.polymarket.markets import Bucket, BucketKind, Market

# The phrase each daily-temperature rule uses for its quantity. High-temp rules
# also name the exact station ("Central Park, New York"); low-temp rules name only
# the city, so TMIN guards on the shared NWS resolution source instead.
_VAR_PHRASE: dict[Variable, str] = {"TMAX": "highest temperature", "TMIN": "minimum temperature"}

_MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}
# A Kalshi high-temp event ticker is KXHIGH<CODE>-<YY><MON><DD> (e.g. -26JUN08).
_TICKER_DATE_RE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})(?:-|$)")


def _price(market: dict[str, Any], key: str) -> float | None:
    raw: Any = market.get(key)
    if raw is None or raw == "":
        return None
    val = float(raw)
    return val if val > 0 else None


def _yes_price(market: dict[str, Any]) -> float:
    """The YES implied price: last trade, else the bid/ask mid, else 0."""
    best_ask = _price(market, "yes_ask_dollars")
    best_bid = _price(market, "yes_bid_dollars")
    last = _price(market, "last_price_dollars")
    mid = None if best_ask is None or best_bid is None else (best_ask + best_bid) / 2
    return last if last is not None else (mid if mid is not None else 0.0)


def parse_kalshi_bucket(market: dict[str, Any]) -> Bucket:
    strike_type = market["strike_type"]
    floor: Any = market.get("floor_strike")
    cap: Any = market.get("cap_strike")
    kind: BucketKind
    lo: int | None = None
    hi: int | None = None
    threshold: int | None = None
    if strike_type == "greater":
        if floor is None:
            raise ValueError(f"floor_strike is None for 'greater' market {market.get('ticker')!r}")
        kind, threshold = "above", int(floor)
    elif strike_type == "less":
        if cap is None:
            raise ValueError(f"cap_strike is None for 'less' market {market.get('ticker')!r}")
        kind, threshold = "below", int(cap)
    elif strike_type == "between":
        if floor is None:
            raise ValueError(f"floor_strike is None for 'between' market {market.get('ticker')!r}")
        if cap is None:
            raise ValueError(f"cap_strike is None for 'between' market {market.get('ticker')!r}")
        kind, lo, hi = "range", int(floor), int(cap)
    else:
        raise ValueError(f"unknown Kalshi strike_type: {strike_type!r}")
    return Bucket(
        label=market.get("subtitle") or market["ticker"],
        kind=kind,
        lo=lo,
        hi=hi,
        threshold=threshold,
        yes_token_id=market["ticker"],
        best_ask=_price(market, "yes_ask_dollars"),
        best_bid=_price(market, "yes_bid_dollars"),
        yes_price=_yes_price(market),
        no_token_id="",
        no_ask=_price(market, "no_ask_dollars"),
    )


def _settlement_date(event_ticker: str) -> date:
    match = _TICKER_DATE_RE.search(event_ticker)
    if match is None:
        raise ValueError(f"no date token in Kalshi event ticker: {event_ticker!r}")
    yy, mon, dd = match.group(1), match.group(2), match.group(3)
    month = _MONTHS.get(mon)
    if month is None:
        raise ValueError(f"unrecognized month in event ticker: {event_ticker!r}")
    return date(2000 + int(yy), month, int(dd))


def parse_kalshi_event(
    city: str, station: Station, event_markets: list[dict[str, Any]], *, variable: Variable = "TMAX"
) -> Market:
    """Build a Market from the strikes of one Kalshi daily-temperature event ladder.

    Guards that the rule text matches the expected quantity, and for high temp that
    it names the settlement station (catching the Central Park/Midway trap). Low-temp
    rules name only the city, so TMIN relies on the confirmed series->station map and
    guards on the shared NWS resolution source. Raises ValueError on any inconsistency
    so one bad event is skipped upstream rather than silently mispriced.
    """
    if not event_markets:
        raise ValueError(f"empty Kalshi event for {city}")
    event_ticker = event_markets[0]["event_ticker"]
    rules = event_markets[0].get("rules_primary", "")
    if _VAR_PHRASE[variable] not in rules.lower():
        raise ValueError(f"event {event_ticker} rules are not a {variable} market")
    if variable == "TMAX":
        if station.name not in rules:
            raise ValueError(
                f"resolution station {station.name!r} not named in event {event_ticker} rules"
            )
    elif "Climatological Report" not in rules:
        raise ValueError(f"event {event_ticker} rules name no NWS Climatological Report source")
    local_date = _settlement_date(event_ticker)
    target = Target(station=station, variable=variable, local_date=local_date)
    buckets = [parse_kalshi_bucket(m) for m in event_markets]
    descriptor = "highest" if variable == "TMAX" else "lowest"
    return Market(
        id=event_ticker,
        slug=event_ticker,
        title=f"Kalshi: {descriptor} temperature in {city} on {local_date.isoformat()}",
        target=target,
        buckets=buckets,
        venue="kalshi",
    )
