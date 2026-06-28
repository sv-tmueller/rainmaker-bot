import json
import math
from datetime import date
from pathlib import Path

import pytest

from rainmaker.config import (
    CONFIDENCE_FLOOR,
    MIN_EDGE,
    MIN_SIGMA_C,
    MIN_SIGMA_F,
    MIN_SOURCES,
    PRECIP_VAR_FLOOR,
    PrecipStation,
    Station,
    Target,
    build_target,
)
from rainmaker.domain import Bucket, Market, PrecipBracket, PrecipMonthlyMarket, PrecipTarget
from rainmaker.forecasts.base import ForecastSample, ForecastSet, SourceCoverage
from rainmaker.forecasts.precip import PrecipForecastSet
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
        station="KLGA", variable="TMAX", lead_time=1, bias=2.0, var_a=0.0, var_b=1.0, n_samples=50
    )
    raw = evaluate_market(market, fs, floor=0.5, min_sources=2, min_sigma=1.5, min_edge=0.0)
    cald = evaluate_market(
        market, fs, floor=0.5, min_sources=2, min_sigma=1.5, min_edge=0.0, calibration=cal
    )
    assert raw.calibrated == "uncalibrated"
    assert cald.calibrated == "full"
    assert raw.mu is not None and cald.mu is not None
    assert cald.mu == raw.mu - 2.0  # bias shifts mu down


def test_evaluate_market_low_sample_calibration_falls_back():
    market = _market([_bucket("70-71°F", "range", lo=70, hi=71, best_ask=0.20)])
    fs = _forecast_set([70, 70, 71, 71])
    cal = Calibration(
        station="KLGA", variable="TMAX", lead_time=1, bias=2.0, var_a=0.0, var_b=1.0, n_samples=5
    )
    out = evaluate_market(
        market, fs, floor=0.5, min_sources=2, min_sigma=1.5, min_edge=0.0, calibration=cal
    )
    assert out.calibrated == "uncalibrated"
    assert out.mu == 70.5  # bias not applied below MIN_CAL_SAMPLES
    assert out.sigma is not None and out.sigma > 1.5  # widened fallback


def test_evaluate_market_bias_only_calibration():
    """n in [10, 30) -> bias_only: mu is shifted, sigma is widened-raw (not EMOS)."""
    market = _market([_bucket("70-71°F", "range", lo=70, hi=71, best_ask=0.20)])
    fs = _forecast_set([70, 70, 71, 71])  # raw fit mean 70.5
    cal = Calibration(
        station="KLGA",
        variable="TMAX",
        lead_time=1,
        bias=2.0,
        var_a=100.0,  # pathological var_a: must not fire in bias_only
        var_b=5.0,
        n_samples=15,
    )
    raw = evaluate_market(market, fs, floor=0.5, min_sources=2, min_sigma=1.5, min_edge=0.0)
    cald = evaluate_market(
        market, fs, floor=0.5, min_sources=2, min_sigma=1.5, min_edge=0.0, calibration=cal
    )
    assert cald.calibrated == "bias_only"
    assert raw.mu is not None and cald.mu is not None
    # mu is shifted by bias
    assert cald.mu == pytest.approx(raw.mu - 2.0)
    # sigma must be widened-raw, not sqrt(100 + 5*g.sigma^2) which would be huge
    # raw.sigma uses fit_gaussian sigma (floored at 1.5); apply_calibration widens it to 1.875
    assert cald.sigma is not None and cald.sigma < 5.0  # not pathological EMOS value
    assert cald.sigma == pytest.approx(1.875)  # max(1.5*1.25, 1.5) = 1.875


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


def test_stale_source_ok_zero_samples_does_not_count_toward_min_sources():
    # A source that responded but had all samples filtered as stale is
    # recorded ok=True, n_samples=0 by aggregate.  It must not count as a
    # live source for the min-source gate in evaluate_market.
    market = _market([_bucket("70-71°F", "range", lo=70, hi=71, best_ask=0.05)])
    # Two coverage entries: one genuinely live, one stale (ok=True, n_samples=0).
    coverage = [
        SourceCoverage(source="nws", ok=True, n_samples=4),
        SourceCoverage(source="open-meteo", ok=True, n_samples=0),
    ]
    fs = ForecastSet(
        target=build_target("NYC", "TMAX", date(2026, 5, 31)),
        samples=[
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
            for v in [69, 70, 71, 72]
        ],
        coverage=coverage,
    )
    # min_sources=2: under the bug both ok=True entries count (n_sources=2, recommended True).
    # After the fix only the entry with n_samples>0 counts (n_sources=1, recommended False).
    report = evaluate_market(market, fs, floor=0.45, min_sources=2, min_sigma=1.5, min_edge=0.0)
    assert report.n_sources == 1
    assert report.outcomes[0].recommended is False


def test_stale_source_ok_zero_samples_does_not_count_for_precip_gate():
    # Same gate on the precip path: ok=True, n_samples=0 must not satisfy min-source count.
    market = _precip_market()
    fs = PrecipForecastSet(
        target=market.target,
        mean=2.5,
        var=0.6,
        coverage=[
            SourceCoverage(source="open-meteo", ok=True, n_samples=40),
            SourceCoverage(source="nws", ok=True, n_samples=0),
        ],
        n_observed_days=5,
        n_forecast_days=7,
        n_clim_days=18,
    )
    report = evaluate_precip_market(
        market,
        fs,
        floor=CONFIDENCE_FLOOR,
        min_sources=MIN_SOURCES,
        min_edge=MIN_EDGE,
        var_floor=PRECIP_VAR_FLOOR,
    )
    assert report.n_sources == 1
    assert not any(o.recommended for o in report.outcomes)


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
    assert report.calibrated == "uncalibrated"
    assert report.n_sources == 2
    assert report.mu == pytest.approx(2.5)
    assert report.sigma == pytest.approx(math.sqrt(0.6))
    yes = [o for o in report.outcomes if o.side == "YES"]
    assert len(yes) == 6  # one YES per inch bracket
    assert abs(sum(o.p_win for o in yes) - 1.0) < 1e-6  # partition sums to one
    edges = [o.edge for o in report.outcomes]
    assert edges == sorted(edges, reverse=True)  # ranked by edge desc


def test_evaluate_precip_market_emits_no_side_complement():
    # Every fixture bracket carries a YES bid (no_ask = 1 - bid), so a NO outcome
    # is emitted per bracket with p_win the complement of the matching YES.
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
    yes_by_label = {o.bucket_label: o.p_win for o in report.outcomes if o.side == "YES"}
    no = [o for o in report.outcomes if o.side == "NO"]
    assert len(no) == 6  # every bracket has a YES bid -> a NO ask
    for o in no:
        assert o.p_win == pytest.approx(1 - yes_by_label[o.bucket_label])
    assert report.excluded_no_ask == []


# ---------------------------------------------------------------------------
# Binding Celsius sigma-floor test (#177)
# ---------------------------------------------------------------------------

_LONDON_STATION = Station(
    city="London",
    icao="EGLC",
    name="London City Airport",
    lat=51.505,
    lon=0.055,
    timezone="Europe/London",
    wunderground_url="https://www.wunderground.com/history/daily/gb/london/EGLC",
    ghcnd_id=None,
    unit="C",
)


def _london_c_market() -> Market:
    """Synthetic 1°C ladder (16-18°C) for a London-style C market."""
    target = Target(station=_LONDON_STATION, variable="TMAX", local_date=date(2026, 6, 15))
    return Market(
        id="london_floor",
        slug="highest-temperature-london",
        title="Highest temperature in London on Jun 15?",
        target=target,
        buckets=[
            _bucket("15°C or below", "below", threshold=15, best_ask=0.30),
            _bucket("16°C", "range", lo=16, hi=16, best_ask=0.40),
            _bucket("17°C", "range", lo=17, hi=17, best_ask=0.20),
            _bucket("18°C or higher", "above", threshold=18, best_ask=0.10),
        ],
    )


def _tight_c_forecast_set(target: Target) -> ForecastSet:
    # Very tight pool: all samples at exactly 16C (= 60.8F).
    # Raw sigma will be ~0; the C floor must bind at MIN_SIGMA_C.
    f_value = 16 * 9 / 5 + 32  # 60.8F
    samples = [
        ForecastSample(
            source="nws",
            model="m",
            member=None,
            station="EGLC",
            variable="TMAX",
            target_date=target.local_date,
            lead_time_days=1,
            value_f=f_value,
            issued_at=None,
        )
        for _ in range(6)
    ]
    return ForecastSet(
        target=target,
        samples=samples,
        coverage=[
            SourceCoverage(source="nws", ok=True, n_samples=6),
            SourceCoverage(source="open-meteo", ok=True, n_samples=6),
        ],
    )


# ---------------------------------------------------------------------------
# Source-gate discriminator: intl markets relax to 1, US markets stay at 2 (#177)
# ---------------------------------------------------------------------------

_US_STATION_FOR_GATE = Station(
    city="NYC",
    icao="KLGA",
    name="LaGuardia Airport",
    lat=40.7792,
    lon=-73.8803,
    timezone="America/New_York",
    wunderground_url="https://www.wunderground.com/history/daily/us/ny/new-york-city/KLGA",
    ghcnd_id="USW00014732",
)

_INTL_STATION_FOR_GATE = Station(
    city="London",
    icao="EGLC",
    name="London City Airport",
    lat=51.505,
    lon=0.055,
    timezone="Europe/London",
    wunderground_url="https://www.wunderground.com/history/daily/gb/london/EGLC",
    ghcnd_id=None,
    unit="C",
)


def _gate_market_intl() -> Market:
    """Intl market with two buckets to exercise both YES and NO recommended paths.

    Forecast centered at 20C:
    - "18C or higher": p_win ~ 0.9+, best_ask=0.05 -> edge >> min_edge (YES side live)
    - "25C or higher": no best_ask so YES excluded; no_ask=0.05 -> p_no ~ 0.98,
      edge_no >> min_edge (NO side live). This forces the test to cover the NO branch.
    Both sides would be recommended for a US station; the intl gate must force both off.
    """
    target = Target(station=_INTL_STATION_FOR_GATE, variable="TMAX", local_date=date(2026, 6, 15))
    return Market(
        id="gate_intl",
        slug="gate-intl",
        title="Highest temperature in London on Jun 15?",
        target=target,
        buckets=[
            _bucket("18°C or higher", "above", threshold=18, best_ask=0.05),
            # no best_ask so the YES side is excluded; cheap no_ask means the NO side
            # would be recommended (p_no ~ 0.98, edge_no ~ 0.93) absent the intl gate.
            _bucket("25°C or higher", "above", threshold=25, no_ask=0.05),
        ],
    )


def _gate_market_us() -> Market:
    """US market: forecast centered at 70F, bucket "68F or higher", so p_win > CONFIDENCE_FLOOR
    and edge > MIN_EDGE. Only the source gate (n_sources=1 < min_sources=2) blocks it.
    """
    target = Target(station=_US_STATION_FOR_GATE, variable="TMAX", local_date=date(2026, 5, 31))
    return Market(
        id="gate_us",
        slug="gate-us",
        title="Highest temperature in NYC on May 31?",
        target=target,
        buckets=[_bucket("68°F or higher", "above", threshold=68, best_ask=0.05)],
    )


def _two_source_c(target: Target) -> ForecastSet:
    """Two live sources (open-meteo + NWS both ok), forecast centered at 20C (68F).

    Both coverage entries are ok=True, n_samples=5 so n_sources == 2 and the source
    gate passes. Use in tests where all other gates must pass so the uncalibratable
    guard is the only binding constraint.
    """
    samples = [
        ForecastSample(
            source="open-meteo",
            model="m",
            member=None,
            station=target.station.icao,
            variable="TMAX",
            target_date=target.local_date,
            lead_time_days=1,
            value_f=68.0 + offset,  # 20C +/- small F offsets
            issued_at=None,
        )
        for offset in (-2.0, -1.0, 0.0, 1.0, 2.0)
    ] + [
        ForecastSample(
            source="nws",
            model="m",
            member=None,
            station=target.station.icao,
            variable="TMAX",
            target_date=target.local_date,
            lead_time_days=1,
            value_f=68.0 + offset,
            issued_at=None,
        )
        for offset in (-2.0, -1.0, 0.0, 1.0, 2.0)
    ]
    return ForecastSet(
        target=target,
        samples=samples,
        coverage=[
            SourceCoverage(source="open-meteo", ok=True, n_samples=5),
            SourceCoverage(source="nws", ok=True, n_samples=5),
        ],
    )


def _single_source_f(target: Target) -> ForecastSet:
    """One live source (NWS absent), forecast centered at 70F (above the 68F threshold)."""
    samples = [
        ForecastSample(
            source="open-meteo",
            model="m",
            member=None,
            station=target.station.icao,
            variable="TMAX",
            target_date=target.local_date,
            lead_time_days=1,
            value_f=70.0 + offset,
            issued_at=None,
        )
        for offset in (-2.0, -1.0, 0.0, 1.0, 2.0)
    ]
    return ForecastSet(
        target=target,
        samples=samples,
        coverage=[
            SourceCoverage(source="open-meteo", ok=True, n_samples=5),
            SourceCoverage(source="nws", ok=False, n_samples=0, error="not available"),
        ],
    )


def test_intl_market_never_recommended() -> None:
    """An intl market (ghcnd_id=None) must never produce recommended=True, on any side.

    Even when all other gates pass (confidence floor, min_sources met,
    edge positive), the uncalibratable flag forces recommended off for both YES and
    NO outcomes. Advisory display is unaffected: outcomes list is non-empty and
    mu/sigma are set.
    """
    market = _gate_market_intl()
    assert market.target.station.ghcnd_id is None

    fs = _two_source_c(market.target)
    assert sum(1 for c in fs.coverage if c.ok and c.n_samples > 0) == 2

    report = evaluate_market(
        market,
        fs,
        floor=CONFIDENCE_FLOOR,
        min_sources=MIN_SOURCES,
        min_sigma=MIN_SIGMA_C,
        min_edge=MIN_EDGE,
    )
    # Advisory display must still render (intl markets stay in the report).
    assert report.outcomes, "outcomes must be non-empty so advisory still renders"
    assert report.mu is not None, "mu must be set so advisory still renders"
    # Recommended must be off for every outcome, both YES and NO sides.
    assert all(not o.recommended for o in report.outcomes), (
        f"intl market must not recommend any outcome; got {report.outcomes}"
    )


def test_us_market_single_source_blocked() -> None:
    """A US market (ghcnd_id set) with n_sources=1 must remain blocked.

    US markets always require min_sources=2; the relaxation must not apply.
    """
    market = _gate_market_us()
    assert market.target.station.ghcnd_id is not None

    fs = _single_source_f(market.target)
    assert sum(1 for c in fs.coverage if c.ok and c.n_samples > 0) == 1

    report = evaluate_market(
        market,
        fs,
        floor=CONFIDENCE_FLOOR,
        min_sources=MIN_SOURCES,
        min_sigma=MIN_SIGMA_F,
        min_edge=MIN_EDGE,
    )
    assert report.n_sources == 1
    yes = next(o for o in report.outcomes if o.side == "YES")
    # Floor and edge gates pass: the source gate is the only binding constraint.
    assert yes.p_win >= CONFIDENCE_FLOOR, f"p_win={yes.p_win} must clear CONFIDENCE_FLOOR"
    assert yes.edge >= MIN_EDGE, f"edge={yes.edge} must clear MIN_EDGE"
    # US 1-source must not be recommended despite passing the other two gates.
    assert yes.recommended is False


def test_ghcnd_none_discriminator_cannot_reach_us_stations() -> None:
    """Every station in STATIONS and KALSHI_STATIONS has a non-None ghcnd_id.

    This invariant is what makes the ghcnd_id=None relaxation US-safe:
    no US station can ever trigger the intl gate.
    """
    from rainmaker.config import KALSHI_STATIONS, STATIONS

    for city, station in STATIONS.items():
        assert station.ghcnd_id is not None, f"STATIONS[{city!r}].ghcnd_id must not be None"
    for city, station in KALSHI_STATIONS.items():
        assert station.ghcnd_id is not None, f"KALSHI_STATIONS[{city!r}].ghcnd_id must not be None"


def test_c_floor_binds_at_min_sigma_c() -> None:
    """A C market with near-zero raw sigma must floor at MIN_SIGMA_C, not MIN_SIGMA_F.

    This test would fail if cli.py passed MIN_SIGMA_F for a C market:
    MIN_SIGMA_F (~1.5) >> MIN_SIGMA_C (~0.833), so using the F floor would
    over-widen the C distribution and produce a different sigma.
    """
    market = _london_c_market()
    assert market.target.station.unit == "C"

    fs = _tight_c_forecast_set(market.target)
    report = evaluate_market(
        market,
        fs,
        floor=CONFIDENCE_FLOOR,
        min_sources=MIN_SOURCES,
        min_sigma=MIN_SIGMA_C,  # the wiring cli.py must choose for C markets
        min_edge=MIN_EDGE,
    )

    assert report.sigma is not None
    # The C floor must bind.
    assert report.sigma == pytest.approx(MIN_SIGMA_C, abs=1e-6)
    # And the floored value must be distinctly less than the F floor,
    # proving this test would fail if the wrong floor were passed.
    assert report.sigma < MIN_SIGMA_F


# ---------------------------------------------------------------------------
# Per-side floor: lower bar for NO (longshot) regime, higher for YES (#85)
# ---------------------------------------------------------------------------


def test_per_side_floor_no_recommended_yes_blocked():
    """A NO bet whose p_no clears floor_no but not floor_yes must be recommended;
    a YES bet at the same probability must be blocked.

    This is the gate-binding property. A flat-floor mutation (floor_no = floor_yes)
    collapses the asymmetry: the NO bet flips to recommended=False.

    Concrete wiring: floor_no=0.80, floor_yes=0.90, bucket "72-73F" with forecast
    centered at 70F, sigma=1.5F (floored). p_win(YES) for 72-73F ~ 0.149;
    p_no ~ 0.851. This clears floor_no=0.80 but not floor_yes=0.90.
    """
    market = _market(
        [
            _bucket(
                "72-73\u00b0F",
                "range",
                lo=72,
                hi=73,
                best_ask=0.20,
                no_ask=0.15,
            )
        ]
    )
    # Forecast centered at 70F, sigma floored to 1.5F.
    # p_win(YES) for 72-73 ~ 0.149 (Z=1.67); p_no ~ 0.851.
    fs = _forecast_set([69, 70, 70, 71, 71])

    # Per-side floor: floor_no=0.80, floor_yes=0.90, min_sources=2.
    report = evaluate_market(
        market,
        fs,
        floor=0.90,
        floor_no=0.80,
        min_sources=2,
        min_sigma=1.5,
        min_edge=0.05,
    )
    sides = {o.side: o for o in report.outcomes}
    yes, no = sides["YES"], sides["NO"]

    # Gate-binding assertions: prove the test is not vacuous.
    assert yes.p_win < 0.90, f"YES p_win={yes.p_win} should be below floor_yes=0.90"
    assert no.p_win > 0.80, f"NO p_win={no.p_win} should clear floor_no=0.80"
    assert no.p_win < 0.90, f"NO p_win={no.p_win} should be below floor_yes=0.90"
    assert no.edge >= 0.05, f"NO edge={no.edge} must clear min_edge"

    assert yes.recommended is False, "YES must be blocked (p_win < floor_yes=0.90)"
    assert no.recommended is True, "NO must be recommended (p_win > floor_no=0.80)"

    # Flat-floor mutation: floor_no = floor_yes = 0.90 collapses the asymmetry.
    report_flat = evaluate_market(
        market,
        fs,
        floor=0.90,
        floor_no=0.90,
        min_sources=2,
        min_sigma=1.5,
        min_edge=0.05,
    )
    no_flat = next(o for o in report_flat.outcomes if o.side == "NO")
    assert no_flat.recommended is False, (
        "NO must flip to not-recommended when floor_no = floor_yes = 0.90 (flat-floor mutation)"
    )


def test_precip_per_side_floor_no_recommended_yes_blocked():
    """A precip NO bet clearing floor_no but not floor_yes must be recommended;
    a YES at the same probability must be blocked. Gate-binding property for
    the precip path (evaluate_precip_market).

    Concrete wiring: mean=2.5 inches, var=0.6 in^2 (gamma fit); bracket "3-4 inches"
    gives p_yes ~ 0.199, p_no ~ 0.801. With floor_yes=0.90, floor_no=0.80,
    and no_ask=0.70, edge_no ~ 0.10 > min_edge=0.05.
    The NO bet clears floor_no=0.80; YES does not clear floor_yes=0.90.

    Flat-floor mutation (floor_no = floor_yes = 0.90) blocks the NO bet.
    """
    station = PrecipStation(
        city="Test City",
        resolution_name="Test Station",
        name="Test Station",
        lat=40.0,
        lon=-74.0,
        timezone="America/New_York",
        ghcnd_id="USW00094728",
    )
    target = PrecipTarget(
        station=station,
        variable="PRCP",
        year=2026,
        month=6,
        settlement_date=date(2026, 6, 30),
    )
    # One bracket: "3-4 inches" -> p_yes ~ 0.199, p_no ~ 0.801
    # no_ask=0.70: edge_no = 0.801 - 0.70 ~ 0.10, clears min_edge=0.05
    bracket = PrecipBracket(
        label='3-4"',
        kind="range",
        lo=3.0,
        hi=4.0,
        threshold=None,
        yes_token_id="tok1",
        best_ask=0.25,
        best_bid=0.30,
        yes_price=0.25,
        no_ask=0.70,
    )
    market = PrecipMonthlyMarket(
        id="test-precip-per-side",
        slug="test-precip-per-side",
        title="Test Precip Per-Side",
        target=target,
        buckets=[bracket],
    )
    fs = PrecipForecastSet(
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

    report = evaluate_precip_market(
        market,
        fs,
        floor=0.90,
        floor_no=0.80,
        min_sources=2,
        min_edge=0.05,
        var_floor=PRECIP_VAR_FLOOR,
    )
    sides = {o.side: o for o in report.outcomes}
    yes, no = sides["YES"], sides["NO"]

    # Gate-binding assertions.
    assert yes.p_win < 0.90, f"YES p_win={yes.p_win} should be below floor_yes=0.90"
    assert no.p_win > 0.80, f"NO p_win={no.p_win} should clear floor_no=0.80"
    assert no.p_win < 0.90, f"NO p_win={no.p_win} should be below floor_yes=0.90"
    assert no.edge >= 0.05, f"NO edge={no.edge} must clear min_edge"

    assert yes.recommended is False, "YES must be blocked (p_win < floor_yes=0.90)"
    assert no.recommended is True, "NO must be recommended (p_win > floor_no=0.80)"

    # Flat-floor mutation: floor_no = floor_yes = 0.90 collapses the asymmetry.
    report_flat = evaluate_precip_market(
        market,
        fs,
        floor=0.90,
        floor_no=0.90,
        min_sources=2,
        min_edge=0.05,
        var_floor=PRECIP_VAR_FLOOR,
    )
    no_flat = next(o for o in report_flat.outcomes if o.side == "NO")
    assert no_flat.recommended is False, (
        "NO must flip to not-recommended when floor_no = floor_yes = 0.90 (flat-floor mutation)"
    )
