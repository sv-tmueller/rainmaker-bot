from datetime import UTC, date, datetime, timedelta

from rainmaker.config import build_target
from rainmaker.forecasts.aggregate import aggregate
from rainmaker.forecasts.base import ForecastSample

TARGET = build_target("NYC", "TMAX", date(2026, 5, 31))
NOW = datetime(2026, 5, 30, 15, 0, tzinfo=UTC)


def _sample(model: str, issued_at: datetime | None) -> ForecastSample:
    return ForecastSample(
        source="x",
        model=model,
        member=None,
        station="KLGA",
        variable="TMAX",
        target_date=date(2026, 5, 31),
        lead_time_days=1,
        value_f=70.0,
        issued_at=issued_at,
    )


class _StubSource:
    def __init__(self, name, samples=None, error=None):
        self.name = name
        self._samples = samples or []
        self._error = error

    def fetch(self, target):
        if self._error:
            raise self._error
        return self._samples


def test_aggregate_pools_samples_and_records_ok_coverage():
    src = _StubSource("a", samples=[_sample("m1", NOW), _sample("m2", NOW)])
    fs = aggregate(TARGET, [src], now=NOW)
    assert len(fs.samples) == 2
    assert fs.coverage[0].source == "a"
    assert fs.coverage[0].ok is True
    assert fs.coverage[0].n_samples == 2


def test_aggregate_records_failure_and_continues():
    good = _StubSource("good", samples=[_sample("m1", NOW)])
    bad = _StubSource("bad", error=RuntimeError("source down"))
    fs = aggregate(TARGET, [good, bad], now=NOW)
    assert len(fs.samples) == 1
    cov = {c.source: c for c in fs.coverage}
    assert cov["good"].ok is True
    assert cov["bad"].ok is False
    assert "source down" in cov["bad"].error


def test_aggregate_drops_stale_samples_but_keeps_unknown_issue_time():
    stale = _sample("stale", NOW - timedelta(hours=48))
    fresh = _sample("fresh", NOW - timedelta(hours=1))
    unknown = _sample("unknown", None)
    src = _StubSource("a", samples=[stale, fresh, unknown])
    fs = aggregate(TARGET, [src], now=NOW, freshness_limit_hours=24)
    kept = {s.model for s in fs.samples}
    assert kept == {"fresh", "unknown"}
    assert fs.coverage[0].n_samples == 2


def test_aggregate_empty_sources_returns_empty_set():
    fs = aggregate(TARGET, [], now=NOW)
    assert fs.samples == []
    assert fs.coverage == []


def test_aggregate_all_sources_fail_returns_empty_samples_with_failed_coverage():
    bad1 = _StubSource("a", error=RuntimeError("down"))
    bad2 = _StubSource("b", error=ValueError("bad data"))
    fs = aggregate(TARGET, [bad1, bad2], now=NOW)
    assert fs.samples == []
    assert all(not c.ok for c in fs.coverage)
    assert len(fs.coverage) == 2


def test_aggregate_all_stale_source_is_ok_with_zero_samples():
    stale_only = _StubSource("a", samples=[_sample("m1", NOW - timedelta(hours=48))])
    fs = aggregate(TARGET, [stale_only], now=NOW, freshness_limit_hours=24)
    assert fs.samples == []
    assert fs.coverage[0].ok is True
    assert fs.coverage[0].n_samples == 0
