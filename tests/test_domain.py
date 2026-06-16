"""Trivial smoke test: rainmaker.domain exports the expected public symbols."""

from rainmaker.domain import (
    SETTLEMENT_DECIMALS,
    Bucket,
    BucketKind,
    Market,
    PrecipBracket,
    PrecipMonthlyMarket,
    PrecipTarget,
    parse_bucket_label,
    parse_precip_bracket_label,
)


def test_domain_exports_bucket_kind() -> None:
    assert BucketKind  # the Literal type is truthy when referenced


def test_domain_exports_settlement_decimals() -> None:
    assert SETTLEMENT_DECIMALS == 2


def test_domain_exports_parse_bucket_label() -> None:
    # Negative-range case: confirms the integer-safe regex is in play.
    assert parse_bucket_label("-10--5°F") == ("range", -10, -5, None)


def test_domain_exports_parse_precip_bracket_label() -> None:
    # Float range case.
    assert parse_precip_bracket_label('2.5-3"') == ("range", 2.5, 3.0, None)


def test_domain_exports_model_classes() -> None:
    assert Bucket
    assert Market
    assert PrecipBracket
    assert PrecipMonthlyMarket
    assert PrecipTarget


def test_parse_bucket_label_single_degree_celsius() -> None:
    # Single-degree Celsius buckets like "15°C" come from Polymarket intl markets.
    # They map to a range bucket with lo=hi=value.
    assert parse_bucket_label("15°C") == ("range", 15, 15, None)
    assert parse_bucket_label("27°C") == ("range", 27, 27, None)
    assert parse_bucket_label("-3°C") == ("range", -3, -3, None)


def test_parse_bucket_label_single_degree_celsius_boundary() -> None:
    # Below/above tails with the degree sign still parse correctly.
    assert parse_bucket_label("14°C or below") == ("below", None, None, 14)
    assert parse_bucket_label("24°C or higher") == ("above", None, None, 24)


def test_parse_bucket_label_existing_fahrenheit_unaffected() -> None:
    # The new single-degree branch must not disturb existing F parsing.
    assert parse_bucket_label("70-71°F") == ("range", 70, 71, None)
    assert parse_bucket_label("-10--5°F") == ("range", -10, -5, None)
    assert parse_bucket_label("59°F or below") == ("below", None, None, 59)
    assert parse_bucket_label("78°F or higher") == ("above", None, None, 78)
