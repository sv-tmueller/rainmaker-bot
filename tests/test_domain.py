"""Trivial smoke test: rainmaker.domain exports the expected public symbols."""

from datetime import date

import pytest

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
    utc_day,
    utc_hour,
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


# ---------------------------------------------------------------------------
# utc_day / utc_hour: proper datetime parsing of stored ISO stamps (#429).
# The old string-slice stamps (ts[:10], ts[11:13]) silently corrupted the
# UTC day/hour whenever a stamp carried a non-UTC offset that crossed a
# midnight boundary; these tests prove that corruption mode is gone.
# ---------------------------------------------------------------------------


def test_utc_day_plain_z_suffix():
    assert utc_day("2026-05-30T23:00:00Z") == date(2026, 5, 30)


def test_utc_day_offset_stamp_normalizes_to_utc_day():
    # 23:00 at -05:00 is 04:00 the NEXT UTC day. The old slice took
    # "2026-05-30" from the local date and silently judged the wrong day.
    assert utc_day("2026-05-30T23:00:00-05:00") == date(2026, 5, 31)


def test_utc_day_positive_offset_stamp_normalizes():
    assert utc_day("2026-05-31T01:00:00+02:00") == date(2026, 5, 30)


def test_utc_day_naive_stamp_treated_as_utc():
    # Store writers emit UTC; a naive stamp keeps its literal calendar day.
    assert utc_day("2026-05-30T09:00:00") == date(2026, 5, 30)


def test_utc_day_space_separator_parses():
    # Some stores write "YYYY-MM-DD HH:MM:SS"; fromisoformat accepts it.
    assert utc_day("2026-05-30 09:00:00") == date(2026, 5, 30)


def test_utc_day_matches_legacy_slice_on_well_formed_utc():
    # Behavior preservation: for the stamps the store actually writes,
    # the helper agrees with the old [:10] slice.
    for ts in (
        "2026-05-30T09:00:00Z",
        "2026-05-30T12:00:00+00:00",
        "2026-01-01T00:30:00Z",
        "2026-12-31T23:59:59Z",
    ):
        assert utc_day(ts).isoformat() == ts[:10]


def test_utc_hour_normalizes_offset_crossing():
    # 23:00 at -05:00 is 04:00 UTC: hour 4, not 23.
    assert utc_hour("2026-05-30T23:00:00-05:00") == 4


def test_utc_hour_plain_utc_stamp():
    assert utc_hour("2026-05-30T09:00:00Z") == 9


def test_utc_hour_matches_legacy_slice_on_well_formed_utc():
    for ts in ("2026-05-30T09:00:00Z", "2026-05-30T23:00:00+00:00"):
        assert utc_hour(ts) == int(ts[11:13])


def test_utc_day_malformed_stamp_raises():
    # Malformed stamps (e.g. the "t" test sentinel) raise ValueError so the
    # callers' existing skip-on-unparsable branches keep firing; they never
    # silently produce a garbage day key.
    with pytest.raises(ValueError):
        utc_day("t")
    with pytest.raises(ValueError):
        utc_day("")
    with pytest.raises(ValueError):
        utc_day("2026-13-45T00:00:00Z")


def test_utc_day_bare_date_stamp_keeps_legacy_semantics():
    # Legacy [:10] slicing turned a bare "YYYY-MM-DD" stamp into that day;
    # fromisoformat parses it as midnight, so the helper agrees.
    assert utc_day("2026-05-30") == date(2026, 5, 30)


def test_utc_hour_malformed_stamp_raises():
    with pytest.raises(ValueError):
        utc_hour("t")


def test_day_key_and_hour_key_tolerate_malformed_stamps():
    # Grouping keys must never raise: a malformed stamp groups under its raw
    # text (day) / its legacy [11:13] slice (hour), matching old behavior.
    from rainmaker.domain import day_key, hour_key

    assert day_key("t") == "t"
    assert day_key("") == ""
    assert hour_key("t") == ""
    assert day_key("2026-05-30T23:00:00-05:00") == "2026-05-31"
    assert hour_key("2026-05-30T23:00:00-05:00") == "4"


def test_day_key_matches_legacy_slice_on_well_formed_utc():
    from rainmaker.domain import day_key

    for ts in ("2026-05-30T09:00:00Z", "2026-12-31T23:59:59+00:00"):
        assert day_key(ts) == ts[:10]
