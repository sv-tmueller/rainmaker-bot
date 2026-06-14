from datetime import date

import pytest

from rainmaker.config import KALSHI_STATIONS
from rainmaker.kalshi.markets import parse_kalshi_bucket, parse_kalshi_event


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


def test_yes_price_prefers_last_price():
    # last_price present -> used directly, ignoring the bid/ask mid.
    b = parse_kalshi_bucket(_mkt(last_price_dollars="0.1000"))
    assert b.yes_price == pytest.approx(0.10)


def test_yes_price_falls_back_to_mid_when_last_none():
    # no last trade -> mid of yes_ask 0.12 and yes_bid 0.09 = 0.105.
    b = parse_kalshi_bucket(_mkt(last_price_dollars=None))
    assert b.yes_price == pytest.approx(0.105)


def test_yes_price_zero_when_all_none():
    # no last, no ask, no bid -> 0.0.
    b = parse_kalshi_bucket(
        _mkt(last_price_dollars=None, yes_ask_dollars=None, yes_bid_dollars=None)
    )
    assert b.yes_price == 0.0


def _event_markets():
    rule = (
        "If the highest temperature recorded in Central Park, New York for "
        "June 08, 2026 as reported by the National Weather Service's "
        "Climatological Report (Daily), is greater than 79, then Yes."
    )
    return [
        {
            "event_ticker": "KXHIGHNY-26JUN08",
            "ticker": "KXHIGHNY-26JUN08-T79",
            "strike_type": "greater",
            "floor_strike": 79,
            "subtitle": "above 79",
            "yes_bid_dollars": "0.0900",
            "yes_ask_dollars": "0.1200",
            "no_ask_dollars": "0.8900",
            "last_price_dollars": "0.1000",
            "rules_primary": rule,
        },
        {
            "event_ticker": "KXHIGHNY-26JUN08",
            "ticker": "KXHIGHNY-26JUN08-B77.5",
            "strike_type": "between",
            "floor_strike": 77,
            "cap_strike": 78,
            "subtitle": "77 to 78",
            "yes_bid_dollars": "0.1500",
            "yes_ask_dollars": "0.1600",
            "no_ask_dollars": "0.8500",
            "last_price_dollars": "0.1500",
            "rules_primary": rule,
        },
    ]


def test_parse_event_builds_market():
    m = parse_kalshi_event("NYC", KALSHI_STATIONS["NYC"], _event_markets())
    assert m.id == "KXHIGHNY-26JUN08"
    assert m.target.station.icao == "KNYC"
    assert m.target.variable == "TMAX"
    assert m.target.local_date == date(2026, 6, 8)
    assert len(m.buckets) == 2
    assert m.venue == "kalshi"


def test_parse_event_guards_station_mismatch():
    markets = _event_markets()
    for mk in markets:
        mk["rules_primary"] = mk["rules_primary"].replace("Central Park, New York", "LaGuardia")
    with pytest.raises(ValueError, match="not named"):
        parse_kalshi_event("NYC", KALSHI_STATIONS["NYC"], markets)


def test_parse_event_bad_ticker_date_raises():
    markets = _event_markets()
    for mk in markets:
        mk["event_ticker"] = "KXHIGHNY-NODATE"
    with pytest.raises(ValueError, match="date token"):
        parse_kalshi_event("NYC", KALSHI_STATIONS["NYC"], markets)


def _low_event_markets():
    # low-temp rules name only the city ("at New York City"), not the station
    rule = (
        "If the minimum temperature recorded at New York City for Jun 8, 2026, is "
        "greater than 67 fahrenheit according to the National Weather Service's "
        "Climatological Report (Daily), then Yes."
    )
    return [
        {
            "event_ticker": "KXLOWTNYC-26JUN08",
            "ticker": "KXLOWTNYC-26JUN08-T67",
            "strike_type": "greater",
            "floor_strike": 67,
            "subtitle": "above 67",
            "yes_bid_dollars": "0.4000",
            "yes_ask_dollars": "0.4200",
            "no_ask_dollars": "0.6000",
            "last_price_dollars": "0.4100",
            "rules_primary": rule,
        }
    ]


def test_parse_low_temp_event_reuses_cli_station():
    m = parse_kalshi_event("NYC", KALSHI_STATIONS["NYC"], _low_event_markets(), variable="TMIN")
    assert m.target.variable == "TMIN"
    assert m.target.station.icao == "KNYC"  # same per-city CLI station as high temp
    assert m.target.local_date == date(2026, 6, 8)
    assert "lowest temperature" in m.title


def test_parse_event_variable_mismatch_raises():
    # a high-temp ladder parsed as TMIN must be rejected by the quantity guard
    with pytest.raises(ValueError, match="not a TMIN"):
        parse_kalshi_event("NYC", KALSHI_STATIONS["NYC"], _event_markets(), variable="TMIN")


def test_greater_strike_with_none_floor_raises_value_error():
    # int(None) raises TypeError pre-fix; post-fix the parser raises ValueError
    # explicitly so the except ValueError in the client catch clause fires.
    with pytest.raises(ValueError, match="floor_strike"):
        parse_kalshi_bucket(_mkt(floor_strike=None))


def test_less_strike_with_none_cap_raises_value_error():
    # int(None) raises TypeError pre-fix; post-fix raises ValueError explicitly.
    with pytest.raises(ValueError, match="cap_strike"):
        parse_kalshi_bucket(_mkt(strike_type="less", floor_strike=None, cap_strike=None))
