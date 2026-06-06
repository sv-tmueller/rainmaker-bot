import json
import math
from datetime import date
from pathlib import Path

import pytest

from rainmaker.config import CONFIDENCE_FLOOR, MIN_EDGE, MIN_SOURCES, PRECIP_VAR_FLOOR, build_target
from rainmaker.forecasts.base import ForecastSample, ForecastSet, SourceCoverage
from rainmaker.forecasts.precip import PrecipForecastSet
from rainmaker.polymarket.markets import Bucket, Market
from rainmaker.polymarket.precip_markets import parse_precip_event
from rainmaker.probability.calibration import Calibration
from rainmaker.ranking.edge import MarketReport, evaluate_market, evaluate_precip_market

FIXTURES = Path(__file__).parent / "fixtures"


def _bucket(label, kind, *, lo=None, hi=None, threshold=None, best_ask=None, no_ask=None) -> Bucket:
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
        no_ask=no_ask,
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
    report = evaluate_market(market, fs, floor=0.45, min_sources=2, min_sigma=1.5, min_edge=0.0)
    assert isinstance(report, MarketReport)
    assert report.n_sources == 2
    # sorted by edge desc
    assert [o.bucket_label for o in report.outcomes] == ["70-71°F", "72-73°F"]
    top = report.outcomes[0]
    assert top.edge > 0
    assert top.recommended is True


def test_evaluate_market_emits_recommended_no_bet():
    # The market overprices an unlikely bucket; our forecast says it almost never
    # settles, so selling it (NO) is the good bet while buying it (YES) is not.
    market = _market([_bucket("80-81°F", "range", lo=80, hi=81, best_ask=0.30, no_ask=0.70)])
    fs = _forecast_set([69, 70, 71])  # mean ~70, far from 80-81
    report = evaluate_market(market, fs, floor=0.90, min_sources=2, min_sigma=1.5, min_edge=0.05)
    sides = {o.side: o for o in report.outcomes}
    assert set(sides) == {"YES", "NO"}
    yes, no = sides["YES"], sides["NO"]
    assert no.p_win == pytest.approx(1 - yes.p_win)
    assert no.best_ask == 0.70
    assert no.edge == pytest.approx(no.p_win - 0.70)
    assert yes.recommended is False  # buying the longshot loses
    assert no.recommended is True  # selling it clears the floor and the edge gate


def test_no_bet_emitted_even_when_yes_ask_absent():
    # A bucket with no YES ask but a NO ask (a bids-only YES book) still has a
    # fillable NO bet; it must not be dropped with the excluded YES side.
    market = _market([_bucket("80-81°F", "range", lo=80, hi=81, best_ask=None, no_ask=0.70)])
    fs = _forecast_set([69, 70, 71])
    report = evaluate_market(market, fs, floor=0.90, min_sources=2, min_sigma=1.5, min_edge=0.05)
    assert report.excluded_no_ask == ["80-81°F"]  # YES has no ask
    assert [o.side for o in report.outcomes] == ["NO"]  # but the NO bet survives
    assert report.outcomes[0].recommended is True


def test_no_bet_skipped_without_no_ask():
    market = _market([_bucket("70-71°F", "range", lo=70, hi=71, best_ask=0.40)])  # no_ask None
    fs = _forecast_set([69, 70, 71, 72])
    report = evaluate_market(market, fs, floor=0.45, min_sources=2, min_sigma=1.5, min_edge=0.0)
    assert [o.side for o in report.outcomes] == ["YES"]


def test_recommended_requires_confidence_floor():
    market = _market([_bucket("70-71°F", "range", lo=70, hi=71, best_ask=0.05)])
    fs = _forecast_set([60, 80])  # wide spread -> low P on any single 2-degree bucket
    report = evaluate_market(market, fs, floor=0.90, min_sources=2, min_sigma=1.5, min_edge=0.0)
    o = report.outcomes[0]
    assert o.edge > 0  # cheap ask, positive edge
    assert o.p_win < 0.90
    assert o.recommended is False  # fails the confidence floor


def test_default_floor_relaxed_to_080_recommends_high_080s():
    # Locks the #58 relaxation: a bet whose p_win lands in [0.80, 0.90) with edge
    # over the bar is recommended at the default floor but not at the old 0.90.
    assert CONFIDENCE_FLOOR == 0.80
    market = _market([_bucket("72°F or below", "below", threshold=72, best_ask=0.70)])
    fs = _forecast_set([70, 71, 72])  # mean 71, sigma floored to 1.5 -> p_win ~0.84

    def yes(floor: float):
        report = evaluate_market(
            market, fs, floor=floor, min_sources=2, min_sigma=1.5, min_edge=0.05
        )
        return next(o for o in report.outcomes if o.side == "YES")

    o = yes(CONFIDENCE_FLOOR)
    assert 0.80 <= o.p_win < 0.90
    assert o.edge >= 0.05
    assert o.recommended is True  # clears the relaxed 0.80 floor
    assert yes(0.90).recommended is False  # would have been blocked at 0.90


def test_recommended_requires_min_sources():
    market = _market([_bucket("70-71°F", "range", lo=70, hi=71, best_ask=0.05)])
    fs = _forecast_set([70, 70, 71, 71], ok_sources=("nws",))  # only 1 source
    report = evaluate_market(market, fs, floor=0.50, min_sources=2, min_sigma=1.5, min_edge=0.0)
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
    report = evaluate_market(market, fs, floor=0.50, min_sources=2, min_sigma=1.5, min_edge=0.0)
    assert [o.bucket_label for o in report.outcomes] == ["72-73°F"]
    assert report.excluded_no_ask == ["70-71°F"]


def test_evaluate_market_no_samples_yields_empty_outcomes():
    market = _market([_bucket("70-71°F", "range", lo=70, hi=71, best_ask=0.20)])
    fs = ForecastSet(
        target=build_target("NYC", "TMAX", date(2026, 5, 31)),
        samples=[],
        coverage=[SourceCoverage(source="nws", ok=False, n_samples=0, error="down")],
    )
    report = evaluate_market(market, fs, floor=0.50, min_sources=2, min_sigma=1.5, min_edge=0.0)
    assert report.outcomes == []
    assert report.mu is None and report.sigma is None
    assert report.n_sources == 0


def test_evaluate_market_applies_calibration():
    market = _market([_bucket("70-71°F", "range", lo=70, hi=71, best_ask=0.20)])
    fs = _forecast_set([70, 70, 71, 71])  # raw fit mean 70.5
    cal = Calibration(
        station="KLGA", variable="TMAX", lead_time=1, bias=2.0, spread_scale=1.0, n_samples=50
    )
    raw = evaluate_market(market, fs, floor=0.5, min_sources=2, min_sigma=1.5, min_edge=0.0)
    cald = evaluate_market(
        market, fs, floor=0.5, min_sources=2, min_sigma=1.5, min_edge=0.0, calibration=cal
    )
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
    out = evaluate_market(
        market, fs, floor=0.5, min_sources=2, min_sigma=1.5, min_edge=0.0, calibration=cal
    )
    assert out.calibrated is False
    assert out.mu == 70.5  # bias not applied below MIN_CAL_SAMPLES
    assert out.sigma is not None and out.sigma > 1.5  # widened fallback


def test_recommended_requires_min_edge():
    # Near-certain bucket priced at 0.99: positive but tiny edge.
    market = _market([_bucket("69°F or below", "below", threshold=69, best_ask=0.99)])
    fs = _forecast_set([60, 60, 60, 60])  # far below threshold -> p_win ~1.0
    report = evaluate_market(market, fs, floor=0.90, min_sources=2, min_sigma=1.5, min_edge=0.05)
    o = report.outcomes[0]
    assert o.p_win > 0.99
    assert 0 < o.edge < 0.05
    assert o.recommended is False  # passes floor and sources, fails min edge


def test_recommended_passes_min_edge():
    # Same near-certain bucket priced at 0.90: edge ~0.10 clears the threshold.
    market = _market([_bucket("69°F or below", "below", threshold=69, best_ask=0.90)])
    fs = _forecast_set([60, 60, 60, 60])
    report = evaluate_market(market, fs, floor=0.90, min_sources=2, min_sigma=1.5, min_edge=0.05)
    o = report.outcomes[0]
    assert o.edge >= 0.05
    assert o.recommended is True


def _precip_market():
    return parse_precip_event(
        json.loads((FIXTURES / "polymarket_precip_monthly_nyc.json").read_text())
    )


def _precip_forecast_set(target, *, mean=2.5, var=0.6):
    return PrecipForecastSet(
        target=target,
        mean=mean,
        var=var,
        coverage=[
            SourceCoverage(source="open-meteo", ok=True, n_samples=40),
            SourceCoverage(source="nws", ok=True, n_samples=3),
        ],
        n_observed_days=5,
        n_forecast_days=7,
        n_clim_days=18,
    )


def test_evaluate_precip_market_ranks_brackets():
    market = _precip_market()
    fs = _precip_forecast_set(market.target)
    report = evaluate_precip_market(
        market,
        fs,
        floor=CONFIDENCE_FLOOR,
        min_sources=MIN_SOURCES,
        min_edge=MIN_EDGE,
        var_floor=PRECIP_VAR_FLOOR,
    )
    assert isinstance(report, MarketReport)
    assert report.variable == "PRCP"
    assert report.station == "Central Park NY"
    assert report.settlement_date == date(2026, 6, 30)
    assert report.calibrated is False
    assert report.n_sources == 2
    assert report.mu == pytest.approx(2.5)
    assert report.sigma == pytest.approx(math.sqrt(0.6))
    yes = [o for o in report.outcomes if o.side == "YES"]
    assert len(yes) == 6  # one YES per inch bracket
    assert abs(sum(o.p_win for o in yes) - 1.0) < 1e-6  # partition sums to one
    edges = [o.edge for o in report.outcomes]
    assert edges == sorted(edges, reverse=True)  # ranked by edge desc
