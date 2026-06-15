# tests/test_golden_e2e.py
import json
from datetime import date
from pathlib import Path

from rainmaker.config import (
    CONFIDENCE_FLOOR,
    MIN_EDGE,
    MIN_SIGMA_C,
    MIN_SIGMA_F,
    MIN_SOURCES,
    PRECIP_VAR_FLOOR,
    Station,
    Target,
    build_target,
)
from rainmaker.domain import Bucket, Market
from rainmaker.forecasts.base import ForecastSample, ForecastSet, SourceCoverage
from rainmaker.forecasts.precip import PrecipForecastSet
from rainmaker.polymarket.markets import parse_market
from rainmaker.polymarket.precip_markets import parse_precip_event
from rainmaker.ranking.edge import evaluate_market, evaluate_precip_market
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


def _nyc_precip_market():
    event = json.loads((FIXTURES / "polymarket_precip_monthly_nyc.json").read_text())
    return parse_precip_event(event)


def _precip_forecast_set(target):
    # A tight monthly total centered at 2.5in: the mode is the 2-3" bracket.
    return PrecipForecastSet(
        target=target,
        mean=2.5,
        var=0.6,
        coverage=[
            SourceCoverage(source="open-meteo", ok=True, n_samples=40),
            SourceCoverage(source="nws", ok=True, n_samples=3),
        ],
        n_observed_days=5,
        n_forecast_days=7,
        n_clim_days=18,
    )


def test_golden_precip_pipeline_on_fixture_market():
    market = _nyc_precip_market()
    fs = _precip_forecast_set(market.target)
    report = evaluate_precip_market(
        market,
        fs,
        floor=CONFIDENCE_FLOOR,
        min_sources=MIN_SOURCES,
        min_edge=MIN_EDGE,
        var_floor=PRECIP_VAR_FLOOR,
    )

    # Every bracket has an ask in the fixture, so none are excluded.
    assert report.excluded_no_ask == []
    yes_outcomes = [o for o in report.outcomes if o.side == "YES"]
    assert len(yes_outcomes) == 6
    # The YES brackets partition the inch line, so P(win) sums to ~1.
    assert abs(sum(o.p_win for o in yes_outcomes) - 1.0) < 1e-6
    # The mode bracket 2-3" carries the most probability.
    mode = max(yes_outcomes, key=lambda o: o.p_win)
    assert mode.bucket_label == '2-3"'
    # Ranking is sorted by edge descending.
    edges = [o.edge for o in report.outcomes]
    assert edges == sorted(edges, reverse=True)

    md = render_markdown(Report(run_date=date(2026, 6, 6), markets=[report]))
    assert "Central Park NY" in md  # the resolution station, not the temperature ICAO
    assert "2026-06-30" in md  # the month's settlement date
    assert "in (uncalibrated)" in md  # the inch unit label


# ---------------------------------------------------------------------------
# Celsius-core golden e2e (#167): synthetic Jeddah-style high-temp C market
# ---------------------------------------------------------------------------

_JEDDAH_STATION = Station(
    city="Jeddah",
    icao="OEJN",
    name="King Abdulaziz International Airport",
    lat=21.67,
    lon=39.16,
    timezone="Asia/Riyadh",
    wunderground_url="https://example.com/jeddah",
    ghcnd_id=None,
    unit="C",
)


def _jeddah_c_market() -> Market:
    """Synthetic 1-degree-C ladder (37-41C) for a Jeddah-style high-temp market."""

    def bucket(label: str, kind: str, lo, hi, threshold, best_ask: float) -> Bucket:
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

    target = Target(
        station=_JEDDAH_STATION,
        variable="TMAX",
        local_date=date(2026, 7, 1),
    )
    return Market(
        id="jeddah1",
        slug="highest-temperature-jeddah",
        title="Highest temperature in Jeddah on Jul 1?",
        target=target,
        buckets=[
            bucket("36C or below", "below", None, None, 36, 0.10),
            bucket("37C", "range", 37, 37, None, 0.15),
            bucket("38C", "range", 38, 38, None, 0.40),
            bucket("39C", "range", 39, 39, None, 0.25),
            bucket("40C or higher", "above", None, None, 40, 0.10),
        ],
    )


def _jeddah_forecast_set(target: Target) -> ForecastSet:
    # Forecast values are always in F (the sources only produce F).
    # 38C = 100.4F; pool centered there so the 38C bucket is the mode.
    f_center = 38 * 9 / 5 + 32  # 100.4F
    samples = [
        ForecastSample(
            source="nws",
            model="m",
            member=None,
            station="OEJN",
            variable="TMAX",
            target_date=target.local_date,
            lead_time_days=1,
            value_f=f_center + offset,
            issued_at=None,
        )
        for offset in (-3.6, -1.8, 0.0, 1.8, 3.6)  # 2C spread in F-space
    ]
    return ForecastSet(
        target=target,
        samples=samples,
        coverage=[
            SourceCoverage(source="nws", ok=True, n_samples=5),
            SourceCoverage(source="open-meteo", ok=True, n_samples=5),
        ],
    )


def test_golden_pipeline_on_celsius_market():
    """C-unit market: forecast -> probability (whole-C rounding) -> edge -> report."""
    market = _jeddah_c_market()
    assert market.target.station.unit == "C"

    fs = _jeddah_forecast_set(market.target)
    report = evaluate_market(
        market,
        fs,
        floor=CONFIDENCE_FLOOR,
        min_sources=MIN_SOURCES,
        min_sigma=MIN_SIGMA_C,
        min_edge=MIN_EDGE,
    )

    # All buckets carry an ask, so none are excluded.
    assert report.excluded_no_ask == []
    yes_outcomes = [o for o in report.outcomes if o.side == "YES"]
    assert len(yes_outcomes) == 5
    # YES partition sums to ~1.
    assert abs(sum(o.p_win for o in yes_outcomes) - 1.0) < 1e-6
    # Mode bucket is 38C (pool centered there).
    mode = max(yes_outcomes, key=lambda o: o.p_win)
    assert mode.bucket_label == "38C"
    # Ranking is sorted by edge descending.
    edges = [o.edge for o in report.outcomes]
    assert edges == sorted(edges, reverse=True)

    # report.mu and report.sigma are in C (not F).
    assert report.mu is not None and report.sigma is not None
    assert 36 < report.mu < 41, f"mu should be near 38C, got {report.mu}"
    assert report.sigma < 5, f"sigma in C should be < 5, got {report.sigma}"

    # The render labels the unit as C, not F.
    md = render_markdown(Report(run_date=date(2026, 7, 1), markets=[report]))
    assert "C (uncalibrated)" in md
    assert "F (uncalibrated)" not in md
    assert "OEJN" in md
