"""Parse Kalshi daily high-temp markets into the shared Market/Bucket types.

Kalshi exposes one binary strike per market (strike_type greater/less/between
with floor_strike/cap_strike), grouped under an event ladder. We map each strike
onto the existing Bucket (above/below/range) so the Polymarket evaluate/record
path is reused unchanged. A parallel of polymarket/markets.py for a different
wire format, sharing the Market/Bucket types.
"""

from typing import Any

from rainmaker.polymarket.markets import Bucket


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
