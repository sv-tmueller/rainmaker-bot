# tests/test_golden_e2e.py
import json
from datetime import date
from pathlib import Path

from rainmaker.config import CONFIDENCE_FLOOR, MIN_EDGE, MIN_SIGMA_F, MIN_SOURCES, build_target
from rainmaker.forecasts.base import ForecastSample, ForecastSet, SourceCoverage
from rainmaker.polymarket.markets import Bucket, Market, parse_market
from rainmaker.ranking.edge import evaluate_market
from rainmaker.report.render import Report, render_markdown

FIXTURES = Path(__file__).parent / "fixtures"


def _nyc_market():
    events = json.loads((FIXTURES / "polymarket_weather_events.json").read_text())
    return parse_market(next(e for e in events if e["id"] == "533147"))


def _forecast_set(target):
    # Controlled pool centered at 70.5F: mode is the 70-71 bucket.
    samples = [
        ForecastSample(
            source="nws",
            model="m",
            member=None,
            station="KLGA",
            variable="TMAX",
            target_date=target.local_date,
            lead_time_days=1,
            value_f=v,
            issued_at=None,
        )
        for v in (68, 69, 70, 71, 72, 73)
    ]
    return ForecastSet(
        target=target,
        samples=samples,
        coverage=[
            SourceCoverage(source="nws", ok=True, n_samples=6),
            SourceCoverage(source="open-meteo", ok=True, n_samples=6),
        ],
    )


def test_golden_pipeline_on_fixture_market():
    market = _nyc_market()
    fs = _forecast_set(market.target)
    report = evaluate_market(
        market,
        fs,
        floor=CONFIDENCE_FLOOR,
        min_sources=MIN_SOURCES,
        min_sigma=MIN_SIGMA_F,
        min_edge=MIN_EDGE,
    )

    # All 11 buckets had an ask in the fixture, so none are excluded.
    assert report.excluded_no_ask == []
    # One YES outcome per bucket, plus a NO outcome where the bucket has a NO ask.
    n_no = sum(1 for b in market.buckets if b.no_ask is not None)
    assert n_no > 0  # the feature is exercised
    yes_outcomes = [o for o in report.outcomes if o.side == "YES"]
    no_outcomes = [o for o in report.outcomes if o.side == "NO"]
    assert len(yes_outcomes) == 11
    assert len(no_outcomes) == n_no
    # P(win) over the full YES partition sums to ~1.
    assert abs(sum(o.p_win for o in yes_outcomes) - 1.0) < 1e-6
    # The mode bucket 70-71 is priced ~0.999 in the fixture, so no positive-edge
    # recommendation survives: an efficient market yields nothing, on either side.
    assert all(not o.recommended for o in report.outcomes)
    # Ranking is sorted by edge descending.
    edges = [o.edge for o in report.outcomes]
    assert edges == sorted(edges, reverse=True)

    # The report renders without error and names the station + settlement date.
    md = render_markdown(Report(run_date=date(2026, 5, 30), markets=[report]))
    assert "KLGA" in md
    assert "2026-05-30" in md


def _miami_tmin_market():
    # A complete partition of the real line: below | range | range | above.
    def bucket(label, kind, lo, hi, threshold, best_ask):
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

    return Market(
        id="tmin1",
        slug="lowest-temperature-in-miami",
        title="Lowest temperature in Miami on May 31?",
        target=build_target("Miami", "TMIN", date(2026, 5, 31)),
        buckets=[
            bucket("54°F or below", "below", None, None, 54, 0.10),
            bucket("55-56°F", "range", 55, 56, None, 0.35),
            bucket("57-58°F", "range", 57, 58, None, 0.35),
            bucket("59°F or higher", "above", None, None, 59, 0.10),
        ],
    )


def _tmin_forecast_set(target):
    # Pool centered at 56.5F so probability mass spreads across the partition.
    samples = [
        ForecastSample(
            source="nws",
            model="m",
            member=None,
            station="KMIA",
            variable="TMIN",
            target_date=target.local_date,
            lead_time_days=1,
            value_f=v,
            issued_at=None,
        )
        for v in (54, 55, 56, 57, 58, 59)
    ]
    return ForecastSet(
        target=target,
        samples=samples,
        coverage=[
            SourceCoverage(source="nws", ok=True, n_samples=6),
            SourceCoverage(source="open-meteo", ok=True, n_samples=6),
        ],
    )


def test_golden_pipeline_on_tmin_market():
    market = _miami_tmin_market()
    fs = _tmin_forecast_set(market.target)
    report = evaluate_market(
        market,
        fs,
        floor=CONFIDENCE_FLOOR,
        min_sources=MIN_SOURCES,
        min_sigma=MIN_SIGMA_F,
        min_edge=MIN_EDGE,
    )

    # Every bucket carries an ask, so none are excluded.
    assert report.excluded_no_ask == []
    # The YES buckets partition the real line, so P(win) sums to ~1.
    yes_outcomes = [o for o in report.outcomes if o.side == "YES"]
    assert len(yes_outcomes) == 4
    assert abs(sum(o.p_win for o in yes_outcomes) - 1.0) < 1e-6
    # Ranking is sorted by edge descending.
    edges = [o.edge for o in report.outcomes]
    assert edges == sorted(edges, reverse=True)

    md = render_markdown(Report(run_date=date(2026, 5, 30), markets=[report]))
    assert "KMIA" in md
    assert "2026-05-31" in md
