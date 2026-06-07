import pytest

from rainmaker.kalshi.markets import parse_kalshi_bucket


def _mkt(**over):
    base = {
        "ticker": "KXHIGHNY-26JUN08-T79",
        "strike_type": "greater",
        "floor_strike": 79,
        "cap_strike": None,
        "subtitle": "above 79",
        "yes_bid_dollars": "0.0900",
        "yes_ask_dollars": "0.1200",
        "no_ask_dollars": "0.8900",
        "last_price_dollars": "0.1000",
    }
    base.update(over)
    return base


def test_greater_strike_is_above():
    b = parse_kalshi_bucket(_mkt())
    assert (b.kind, b.threshold) == ("above", 79)
    assert b.best_ask == 0.12 and b.best_bid == 0.09 and b.no_ask == 0.89


def test_less_strike_is_below():
    b = parse_kalshi_bucket(_mkt(strike_type="less", floor_strike=None, cap_strike=72))
    assert (b.kind, b.threshold) == ("below", 72)


def test_between_strike_is_range():
    b = parse_kalshi_bucket(_mkt(strike_type="between", floor_strike=78, cap_strike=79))
    assert (b.kind, b.lo, b.hi) == ("range", 78, 79)


def test_missing_price_is_none():
    b = parse_kalshi_bucket(_mkt(yes_ask_dollars="", no_ask_dollars=None))
    assert b.best_ask is None and b.no_ask is None


def test_unknown_strike_type_raises():
    with pytest.raises(ValueError):
        parse_kalshi_bucket(_mkt(strike_type="weird"))
