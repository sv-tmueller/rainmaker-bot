import json
from datetime import date
from pathlib import Path

from rainmaker.config import build_target
from rainmaker.forecasts.openmeteo import parse_multimodel

FIXTURES = Path(__file__).parent / "fixtures"


def _multimodel_fixture() -> dict:  # type: ignore[type-arg]
    return json.loads((FIXTURES / "openmeteo_multimodel_klga.json").read_text())


def test_parse_multimodel_returns_one_sample_per_model():
    target = build_target("NYC", "TMAX", date(2026, 5, 31))
    samples = parse_multimodel(_multimodel_fixture(), target)
    by_model = {s.model: s.value_f for s in samples}
    assert by_model == {
        "gfs_seamless": 74.6,
        "ecmwf_ifs025": 77.3,
        "icon_seamless": 74.9,
        "gem_seamless": 75.1,
        "meteofrance_seamless": 73.8,
    }
    for s in samples:
        assert s.source == "open-meteo"
        assert s.member is None
        assert s.station == "KLGA"
        assert s.lead_time_days == 1
        assert s.issued_at is None


def test_parse_multimodel_empty_when_date_absent():
    target = build_target("NYC", "TMAX", date(2030, 1, 1))
    assert parse_multimodel(_multimodel_fixture(), target) == []
