from datetime import UTC, date, datetime

from rainmaker.config import build_target
from rainmaker.forecasts.base import ForecastSample, ForecastSet, SourceCoverage


def test_forecast_sample_construct():
    s = ForecastSample(
        source="nws",
        model="nws",
        member=None,
        station="KLGA",
        variable="TMAX",
        target_date=date(2026, 5, 31),
        lead_time_days=1,
        value_f=76.0,
        issued_at=datetime(2026, 5, 30, 14, 23, 35, tzinfo=UTC),
    )
    assert s.value_f == 76.0
    assert s.member is None
    assert s.source == "nws"
    assert s.model == "nws"
    assert s.station == "KLGA"
    assert s.variable == "TMAX"
    assert s.target_date == date(2026, 5, 31)
    assert s.lead_time_days == 1
    assert s.issued_at == datetime(2026, 5, 30, 14, 23, 35, tzinfo=UTC)


def test_forecast_set_holds_samples_and_coverage():
    cov = SourceCoverage(source="nws", ok=True, n_samples=1)
    target = build_target("NYC", "TMAX", date(2026, 5, 31))
    fs = ForecastSet(target=target, samples=[], coverage=[cov])
    assert fs.coverage[0].ok is True
    assert fs.coverage[0].error is None
    assert fs.target.station.icao == "KLGA"
