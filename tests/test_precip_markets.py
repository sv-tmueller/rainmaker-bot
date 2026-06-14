import json
from datetime import date
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

from rainmaker.domain import SETTLEMENT_DECIMALS, PrecipMonthlyMarket, parse_precip_bracket_label
from rainmaker.polymarket.precip_markets import (
    ROUND_BETWEEN_BRACKETS_UP,
    parse_precip_bracket,
    parse_precip_event,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _nyc_event() -> dict[str, Any]:
    return json.loads((FIXTURES / "polymarket_precip_monthly_nyc.json").read_text())


def _seattle_event() -> dict[str, Any]:
    return json.loads((FIXTURES / "polymarket_precip_monthly_seattle.json").read_text())


def test_parse_precip_bracket_label_open_low_tail():
    assert parse_precip_bracket_label('<2"') == ("below", None, None, 2.0)
    assert parse_precip_bracket_label('<0.5"') == ("below", None, None, 0.5)


def test_parse_precip_bracket_label_interior_range():
    assert parse_precip_bracket_label('2-3"') == ("range", 2.0, 3.0, None)
    assert parse_precip_bracket_label('0.5-1"') == ("range", 0.5, 1.0, None)
    assert parse_precip_bracket_label('2.5-3"') == ("range", 2.5, 3.0, None)


def test_parse_precip_bracket_label_open_high_tail():
    assert parse_precip_bracket_label('>6"') == ("above", None, None, 6.0)
    assert parse_precip_bracket_label('>3"') == ("above", None, None, 3.0)


def test_parse_precip_bracket_label_unrecognized_raises():
    with pytest.raises(ValueError, match="unrecognized"):
        parse_precip_bracket_label("total nonsense")


def test_parse_precip_bracket_label_inverted_range_raises():
    with pytest.raises(ValueError, match="inverted"):
        parse_precip_bracket_label('3-2"')


def test_parse_precip_bracket_from_fixture():
    market = _nyc_event()["markets"][0]  # the open-low tail "<2\""
    b = parse_precip_bracket(market)
    assert b.label == '<2"'
    assert b.kind == "below"
    assert b.threshold == 2.0
    assert b.lo is None and b.hi is None
    assert b.yes_price == 0.41
    assert b.best_ask == 0.49
    assert b.best_bid == 0.33
    token_ids = json.loads(market["clobTokenIds"])
    assert b.yes_token_id == token_ids[0]
    assert b.no_token_id == token_ids[1]
    # NO ask = 1 - YES bid (buying NO == selling YES), same as the temperature path.
    assert b.no_ask == pytest.approx(1 - 0.33)


def test_parse_precip_bracket_null_bid_has_no_no_ask():
    market = dict(_nyc_event()["markets"][0])
    market["bestBid"] = None
    b = parse_precip_bracket(market)
    assert b.no_ask is None


def test_parse_precip_event_nyc():
    m = parse_precip_event(_nyc_event())
    assert isinstance(m, PrecipMonthlyMarket)
    assert m.id == "531291"
    assert m.target.station.ghcnd_id == "USW00094728"
    assert m.target.variable == "PRCP"
    assert m.target.year == 2026
    assert m.target.month == 6
    assert m.target.settlement_date == date(2026, 6, 30)
    assert len(m.buckets) == 6


def test_parse_precip_event_seattle_half_inch_step():
    m = parse_precip_event(_seattle_event())
    assert m.id == "531299"
    assert m.target.station.ghcnd_id == "USW00094290"
    assert m.target.settlement_date == date(2026, 6, 30)
    assert len(m.buckets) == 7
    labels = {b.label for b in m.buckets}
    assert {'<0.5"', '0.5-1"', '2.5-3"', '>3"'} <= labels


def test_parse_precip_event_unknown_city_raises():
    event = dict(_nyc_event())
    event["title"] = "Precipitation in Atlantis in June?"
    with pytest.raises(KeyError):
        parse_precip_event(event)


def test_parse_precip_event_station_mismatch_raises():
    event = dict(_nyc_event())
    event["description"] = "resolves at some other station, Central Park is not named here"
    with pytest.raises(ValueError, match="resolution station"):
        parse_precip_event(event)


def test_parse_precip_event_enddate_month_mismatch_raises():
    event = dict(_nyc_event())
    event["endDate"] = "2026-07-31T00:00:00Z"  # July, but the title says June
    with pytest.raises(ValueError, match="month"):
        parse_precip_event(event)


def _assert_tiles_the_line(m: PrecipMonthlyMarket) -> None:
    below = [b for b in m.buckets if b.kind == "below"]
    above = [b for b in m.buckets if b.kind == "above"]
    ranges = sorted((b for b in m.buckets if b.kind == "range"), key=lambda b: b.lo or 0.0)
    assert len(below) == 1 and len(above) == 1  # exactly two open tails
    assert below[0].threshold == ranges[0].lo  # low tail meets the first range
    assert above[0].threshold == ranges[-1].hi  # high tail meets the last range
    for left, right in pairwise(ranges):
        assert left.hi == right.lo  # interior ranges are contiguous, no gaps or overlaps


def test_nyc_brackets_tile_the_line():
    _assert_tiles_the_line(parse_precip_event(_nyc_event()))


def test_seattle_brackets_tile_the_line():
    _assert_tiles_the_line(parse_precip_event(_seattle_event()))


def test_settlement_rule_constants_match_the_market_rules():
    # NOAA monthly figure is reported to 2 decimals; a value exactly between two
    # brackets resolves to the higher one. Confirmed from both market descriptions.
    assert SETTLEMENT_DECIMALS == 2
    assert ROUND_BETWEEN_BRACKETS_UP is True
