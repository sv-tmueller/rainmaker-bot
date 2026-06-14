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
