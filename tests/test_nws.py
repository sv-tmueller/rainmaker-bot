import json
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from rainmaker.config import build_target
from rainmaker.forecasts.nws import NwsSource, _select_period, parse

FIXTURES = Path(__file__).parent / "fixtures"


def _forecast_fixture() -> dict:
    return json.loads((FIXTURES / "nws_forecast_klga.json").read_text())


def _hourly_fixture() -> dict:
    # Saved live forecastHourly product for KLGA (2026-10-18T23:53Z update,
    # 156 hourly periods covering 2026-10-19..25 local). Used by the TMIN
    # calendar-day retarget (#426).
    return json.loads((FIXTURES / "nws_forecast_hourly_klga.json").read_text())


HOURLY_ISSUED_AT = datetime(2026, 10, 18, 23, 53, 1, tzinfo=UTC)


def test_parse_returns_daytime_high_for_target_date():
    target = build_target("NYC", "TMAX", date(2026, 5, 31))
    samples = parse(_forecast_fixture(), target)
    assert len(samples) == 1
    s = samples[0]
    assert s.source == "nws"
    assert s.model == "nws"
    assert s.member is None
    assert s.station == "KLGA"
    assert s.variable == "TMAX"
    assert s.value_f == 76.0
    assert s.lead_time_days == 1
    assert s.issued_at == datetime(2026, 5, 30, 14, 23, 35, tzinfo=UTC)


def test_parse_returns_empty_when_date_absent():
    target = build_target("NYC", "TMAX", date(2030, 1, 1))
    assert parse(_forecast_fixture(), target) == []


def test_parse_returns_calendar_day_low_for_target_date():
    # #426: TMIN is the calendar-day low Polymarket settles on, derived from
    # the hourly product restricted to the target local day. The fixture's
    # 2026-10-20 local hours bottom out at 52F.
    target = build_target("NYC", "TMIN", date(2026, 10, 20))
    samples = parse(_hourly_fixture(), target)
    assert len(samples) == 1
    s = samples[0]
    assert s.variable == "TMIN"
    assert s.station == "KLGA"
    assert s.value_f == 52.0
    assert s.lead_time_days == 2  # issued late 10-18 EDT, target 10-20
    assert s.issued_at == HOURLY_ISSUED_AT


def test_parse_tmin_empty_when_target_day_absent_from_horizon():
    target = build_target("NYC", "TMIN", date(2030, 1, 1))
    assert parse(_hourly_fixture(), target) == []

    # A day inside the product's span but with no hourly periods (partial
    # horizon) also yields no sample rather than a garbage min.
    target_partial = build_target("NYC", "TMIN", date(2026, 10, 26))
    assert parse(_hourly_fixture(), target_partial) == []


def test_parse_tmin_rejects_partially_covered_target_day():
    # Routine lead-0 fetches happen mid-afternoon: the hourly product starts
    # at the current hour, so the already-observed morning is absent. Taking
    # the min over the surviving hours biases the sample HIGH and fabricates
    # edge on higher buckets. A partial day must yield NO sample.
    data = _hourly_fixture()
    tz = ZoneInfo("America/New_York")
    kept = []
    for period in data["properties"]["periods"]:
        lt = datetime.fromisoformat(period["startTime"]).astimezone(tz)
        if lt.date() == date(2026, 10, 20) and lt.hour < 10:
            continue  # simulate an afternoon fetch: morning hours gone
        kept.append(period)
    data["properties"]["periods"] = kept
    target = build_target("NYC", "TMIN", date(2026, 10, 20))
    assert parse(data, target) == []


def test_parse_tmin_accepts_full_coverage_across_dst_free_day():
    # Sanity for the coverage gate itself: a complete 24-hour local day
    # (00:00 through 23:00) still yields the calendar-day min.
    target = build_target("NYC", "TMIN", date(2026, 10, 24))
    samples = parse(_hourly_fixture(), target)
    assert len(samples) == 1
    assert samples[0].value_f == 45.0


def _fragment_leading_periods() -> list[dict]:
    """Night-fragment-leading period list, the hostile shape from #434.

    Built like a real post-midnight periods product: a residual "Overnight"
    fragment covering 00:00-06:00 of May 31 (the END of the May 30/31 night)
    precedes the wanted "Tonight" period (May 31 18:00 -> June 1 06:00).
    Both begin on May 31, so the legacy first-hit predicate cannot tell them
    apart. Times are America/New_York (-04:00), matching the KLGA fixture.
    """
    return [
        {
            "number": 1,
            "name": "Overnight",
            "startTime": "2026-05-31T00:00:00-04:00",
            "endTime": "2026-05-31T06:00:00-04:00",
            "isDaytime": False,
            "temperature": 48,
            "temperatureUnit": "F",
        },
        {
            "number": 2,
            "name": "Today",
            "startTime": "2026-05-31T06:00:00-04:00",
            "endTime": "2026-05-31T18:00:00-04:00",
            "isDaytime": True,
            "temperature": 71,
            "temperatureUnit": "F",
        },
        {
            "number": 3,
            "name": "Tonight",
            "startTime": "2026-05-31T18:00:00-04:00",
            "endTime": "2026-06-01T06:00:00-04:00",
            "isDaytime": False,
            "temperature": 62,
            "temperatureUnit": "F",
        },
    ]


def test_matcher_prefers_covering_night_over_fragment_shadowing():
    # Corruption mode (#434): TMIN target June 1 wants the night ENDING on
    # June 1. The leading Overnight fragment also begins on the match date
    # (May 31), so first-hit returns its residual 48F of the ENDING night -
    # anchoring the forecast on the wrong night entirely.
    tz = ZoneInfo("America/New_York")
    picked = _select_period(
        _fragment_leading_periods(),
        want_daytime=False,
        match_date=date(2026, 5, 31),
        target_date=date(2026, 6, 1),
        tz=tz,
    )
    assert picked is not None
    assert picked["name"] == "Tonight"
    assert picked["temperature"] == 62


def test_matcher_covers_ongoing_night_when_evening_period_dropped():
    # Silent-loss mode (#434): lead-0 TMIN (target May 31, match date May 30,
    # the ongoing night). The WFO replaced the originating "Tonight" period
    # (May 30 18:00 -> May 31 06:00) with the post-midnight fragment, so the
    # list holds ONLY the fragment among nights. It begins on May 31, not the
    # match date, so first-hit finds nothing and the source silently yields
    # zero samples. The fragment ENDS on the target date, i.e. it covers the
    # remainder of the wanted window: it must be selected.
    periods = [p for p in _fragment_leading_periods() if p["name"] != "Tonight"]
    tz = ZoneInfo("America/New_York")
    picked = _select_period(
        periods,
        want_daytime=False,
        match_date=date(2026, 5, 30),
        target_date=date(2026, 5, 31),
        tz=tz,
    )
    assert picked is not None
    assert picked["name"] == "Overnight"


def test_matcher_keeps_first_hit_as_fallback_without_covering_candidate():
    # No qualifying period ends on the target date (both candidates end on
    # May 31 for a June 1 target): first-hit on the match date stands.
    periods = [
        {
            "number": 1,
            "name": "Overnight",
            "startTime": "2026-05-31T00:00:00-04:00",
            "endTime": "2026-05-31T06:00:00-04:00",
            "isDaytime": False,
            "temperature": 48,
            "temperatureUnit": "F",
        }
    ]
    tz = ZoneInfo("America/New_York")
    picked = _select_period(
        periods,
        want_daytime=False,
        match_date=date(2026, 5, 31),
        target_date=date(2026, 6, 1),
        tz=tz,
    )
    assert picked is not None
    assert picked["name"] == "Overnight"


def test_matcher_tmax_still_matches_first_daytime_period():
    # TMAX semantics are untouched: the daytime period starting on the
    # target date is the covering window by construction.
    tz = ZoneInfo("America/New_York")
    picked = _select_period(
        _fragment_leading_periods(),
        want_daytime=True,
        match_date=date(2026, 5, 31),
        target_date=date(2026, 5, 31),
        tz=tz,
    )
    assert picked is not None
    assert picked["name"] == "Today"
    assert picked["temperature"] == 71


def test_fetch_calls_points_then_forecast_and_sets_user_agent(httpx_mock):
    points_body = {
        "properties": {"forecast": "https://api.weather.gov/gridpoints/OKX/37,46/forecast"}
    }
    httpx_mock.add_response(url="https://api.weather.gov/points/40.7792,-73.8803", json=points_body)
    httpx_mock.add_response(
        url="https://api.weather.gov/gridpoints/OKX/37,46/forecast",
        json=_forecast_fixture(),
    )
    client = httpx.Client(headers={"User-Agent": "rainmaker-bot (test)"})
    target = build_target("NYC", "TMAX", date(2026, 5, 31))
    samples = NwsSource(client).fetch(target)
    client.close()
    assert len(samples) == 1
    assert samples[0].value_f == 76.0
    requests = httpx_mock.get_requests()
    assert all(r.headers["User-Agent"] == "rainmaker-bot (test)" for r in requests)


def test_fetch_returns_tmin_via_hourly_product(httpx_mock):
    # #426: TMIN dispatches to properties.forecastHourly, not the periods
    # product.
    points_body = {
        "properties": {
            "forecast": "https://api.weather.gov/gridpoints/OKX/37,46/forecast",
            "forecastHourly": "https://api.weather.gov/gridpoints/OKX/37,46/forecast/hourly",
        }
    }
    httpx_mock.add_response(url="https://api.weather.gov/points/40.7792,-73.8803", json=points_body)
    httpx_mock.add_response(
        url="https://api.weather.gov/gridpoints/OKX/37,46/forecast/hourly",
        json=_hourly_fixture(),
    )
    client = httpx.Client()
    target = build_target("NYC", "TMIN", date(2026, 10, 20))
    samples = NwsSource(client).fetch(target)
    client.close()
    assert len(samples) == 1
    assert samples[0].value_f == 52.0
    assert samples[0].variable == "TMIN"
    # The periods product must NOT have been requested.
    urls = [str(r.url) for r in httpx_mock.get_requests()]
    assert all("/forecast/hourly" in u or u.endswith("/points/40.7792,-73.8803") for u in urls)


def test_parse_raises_not_implemented_for_unsupported_variable():
    # PRCP falls through to the TMIN branch and returns a mislabeled result.
    # The guard must raise NotImplementedError for anything other than TMAX/TMIN.
    target = build_target("NYC", "PRCP", date(2026, 5, 31))
    with pytest.raises(NotImplementedError):
        parse(_forecast_fixture(), target)


def test_fetch_raises_on_http_error(httpx_mock):
    httpx_mock.add_response(url="https://api.weather.gov/points/40.7792,-73.8803", status_code=500)
    client = httpx.Client()
    target = build_target("NYC", "TMAX", date(2026, 5, 31))
    with pytest.raises(httpx.HTTPStatusError):
        NwsSource(client).fetch(target)
    client.close()


def test_fetch_raises_when_forecast_call_fails(httpx_mock):
    # points lookup succeeds, but the second call (the forecast product) errors.
    points_body = {
        "properties": {"forecast": "https://api.weather.gov/gridpoints/OKX/37,46/forecast"}
    }
    httpx_mock.add_response(url="https://api.weather.gov/points/40.7792,-73.8803", json=points_body)
    httpx_mock.add_response(
        url="https://api.weather.gov/gridpoints/OKX/37,46/forecast", status_code=500
    )
    client = httpx.Client()
    target = build_target("NYC", "TMAX", date(2026, 5, 31))
    with pytest.raises(httpx.HTTPStatusError):
        NwsSource(client).fetch(target)
    client.close()


def test_parse_rejects_non_fahrenheit_unit():
    # NWS reports degrees C in some products; the matching period must be guarded.
    data = _forecast_fixture()
    target = build_target("NYC", "TMAX", date(2026, 5, 31))
    for period in data["properties"]["periods"]:
        period["temperatureUnit"] = "C"
    with pytest.raises(ValueError, match="Fahrenheit"):
        parse(data, target)


def test_parse_tmin_rejects_non_fahrenheit_unit():
    # The hourly-product guard mirrors the periods guard: any hour of the
    # target day in C units refuses rather than producing a bogus min.
    data = _hourly_fixture()
    target = build_target("NYC", "TMIN", date(2026, 10, 20))
    for period in data["properties"]["periods"]:
        period["temperatureUnit"] = "C"
    with pytest.raises(ValueError, match="Fahrenheit"):
        parse(data, target)
