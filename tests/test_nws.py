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


def test_fetch_calls_points_then_forecast_and_sets_user_agent(httpx_mock):
    points_body = {
        "properties": {"forecast": "https://api.weather.gov/gridpoints/OKX/37,46/forecast"}
    }
    httpx_mock.add_response(
        url="https://api.weather.gov/points/40.7792,-73.8803", json=points_body
    )
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


def test_fetch_raises_on_http_error(httpx_mock):
    httpx_mock.add_response(
        url="https://api.weather.gov/points/40.7792,-73.8803", status_code=500
    )
    client = httpx.Client()
    target = build_target("NYC", "TMAX", date(2026, 5, 31))
    with pytest.raises(httpx.HTTPStatusError):
        NwsSource(client).fetch(target)
    client.close()
