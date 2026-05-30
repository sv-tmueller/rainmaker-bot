import json
from datetime import date
from pathlib import Path

from rainmaker.config import build_target
from rainmaker.forecasts.nws import parse

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
    assert s.issued_at is not None


def test_parse_returns_empty_when_date_absent():
    target = build_target("NYC", "TMAX", date(2030, 1, 1))
    assert parse(_forecast_fixture(), target) == []
