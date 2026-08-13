from datetime import date

import pytest

from rainmaker.config import KALSHI_PRECIP_STATIONS
from rainmaker.domain import parse_precip_bracket_label
from rainmaker.kalshi.precip_markets import parse_kalshi_precip_bracket, parse_kalshi_precip_event


def _rain_markets():
    rule = (
        "If the total precipitation at Central Park, New York City in Jun 2026 is "
        "strictly greater than 4 inches, then the market resolves to Yes."
    )
    return [
        {
            "event_ticker": "KXRAINNYCM-26JUN",
            "ticker": "KXRAINNYCM-26JUN-4",
            "strike_type": "greater",
            "floor_strike": 4,
            "subtitle": "greater than 4in",
            "yes_bid_dollars": "0.1000",
            "yes_ask_dollars": "0.1200",
            "no_ask_dollars": "0.8800",
            "last_price_dollars": "0.1100",
            "rules_primary": rule,
        },
        {
            "event_ticker": "KXRAINNYCM-26JUN",
            "ticker": "KXRAINNYCM-26JUN-B2.5",
            "strike_type": "between",
            "floor_strike": 2,
            "cap_strike": 3,
            "subtitle": "2 to 3in",
            "yes_bid_dollars": "0.3000",
            "yes_ask_dollars": "0.3200",
            "no_ask_dollars": "0.6800",
            "last_price_dollars": "0.3100",
            "rules_primary": rule,
        },
    ]


def test_precip_bracket_above_uses_inches():
    b = parse_kalshi_precip_bracket(_rain_markets()[0])
    assert (b.kind, b.threshold) == ("above", 4.0)
    assert b.best_ask == 0.12 and b.no_ask == 0.88


def test_precip_bracket_between_uses_inches():
    b = parse_kalshi_precip_bracket(_rain_markets()[1])
    assert (b.kind, b.lo, b.hi) == ("range", 2.0, 3.0)


def test_parse_precip_event_builds_monthly_market():
    m = parse_kalshi_precip_event("NYC", KALSHI_PRECIP_STATIONS["NYC"], _rain_markets())
    assert m.id == "KXRAINNYCM-26JUN"
    assert m.target.variable == "PRCP"
    assert (m.target.year, m.target.month) == (2026, 6)
    assert m.target.settlement_date == date(2026, 6, 30)  # last day of the month
    assert m.target.station.ghcnd_id == "USW00094728"  # Central Park
    assert len(m.buckets) == 2
    assert m.venue == "kalshi"


def test_parse_precip_event_guards_station():
    markets = _rain_markets()
    for mk in markets:
        mk["rules_primary"] = mk["rules_primary"].replace("Central Park", "JFK Airport")
    with pytest.raises(ValueError, match="not named"):
        parse_kalshi_precip_event("NYC", KALSHI_PRECIP_STATIONS["NYC"], markets)


def test_parse_precip_event_rejects_non_precip():
    markets = _rain_markets()
    for mk in markets:
        mk["rules_primary"] = "If the highest temperature is greater than 80, then Yes."
    with pytest.raises(ValueError, match="not a precipitation"):
        parse_kalshi_precip_event("NYC", KALSHI_PRECIP_STATIONS["NYC"], markets)


def _rain_bracket(**over):
    base = {
        "ticker": "KXRAINNYCM-26JUN-4",
        "strike_type": "greater",
        "floor_strike": 4,
        "cap_strike": None,
        "subtitle": "greater than 4in",
        "yes_bid_dollars": "0.1000",
        "yes_ask_dollars": "0.1200",
        "no_ask_dollars": "0.8800",
        "last_price_dollars": "0.1100",
    }
    base.update(over)
    return base


def test_precip_greater_strike_none_floor_raises_value_error():
    # float(None) raises TypeError pre-fix; post-fix raises ValueError explicitly
    # so the except ValueError in the client catch clause fires.
    with pytest.raises(ValueError, match="floor_strike"):
        parse_kalshi_precip_bracket(_rain_bracket(floor_strike=None))


def test_precip_less_strike_none_cap_raises_value_error():
    # float(None) raises TypeError pre-fix; post-fix raises ValueError explicitly.
    with pytest.raises(ValueError, match="cap_strike"):
        parse_kalshi_precip_bracket(
            _rain_bracket(strike_type="less", floor_strike=None, cap_strike=None)
        )


def test_precip_between_strike_none_floor_raises_value_error():
    with pytest.raises(ValueError, match="floor_strike"):
        parse_kalshi_precip_bracket(
            _rain_bracket(strike_type="between", floor_strike=None, cap_strike=3)
        )


def test_precip_between_strike_none_cap_raises_value_error():
    with pytest.raises(ValueError, match="cap_strike"):
        parse_kalshi_precip_bracket(
            _rain_bracket(strike_type="between", floor_strike=2, cap_strike=None)
        )


# ---------------------------------------------------------------------------
# Label synthesis for a bare/missing subtitle (#333: root cause of the
# unrecognized precip bracket label 'inches' that killed the diagnostics jobs)
# ---------------------------------------------------------------------------


def test_bare_unit_subtitle_synthesizes_canonical_label_above():
    # Kalshi sent subtitle="inches" (no digit) for this strike in the wild.
    b = parse_kalshi_precip_bracket(_rain_bracket(subtitle="inches"))
    assert b.label == '>4"'
    parse_precip_bracket_label(b.label)  # must round-trip through the label parser


def test_bare_unit_subtitle_synthesizes_canonical_label_below():
    b = parse_kalshi_precip_bracket(
        _rain_bracket(strike_type="less", floor_strike=None, cap_strike=2, subtitle="inches")
    )
    assert b.label == '<2"'
    parse_precip_bracket_label(b.label)


def test_bare_unit_subtitle_synthesizes_canonical_label_range():
    b = parse_kalshi_precip_bracket(
        _rain_bracket(strike_type="between", floor_strike=2, cap_strike=3, subtitle="inches")
    )
    assert b.label == '2-3"'
    parse_precip_bracket_label(b.label)


def test_missing_subtitle_synthesizes_canonical_label():
    b = parse_kalshi_precip_bracket(_rain_bracket(subtitle=None))
    assert b.label == '>4"'


def test_empty_subtitle_synthesizes_canonical_label():
    b = parse_kalshi_precip_bracket(_rain_bracket(subtitle=""))
    assert b.label == '>4"'


def test_well_formed_subtitle_is_not_relabeled():
    """Digit-bearing subtitles are the join key for historical rows: must pass through
    unchanged, never rewritten to the canonical form."""
    b = parse_kalshi_precip_bracket(_rain_bracket(subtitle="greater than 4in"))
    assert b.label == "greater than 4in"


def test_duplicate_synthesized_labels_raise_value_error():
    """Two strikes both missing a usable subtitle would synthesize colliding
    labels if their strike geometry matched; the ladder-level guard catches this
    even when the strikes differ, since a same-kind duplicate is still unsafe as
    a join key for one settled bucket."""
    markets = _rain_markets()
    for mk in markets:
        mk["subtitle"] = "inches"
    # Force both strikes to synthesize the same label (both 'above' at the same
    # threshold), simulating a malformed ladder.
    markets[1]["strike_type"] = "greater"
    markets[1]["floor_strike"] = 4
    markets[1]["cap_strike"] = None
    with pytest.raises(ValueError, match="duplicate"):
        parse_kalshi_precip_event("NYC", KALSHI_PRECIP_STATIONS["NYC"], markets)
