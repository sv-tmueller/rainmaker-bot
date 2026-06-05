import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from rainmaker.polymarket.markets import (
    Market,
    parse_bucket,
    parse_bucket_label,
    parse_market,
    parse_variable,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _nyc_event() -> dict[str, Any]:
    events = json.loads((FIXTURES / "polymarket_weather_events.json").read_text())
    return next(e for e in events if e["id"] == "533147")


def test_parse_bucket_label_below():
    assert parse_bucket_label("59°F or below") == ("below", None, None, 59)


def test_parse_bucket_label_range():
    assert parse_bucket_label("70-71°F") == ("range", 70, 71, None)


def test_parse_bucket_label_above():
    assert parse_bucket_label("78°F or higher") == ("above", None, None, 78)


def test_parse_bucket_below():
    market = _nyc_event()["markets"][0]
    b = parse_bucket(market)
    assert b.label == "59°F or below"
    assert b.kind == "below"
    assert b.threshold == 59
    assert b.lo is None and b.hi is None
    assert b.best_ask == 0.004
    assert b.best_bid == 0.001
    assert b.yes_price == 0.0025
    assert b.yes_token_id == (
        "63103732622160665189154558165913165656167238975108887912070417445520275819404"
    )


def test_parse_bucket_derives_no_side():
    markets = _nyc_event()["markets"]
    below = parse_bucket(markets[0])
    # NO ask = 1 - YES bid (buying NO == selling YES).
    assert below.no_ask == pytest.approx(1 - 0.001)
    assert below.no_token_id == json.loads(markets[0]["clobTokenIds"])[1]
    # A null YES bid means no resting NO offer to take, so no NO ask.
    above = parse_bucket(markets[10])
    assert above.best_bid is None
    assert above.no_ask is None


def test_parse_bucket_range_and_above():
    markets = _nyc_event()["markets"]
    rng = parse_bucket(markets[6])
    assert (rng.label, rng.kind, rng.lo, rng.hi) == ("70-71°F", "range", 70, 71)
    assert rng.best_ask == 0.999

    above = parse_bucket(markets[10])
    assert (above.label, above.kind, above.threshold) == ("78°F or higher", "above", 78)
    assert above.best_bid is None


def test_parse_variable():
    assert parse_variable("Highest temperature in NYC on May 30?") == "TMAX"
    assert parse_variable("Lowest temperature in Miami on May 29?") == "TMIN"


def test_parse_market_nyc():
    m = parse_market(_nyc_event())
    assert isinstance(m, Market)
    assert m.id == "533147"
    assert m.target.station.icao == "KLGA"
    assert m.target.variable == "TMAX"
    assert m.target.local_date == date(2026, 5, 30)
    assert len(m.buckets) == 11
    assert m.buckets[0].kind == "below"


def test_parse_market_unknown_city_raises():
    event = dict(_nyc_event())
    event["title"] = "Highest temperature in Atlantis on May 30?"
    with pytest.raises(KeyError):
        parse_market(event)


def test_parse_market_station_mismatch_raises():
    event = dict(_nyc_event())
    event["description"] = "resolves at some other station, no icao here"
    with pytest.raises(ValueError, match="resolution station"):
        parse_market(event)


def test_parse_bucket_label_negative_below():
    assert parse_bucket_label("-5°F or below") == ("below", None, None, -5)


def test_parse_bucket_label_negative_range():
    assert parse_bucket_label("-10--5°F") == ("range", -10, -5, None)


def test_parse_market_rejects_non_midday_enddate():
    event = dict(_nyc_event())
    event["endDate"] = "2026-05-30T03:00:00Z"
    with pytest.raises(ValueError, match="midday UTC"):
        parse_market(event)


def test_parse_market_tmin():
    event = dict(_nyc_event())
    event["title"] = "Lowest temperature in NYC on May 30?"
    m = parse_market(event)
    assert m.target.variable == "TMIN"
    assert m.target.station.icao == "KLGA"


def test_parse_bucket_label_unrecognized_raises():
    with pytest.raises(ValueError, match="unrecognized"):
        parse_bucket_label("total nonsense")
