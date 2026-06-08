"""Parse Kalshi monthly rain markets into the shared PrecipMonthlyMarket type.

Kalshi exposes a strike ladder on total monthly precipitation (inches), settled on
the per-city NWS Climatological Report. Mirrors kalshi/markets.py but builds the
precip types so the existing precip forecast/evaluate/record path is reused. The
event ticker is monthly (KXRAINNYCM-26JUN), so the date token has no day.
"""

import calendar
import re
from datetime import date
from typing import Any

from rainmaker.config import PrecipStation
from rainmaker.kalshi.markets import _MONTHS, _price, _yes_price
from rainmaker.polymarket.markets import BucketKind
from rainmaker.polymarket.precip_markets import PrecipBracket, PrecipMonthlyMarket, PrecipTarget

_MONTH_TOKEN_RE = re.compile(r"-(\d{2})([A-Z]{3})(?:-|$)")


def parse_kalshi_precip_bracket(market: dict[str, Any]) -> PrecipBracket:
    strike_type = market["strike_type"]
    floor: Any = market.get("floor_strike")
    cap: Any = market.get("cap_strike")
    kind: BucketKind
    lo: float | None = None
    hi: float | None = None
    threshold: float | None = None
    if strike_type == "greater":
        kind, threshold = "above", float(floor)
    elif strike_type == "less":
        kind, threshold = "below", float(cap)
    elif strike_type == "between":
        kind, lo, hi = "range", float(floor), float(cap)
    else:
        raise ValueError(f"unknown Kalshi strike_type: {strike_type!r}")
    return PrecipBracket(
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


def _month_token(event_ticker: str) -> tuple[int, int]:
    match = _MONTH_TOKEN_RE.search(event_ticker)
    if match is None:
        raise ValueError(f"no month token in Kalshi event ticker: {event_ticker!r}")
    month = _MONTHS.get(match.group(2))
    if month is None:
        raise ValueError(f"unrecognized month in event ticker: {event_ticker!r}")
    return 2000 + int(match.group(1)), month


def parse_kalshi_precip_event(
    city: str, station: PrecipStation, event_markets: list[dict[str, Any]]
) -> PrecipMonthlyMarket:
    """Build a PrecipMonthlyMarket from the strikes of one Kalshi rain event ladder.

    Guards that the rule text is a precipitation market and names the expected
    station, mirroring the Polymarket precip parser. Raises ValueError on any
    inconsistency so one bad event is skipped upstream rather than mispriced.
    """
    if not event_markets:
        raise ValueError(f"empty Kalshi precip event for {city}")
    event_ticker = event_markets[0]["event_ticker"]
    rules = event_markets[0].get("rules_primary", "")
    if "precipitation" not in rules.lower():
        raise ValueError(f"event {event_ticker} rules are not a precipitation market")
    if station.resolution_name not in rules:
        raise ValueError(
            f"station {station.resolution_name!r} not named in Kalshi event {event_ticker} rules"
        )
    year, month = _month_token(event_ticker)
    last_day = calendar.monthrange(year, month)[1]
    target = PrecipTarget(
        station=station,
        variable="PRCP",
        year=year,
        month=month,
        settlement_date=date(year, month, last_day),
    )
    buckets = [parse_kalshi_precip_bracket(m) for m in event_markets]
    return PrecipMonthlyMarket(
        id=event_ticker,
        slug=event_ticker,
        title=f"Kalshi: monthly precipitation in {city} {calendar.month_name[month]} {year}",
        target=target,
        buckets=buckets,
        venue="kalshi",
    )
