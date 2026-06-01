from datetime import date

from rainmaker.config import build_target
from rainmaker.forecasts.base import ForecastSample, ForecastSet, SourceCoverage
from rainmaker.polymarket.markets import Bucket, Market
from rainmaker.probability.calibration import Calibration
from rainmaker.ranking.edge import MarketReport, evaluate_market


def _bucket(label, kind, *, lo=None, hi=None, threshold=None, best_ask=None) -> Bucket:
    return Bucket(
        label=label,
        kind=kind,
        lo=lo,
        hi=hi,
        threshold=threshold,
        yes_token_id="t",
        best_ask=best_ask,
        best_bid=None,
        yes_price=0.0,
    )


def _market(buckets) -> Market:
    return Market(
        id="m1",
        slug="s",
        title="Highest temperature in NYC on May 31?",
        target=build_target("NYC", "TMAX", date(2026, 5, 31)),
        buckets=buckets,
    )


def _forecast_set(values, *, ok_sources=("nws", "open-meteo")) -> ForecastSet:
    # All samples are tagged source="nws" for simplicity; n_sources is derived from
    # coverage entries (ok=True), not from sample tags.
    samples = [
        ForecastSample(
            source="nws",
            model="m",
            member=None,
            station="KLGA",
            variable="TMAX",
            target_date=date(2026, 5, 31),
            lead_time_days=1,
            value_f=v,
            issued_at=None,
        )
        for v in values
    ]
    coverage = [SourceCoverage(source=s, ok=True, n_samples=len(values)) for s in ok_sources]
    return ForecastSet(
        target=build_target("NYC", "TMAX", date(2026, 5, 31)),
        samples=samples,
        coverage=coverage,
    )


def test_evaluate_market_ranks_by_edge_and_flags_recommended():
    # Forecast centered at 70.5 -> mode bucket 70-71 has high P(win).
    market = _market(
        [
            _bucket("70-71°F", "range", lo=70, hi=71, best_ask=0.40),  # cheap mode -> big edge
            _bucket("72-73°F", "range", lo=72, hi=73, best_ask=0.30),
        ]
    )
    fs = _forecast_set([69, 70, 71, 72])  # mean 70.5
    # floor=0.45: p_win for the mode bucket at mu=70.5, sigma=1.5 is ~0.495, which
    # clears 0.45 but not 0.50 (2-degree bucket + sigma floor make it tight).
    report = evaluate_market(market, fs, floor=0.45, min_sources=2, min_sigma=1.5)
    assert isinstance(report, MarketReport)
    assert report.n_sources == 2
    # sorted by edge desc
    assert [o.bucket_label for o in report.outcomes] == ["70-71°F", "72-73°F"]
    top = report.outcomes[0]
    assert top.edge > 0
    assert top.recommended is True


def test_recommended_requires_confidence_floor():
    market = _market([_bucket("70-71°F", "range", lo=70, hi=71, best_ask=0.05)])
    fs = _forecast_set([60, 80])  # wide spread -> low P on any single 2-degree bucket
    report = evaluate_market(market, fs, floor=0.90, min_sources=2, min_sigma=1.5)
    o = report.outcomes[0]
    assert o.edge > 0  # cheap ask, positive edge
    assert o.p_win < 0.90
    assert o.recommended is False  # fails the confidence floor


def test_recommended_requires_min_sources():
    market = _market([_bucket("70-71°F", "range", lo=70, hi=71, best_ask=0.05)])
    fs = _forecast_set([70, 70, 71, 71], ok_sources=("nws",))  # only 1 source
    report = evaluate_market(market, fs, floor=0.50, min_sources=2, min_sigma=1.5)
    assert report.n_sources == 1
    assert report.outcomes[0].recommended is False


def test_bucket_without_ask_is_excluded_not_ranked():
    market = _market(
        [
            _bucket("70-71°F", "range", lo=70, hi=71, best_ask=None),
            _bucket("72-73°F", "range", lo=72, hi=73, best_ask=0.20),
        ]
    )
    fs = _forecast_set([70, 71, 72])
    report = evaluate_market(market, fs, floor=0.50, min_sources=2, min_sigma=1.5)
    assert [o.bucket_label for o in report.outcomes] == ["72-73°F"]
    assert report.excluded_no_ask == ["70-71°F"]


def test_evaluate_market_no_samples_yields_empty_outcomes():
    market = _market([_bucket("70-71°F", "range", lo=70, hi=71, best_ask=0.20)])
    fs = ForecastSet(
        target=build_target("NYC", "TMAX", date(2026, 5, 31)),
        samples=[],
        coverage=[SourceCoverage(source="nws", ok=False, n_samples=0, error="down")],
    )
    report = evaluate_market(market, fs, floor=0.50, min_sources=2, min_sigma=1.5)
    assert report.outcomes == []
    assert report.mu is None and report.sigma is None
    assert report.n_sources == 0


def test_evaluate_market_applies_calibration():
    market = _market([_bucket("70-71°F", "range", lo=70, hi=71, best_ask=0.20)])
    fs = _forecast_set([70, 70, 71, 71])  # raw fit mean 70.5
    cal = Calibration(
        station="KLGA", variable="TMAX", lead_time=1, bias=2.0, spread_scale=1.0, n_samples=50
    )
    raw = evaluate_market(market, fs, floor=0.5, min_sources=2, min_sigma=1.5)
    cald = evaluate_market(market, fs, floor=0.5, min_sources=2, min_sigma=1.5, calibration=cal)
    assert raw.calibrated is False
    assert cald.calibrated is True
    assert raw.mu is not None and cald.mu is not None
    assert cald.mu == raw.mu - 2.0  # bias shifts mu down


def test_evaluate_market_low_sample_calibration_falls_back():
    market = _market([_bucket("70-71°F", "range", lo=70, hi=71, best_ask=0.20)])
    fs = _forecast_set([70, 70, 71, 71])
    cal = Calibration(
        station="KLGA", variable="TMAX", lead_time=1, bias=2.0, spread_scale=1.0, n_samples=5
    )
    out = evaluate_market(market, fs, floor=0.5, min_sources=2, min_sigma=1.5, calibration=cal)
    assert out.calibrated is False
    assert out.mu == 70.5  # bias not applied below MIN_CAL_SAMPLES
    assert out.sigma is not None and out.sigma > 1.5  # widened fallback
