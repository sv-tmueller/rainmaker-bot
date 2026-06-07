"""Parse Kalshi daily high-temp markets into the shared Market/Bucket types.

Kalshi exposes one binary strike per market (strike_type greater/less/between
with floor_strike/cap_strike), grouped under an event ladder. We map each strike
onto the existing Bucket (above/below/range) so the Polymarket evaluate/record
path is reused unchanged. A parallel of polymarket/markets.py for a different
wire format, sharing the Market/Bucket types.
"""

import re
from datetime import date
from typing import Any

from rainmaker.config import Station, Target
from rainmaker.polymarket.markets import Bucket, Market

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
    raw = market.get(key)
    if raw in (None, ""):
        return None
    val = float(raw)
    return val if val > 0 else None


def parse_kalshi_bucket(market: dict[str, Any]) -> Bucket:
    strike_type = market["strike_type"]
    floor = market.get("floor_strike")
    cap = market.get("cap_strike")
    if strike_type == "greater":
        kind, lo, hi, threshold = "above", None, None, int(floor)
    elif strike_type == "less":
        kind, lo, hi, threshold = "below", None, None, int(cap)
    elif strike_type == "between":
        kind, lo, hi, threshold = "range", int(floor), int(cap), None
    else:
        raise ValueError(f"unknown Kalshi strike_type: {strike_type!r}")
    best_ask = _price(market, "yes_ask_dollars")
    best_bid = _price(market, "yes_bid_dollars")
    last = _price(market, "last_price_dollars")
    mid = None if best_ask is None or best_bid is None else (best_ask + best_bid) / 2
    yes_price = last if last is not None else (mid if mid is not None else 0.0)
    return Bucket(
        label=market.get("subtitle") or market["ticker"],
        kind=kind,
        lo=lo,
        hi=hi,
        threshold=threshold,
        yes_token_id=market["ticker"],
        best_ask=best_ask,
        best_bid=best_bid,
        yes_price=yes_price,
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
    city: str, station: Station, event_markets: list[dict[str, Any]]
) -> Market:
    """Build a Market from the strikes of one Kalshi high-temp event ladder.

    Guards that the rule text names the expected settlement station, mirroring the
    Polymarket parser's ICAO guard. Raises ValueError on any inconsistency so one
    bad event is skipped upstream rather than silently mispriced.
    """
    if not event_markets:
        raise ValueError(f"empty Kalshi event for {city}")
    event_ticker = event_markets[0]["event_ticker"]
    rules = event_markets[0].get("rules_primary", "")
    if station.name not in rules:
        raise ValueError(
            f"resolution station {station.name!r} not named in event {event_ticker} rules"
        )
    local_date = _settlement_date(event_ticker)
    target = Target(station=station, variable="TMAX", local_date=local_date)
    buckets = [parse_kalshi_bucket(m) for m in event_markets]
    return Market(
        id=event_ticker,
        slug=event_ticker,
        title=f"Kalshi: highest temperature in {city} on {local_date.isoformat()}",
        target=target,
        buckets=buckets,
    )
