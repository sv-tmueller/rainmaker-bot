import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest

from rainmaker.config import build_target
from rainmaker.forecasts.openmeteo import (
    ENSEMBLE_URL,
    FORECAST_URL,
    OpenMeteoSource,
    parse_ensemble,
    parse_multimodel,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _multimodel_fixture() -> dict[str, Any]:
    return json.loads((FIXTURES / "openmeteo_multimodel_klga.json").read_text())


def _ensemble_fixture() -> dict[str, Any]:
    return json.loads((FIXTURES / "openmeteo_ensemble_gfs_klga.json").read_text())


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


def test_parse_ensemble_returns_one_sample_per_member():
    target = build_target("NYC", "TMAX", date(2026, 5, 31))
    samples = parse_ensemble(_ensemble_fixture(), target, "gfs_seamless")
    assert len(samples) == 30
    members = {s.member for s in samples}
    assert members == set(range(1, 31))
    m1 = next(s for s in samples if s.member == 1)
    assert m1.value_f == 73.0
    assert m1.model == "gfs_seamless_ens"
    assert m1.source == "open-meteo"
    assert m1.lead_time_days == 1
    assert m1.issued_at is None


def test_parse_ensemble_empty_when_date_absent():
    target = build_target("NYC", "TMAX", date(2030, 1, 1))
    assert parse_ensemble(_ensemble_fixture(), target, "gfs_seamless") == []


def test_open_meteo_source_pools_multimodel_and_ensemble(httpx_mock):
    httpx_mock.add_response(url=re.compile(re.escape(FORECAST_URL)), json=_multimodel_fixture())
    for _ in range(3):
        httpx_mock.add_response(url=re.compile(re.escape(ENSEMBLE_URL)), json=_ensemble_fixture())

    target = build_target("NYC", "TMAX", date(2026, 5, 31))
    with httpx.Client() as client:
        samples = OpenMeteoSource(client).fetch(target)

    multimodel = [s for s in samples if s.member is None]
    ensemble = [s for s in samples if s.member is not None]
    assert len(multimodel) == 5
    assert len(ensemble) == 30 * 3  # OPENMETEO_ENSEMBLE_MODELS has 3 entries


def test_parse_multimodel_rejects_non_fahrenheit():
    data = _multimodel_fixture()
    data["daily_units"] = {
        k: ("°C" if k.startswith("temperature") else v) for k, v in data["daily_units"].items()
    }
    target = build_target("NYC", "TMAX", date(2026, 5, 31))
    with pytest.raises(ValueError, match="Fahrenheit"):
        parse_multimodel(data, target)


def test_parse_ensemble_rejects_non_fahrenheit():
    data = _ensemble_fixture()
    data["daily_units"] = {
        k: ("°C" if k.startswith("temperature") else v) for k, v in data["daily_units"].items()
    }
    target = build_target("NYC", "TMAX", date(2026, 5, 31))
    with pytest.raises(ValueError, match="Fahrenheit"):
        parse_ensemble(data, target, "gfs_seamless")
