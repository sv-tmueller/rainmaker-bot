import json
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from rainmaker.config import build_target
from rainmaker.forecasts.nws import NwsSource, parse

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
