import json
import math
from datetime import date
from pathlib import Path

import pytest

from rainmaker.config import (
    CONFIDENCE_FLOOR,
    MAX_EDGE,
    MIN_EDGE,
    MIN_SIGMA_C,
    MIN_SIGMA_F,
    MIN_SOURCES,
    PRECIP_STATIONS,
    PRECIP_VAR_FLOOR,
    STATION_EDGE_DELTA,
    STATION_POLICIES,
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
from rainmaker.probability.precip_distribution import fit_gamma
from rainmaker.probability.precip_outcomes import bracket_probability
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


def _cal(
    n_samples: int, *, bias: float = 0.0, var_a: float = 0.0, var_b: float = 1.0
) -> Calibration:
    return Calibration(
        station="KLGA",
        variable="TMAX",
        lead_time=1,
        bias=bias,
        var_a=var_a,
        var_b=var_b,
        n_samples=n_samples,
    )


def _full_cal() -> Calibration:
    """Identity full calibration (n=30, bias=0, var_a=0, var_b=1).

    apply_calibration then returns mu and sigma unchanged (mu - 0 = mu;
    sqrt(0 + 1*sigma^2) = sigma), so passing this isolates the recommended
    gate without moving any numeric expectation in the tests that use it.
    """
    return _cal(30)


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
    report = evaluate_market(
        market, fs, floor=0.45, min_sources=2, min_sigma=1.5, min_edge=0.0, calibration=_full_cal()
    )
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
    report = evaluate_market(
        market, fs, floor=0.90, min_sources=2, min_sigma=1.5, min_edge=0.05, calibration=_full_cal()
    )
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
    report = evaluate_market(
        market, fs, floor=0.90, min_sources=2, min_sigma=1.5, min_edge=0.05, calibration=_full_cal()
    )
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
    report = evaluate_market(
        market,
        fs,
        floor=0.90,
        min_sources=2,
        min_sigma=1.5,
        min_edge=0.0,
        calibration=_full_cal(),  # isolate the confidence-floor gate from the calibration gate
    )
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
            market,
            fs,
            floor=floor,
            min_sources=2,
            min_sigma=1.5,
            min_edge=0.05,
            calibration=_full_cal(),
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
    report = evaluate_market(
        market,
        fs,
        floor=0.50,
        min_sources=2,
        min_sigma=1.5,
        min_edge=0.0,
        calibration=_full_cal(),  # isolate the source gate from the calibration gate
    )
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


# ---------------------------------------------------------------------------
# MarketReport.df (#292): threads the Student-t predictive family through
# to the report so record.py can persist it. Only the full-EMOS regime
# carries a fitted df; every other path stays Gaussian (df=None).
# ---------------------------------------------------------------------------


def test_evaluate_market_df_threads_from_full_calibration():
    market = _market([_bucket("70-71°F", "range", lo=70, hi=71, best_ask=0.20)])
    fs = _forecast_set([70, 70, 71, 71])
    cal = Calibration(
        station="KLGA",
        variable="TMIN",
        lead_time=1,
        bias=0.0,
        var_a=0.0,
        var_b=1.0,
        df=6.5,
        n_samples=50,
    )
    report = evaluate_market(
        market, fs, floor=0.5, min_sources=2, min_sigma=1.5, min_edge=0.0, calibration=cal
    )
    assert report.calibrated == "full"
    assert report.df == 6.5


def test_evaluate_market_df_stays_none_on_uncalibrated():
    market = _market([_bucket("70-71°F", "range", lo=70, hi=71, best_ask=0.20)])
    fs = _forecast_set([70, 70, 71, 71])
    cal = Calibration(
        station="KLGA",
        variable="TMIN",
        lead_time=1,
        bias=0.0,
        var_a=0.0,
        var_b=1.0,
        df=6.5,
        n_samples=5,
    )
    report = evaluate_market(
        market, fs, floor=0.5, min_sources=2, min_sigma=1.5, min_edge=0.0, calibration=cal
    )
    assert report.calibrated == "uncalibrated"
    assert report.df is None


def test_evaluate_market_df_stays_none_on_bias_only():
    market = _market([_bucket("70-71°F", "range", lo=70, hi=71, best_ask=0.20)])
    fs = _forecast_set([70, 70, 71, 71])
    cal = Calibration(
        station="KLGA",
        variable="TMIN",
        lead_time=1,
        bias=0.0,
        var_a=0.0,
        var_b=1.0,
        df=6.5,
        n_samples=15,
    )
    report = evaluate_market(
        market, fs, floor=0.5, min_sources=2, min_sigma=1.5, min_edge=0.0, calibration=cal
    )
    assert report.calibrated == "bias_only"
    assert report.df is None


def test_evaluate_market_df_stays_none_on_no_calibration():
    market = _market([_bucket("70-71°F", "range", lo=70, hi=71, best_ask=0.20)])
    fs = _forecast_set([70, 70, 71, 71])
    report = evaluate_market(market, fs, floor=0.5, min_sources=2, min_sigma=1.5, min_edge=0.0)
    assert report.calibrated == "uncalibrated"
    assert report.df is None


def test_evaluate_market_df_stays_none_on_no_samples():
    market = _market([_bucket("70-71°F", "range", lo=70, hi=71, best_ask=0.20)])
    fs = ForecastSet(
        target=build_target("NYC", "TMAX", date(2026, 5, 31)),
        samples=[],
        coverage=[SourceCoverage(source="nws", ok=False, n_samples=0, error="down")],
    )
    report = evaluate_market(market, fs, floor=0.50, min_sources=2, min_sigma=1.5, min_edge=0.0)
    assert report.df is None


def test_evaluate_precip_market_df_stays_none():
    market = _precip_market()
    fs = _precip_forecast_set(market.target)
    report = evaluate_precip_market(
        market, fs, floor=0.5, min_sources=1, min_edge=0.0, var_floor=PRECIP_VAR_FLOOR
    )
    assert report.df is None


# ---------------------------------------------------------------------------
# Recommendation gate requires an applied full calibration (#225)
# ---------------------------------------------------------------------------


def _cal_gate_market() -> Market:
    """A market with a YES-clearing bucket and a NO-clearing bucket.

    Forecast centered at 70F, sigma floored to 1.5F.
    - "68F or higher": p_win ~0.91, best_ask=0.05 -> clears floor/sources/edge on YES.
    - "80F or higher": no best_ask (YES excluded); no_ask=0.05, p_no ~1.0 ->
      clears floor/sources/edge on NO.
    Both sides would be recommended once calibrated="full"; the gate under test
    is calibration, not floor/sources/edge (already cleared).
    """
    return _market(
        [
            _bucket("68°F or higher", "above", threshold=68, best_ask=0.05),
            _bucket("80°F or higher", "above", threshold=80, no_ask=0.05),
        ]
    )


def _cal_gate_forecast_set() -> ForecastSet:
    return _forecast_set([69, 70, 71])  # mean 70, sigma floored to 1.5


@pytest.mark.parametrize(
    "calibration",
    [
        pytest.param(None, id="no-calibration"),
        pytest.param(_cal(5), id="uncalibrated-low-n"),
        pytest.param(_cal(15), id="bias-only"),
    ],
)
def test_recommended_false_without_full_calibration(calibration):
    market = _cal_gate_market()
    fs = _cal_gate_forecast_set()
    report = evaluate_market(
        market,
        fs,
        floor=0.80,
        min_sources=2,
        min_sigma=1.5,
        min_edge=0.05,
        calibration=calibration,
    )
    assert report.calibrated != "full"
    yes = next(o for o in report.outcomes if o.side == "YES")
    no = next(o for o in report.outcomes if o.side == "NO")
    # Gate-binding: both sides clear floor, sources, and edge on their own merits.
    assert yes.p_win >= 0.80 and yes.edge >= 0.05
    assert no.p_win >= 0.80 and no.edge >= 0.05
    assert yes.recommended is False
    assert no.recommended is False


def test_recommended_true_with_applied_full_calibration():
    market = _cal_gate_market()
    fs = _cal_gate_forecast_set()
    report = evaluate_market(
        market,
        fs,
        floor=0.80,
        min_sources=2,
        min_sigma=1.5,
        min_edge=0.05,
        calibration=_full_cal(),
    )
    assert report.calibrated == "full"
    yes = next(o for o in report.outcomes if o.side == "YES")
    no = next(o for o in report.outcomes if o.side == "NO")
    assert yes.recommended is True
    assert no.recommended is True


def test_recommended_requires_min_edge():
    # Near-certain bucket priced at 0.99: positive but tiny edge.
    market = _market([_bucket("69°F or below", "below", threshold=69, best_ask=0.99)])
    fs = _forecast_set([60, 60, 60, 60])  # far below threshold -> p_win ~1.0
    report = evaluate_market(
        market,
        fs,
        floor=0.90,
        min_sources=2,
        min_sigma=1.5,
        min_edge=0.05,
        calibration=_full_cal(),  # isolate the edge gate from the calibration gate
    )
    o = report.outcomes[0]
    assert o.p_win > 0.99
    assert 0 < o.edge < 0.05
    assert o.recommended is False  # passes floor and sources, fails min edge


def test_recommended_passes_min_edge():
    # Same near-certain bucket priced at 0.90: edge ~0.10 clears the threshold.
    market = _market([_bucket("69°F or below", "below", threshold=69, best_ask=0.90)])
    fs = _forecast_set([60, 60, 60, 60])
    report = evaluate_market(
        market, fs, floor=0.90, min_sources=2, min_sigma=1.5, min_edge=0.05, calibration=_full_cal()
    )
    o = report.outcomes[0]
    assert o.edge >= 0.05
    assert o.recommended is True


# ---------------------------------------------------------------------------
# max_edge cap (#356): an inclusive upper bound on edge, symmetric with the
# min_edge floor. Default None preserves every existing call site byte-for-byte.
# ---------------------------------------------------------------------------


def test_max_edge_pin():
    assert MAX_EDGE == 0.25


def _max_edge_market(*, best_ask: float = 0.50, no_ask: float = 0.50) -> Market:
    return _market([_bucket("70-71°F", "range", lo=70, hi=71, best_ask=best_ask, no_ask=no_ask)])


def _max_edge_probe_p_win(side: str) -> float:
    """p_win (YES) or p_no (NO) for the shared 70-71°F / [69,70,71,72] fixture.

    Read off a neutral-ask, gate-open call so the boundary tests can derive
    an exact ask/no_ask relative to the real p_win.
    """
    fs = _forecast_set([69, 70, 71, 72])
    report = evaluate_market(
        _max_edge_market(),
        fs,
        floor=0.0,
        min_sources=2,
        min_sigma=1.5,
        min_edge=0.0,
        calibration=_full_cal(),
    )
    return next(o.p_win for o in report.outcomes if o.side == side)


def test_max_edge_boundary_is_inclusive_yes_side():
    p_win = _max_edge_probe_p_win("YES")
    ask = p_win - 0.25  # exact in IEEE double: 0.25 is 2^-2, no epsilon needed
    fs = _forecast_set([69, 70, 71, 72])
    report = evaluate_market(
        _max_edge_market(best_ask=ask),
        fs,
        floor=0.0,
        min_sources=2,
        min_sigma=1.5,
        min_edge=0.0,
        max_edge=0.25,
        calibration=_full_cal(),
    )
    o = next(o for o in report.outcomes if o.side == "YES")
    assert o.edge == 0.25
    assert o.recommended is True


def test_max_edge_boundary_is_inclusive_no_side():
    p_no = _max_edge_probe_p_win("NO")
    no_ask = p_no - 0.25  # exact in IEEE double
    fs = _forecast_set([69, 70, 71, 72])
    report = evaluate_market(
        _max_edge_market(no_ask=no_ask),
        fs,
        floor=0.0,
        min_sources=2,
        min_sigma=1.5,
        min_edge=0.0,
        max_edge=0.25,
        calibration=_full_cal(),
    )
    o = next(o for o in report.outcomes if o.side == "NO")
    assert o.edge == 0.25
    assert o.recommended is True


def test_max_edge_just_over_cap_drops_yes_side():
    p_win = _max_edge_probe_p_win("YES")
    ask = p_win - 0.26  # edge 0.26: just over the 0.25 cap
    fs = _forecast_set([69, 70, 71, 72])
    report = evaluate_market(
        _max_edge_market(best_ask=ask),
        fs,
        floor=0.0,
        min_sources=2,
        min_sigma=1.5,
        min_edge=0.0,
        max_edge=0.25,
        calibration=_full_cal(),
    )
    o = next(o for o in report.outcomes if o.side == "YES")
    assert o.edge > 0.25
    assert o.recommended is False
    # Over-cap outcomes stay in the report with their real numbers; only
    # `recommended` flips.
    assert o in report.outcomes


def test_max_edge_just_over_cap_drops_no_side():
    p_no = _max_edge_probe_p_win("NO")
    no_ask = p_no - 0.26  # edge 0.26: just over the 0.25 cap
    fs = _forecast_set([69, 70, 71, 72])
    report = evaluate_market(
        _max_edge_market(no_ask=no_ask),
        fs,
        floor=0.0,
        min_sources=2,
        min_sigma=1.5,
        min_edge=0.0,
        max_edge=0.25,
        calibration=_full_cal(),
    )
    o = next(o for o in report.outcomes if o.side == "NO")
    assert o.edge > 0.25
    assert o.recommended is False


def test_max_edge_stacks_with_other_gates():
    """max_edge is a conjunct like every other gate: a bet that clears floor,
    sources, and min_edge is still blocked when it fails the cap, and the
    same market is recommended again once the cap is lifted (None, the
    default)."""
    p_win = _max_edge_probe_p_win("YES")
    ask = p_win - 0.30  # edge 0.30: clears every other gate, fails the cap
    market = _max_edge_market(best_ask=ask)
    fs = _forecast_set([69, 70, 71, 72])

    uncapped = evaluate_market(
        market, fs, floor=0.0, min_sources=2, min_sigma=1.5, min_edge=0.0, calibration=_full_cal()
    )
    assert uncapped.outcomes[0].recommended is True

    capped = evaluate_market(
        market,
        fs,
        floor=0.0,
        min_sources=2,
        min_sigma=1.5,
        min_edge=0.0,
        max_edge=0.25,
        calibration=_full_cal(),
    )
    assert capped.outcomes[0].recommended is False


def test_max_edge_under_cap_still_blocked_by_other_gate():
    """The reverse stack: clearing max_edge is not sufficient on its own.  A
    bet with edge comfortably under the 0.25 cap is still not recommended
    when it fails a different gate (here, the confidence floor)."""
    p_win = _max_edge_probe_p_win("YES")
    ask = p_win - 0.10  # edge ~0.10: well under the cap
    market = _max_edge_market(best_ask=ask)
    fs = _forecast_set([69, 70, 71, 72])

    report = evaluate_market(
        market,
        fs,
        floor=p_win + 0.01,  # just above p_win: fails the confidence floor
        min_sources=2,
        min_sigma=1.5,
        min_edge=0.0,
        max_edge=0.25,
        calibration=_full_cal(),
    )
    o = next(o for o in report.outcomes if o.side == "YES")
    assert o.edge < 0.25
    assert o.recommended is False


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
    report = evaluate_market(
        market,
        fs,
        floor=0.45,
        min_sources=2,
        min_sigma=1.5,
        min_edge=0.0,
        calibration=_full_cal(),  # isolate the min-sources gate from the calibration gate
    )
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


def test_evaluate_precip_market_sigma_matches_floored_gamma():
    # var (0.001) is below PRECIP_VAR_FLOOR (0.01), so the floor binds: the
    # gamma actually integrated for p_win uses var_floor, not the raw var.
    # The reported sigma must match that floored gamma, not the raw moment.
    market = _precip_market()
    fs = _precip_forecast_set(market.target, var=0.001)
    report = evaluate_precip_market(
        market,
        fs,
        floor=CONFIDENCE_FLOOR,
        min_sources=MIN_SOURCES,
        min_edge=MIN_EDGE,
        var_floor=PRECIP_VAR_FLOOR,
    )
    assert report.sigma == pytest.approx(math.sqrt(PRECIP_VAR_FLOOR))
    gamma = fit_gamma(fs.mean, fs.var, floor=PRECIP_VAR_FLOOR)
    assert report.sigma**2 == pytest.approx(gamma.k * gamma.scale**2)
    bracket = market.buckets[0]
    yes = next(o for o in report.outcomes if o.bucket_label == bracket.label and o.side == "YES")
    assert yes.p_win == pytest.approx(bracket_probability(gamma, bracket))


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


def _precip_max_edge_target() -> PrecipTarget:
    return PrecipTarget(
        station=PRECIP_STATIONS["NYC"],
        variable="PRCP",
        year=2026,
        month=6,
        settlement_date=date(2026, 6, 30),
    )


def _precip_max_edge_market(*, best_ask: float, no_ask: float) -> PrecipMonthlyMarket:
    """A single-bracket precip market with an ask under our own control,
    parallel to _max_edge_market on the temperature path."""
    target = _precip_max_edge_target()
    bracket = PrecipBracket(
        label="2-3in",
        kind="range",
        lo=2.0,
        hi=3.0,
        threshold=None,
        yes_token_id="t",
        best_ask=best_ask,
        best_bid=None,
        yes_price=0.0,
        no_ask=no_ask,
    )
    return PrecipMonthlyMarket(id="pm1", slug="s", title="t", target=target, buckets=[bracket])


def test_evaluate_precip_market_max_edge_caps_both_sides():
    """Same inclusive-cap contract as evaluate_market's temperature path: an
    over-cap YES or NO outcome is dropped from recommended, an at-cap one
    stays recommended."""
    fs = _precip_forecast_set(_precip_max_edge_target(), mean=2.5, var=0.6)
    probe = evaluate_precip_market(
        _precip_max_edge_market(best_ask=0.50, no_ask=0.50),
        fs,
        floor=0.0,
        min_sources=0,
        min_edge=0.0,
        var_floor=PRECIP_VAR_FLOOR,
    )
    p_win = next(o.p_win for o in probe.outcomes if o.side == "YES")
    p_no = next(o.p_win for o in probe.outcomes if o.side == "NO")

    at_cap = evaluate_precip_market(
        _precip_max_edge_market(best_ask=p_win - 0.25, no_ask=p_no - 0.25),
        fs,
        floor=0.0,
        min_sources=0,
        min_edge=0.0,
        var_floor=PRECIP_VAR_FLOOR,
        max_edge=0.25,
    )
    yes_at_cap = next(o for o in at_cap.outcomes if o.side == "YES")
    no_at_cap = next(o for o in at_cap.outcomes if o.side == "NO")
    assert yes_at_cap.edge == 0.25
    assert yes_at_cap.recommended is True
    assert no_at_cap.edge == 0.25
    assert no_at_cap.recommended is True

    over_cap = evaluate_precip_market(
        _precip_max_edge_market(best_ask=p_win - 0.30, no_ask=p_no - 0.30),
        fs,
        floor=0.0,
        min_sources=0,
        min_edge=0.0,
        var_floor=PRECIP_VAR_FLOOR,
        max_edge=0.25,
    )
    yes_over_cap = next(o for o in over_cap.outcomes if o.side == "YES")
    no_over_cap = next(o for o in over_cap.outcomes if o.side == "NO")
    assert yes_over_cap.edge > 0.25
    assert yes_over_cap.recommended is False
    assert no_over_cap.edge > 0.25
    assert no_over_cap.recommended is False


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
        calibration=_full_cal(),  # isolate the uncalibratable gate from the calibration gate
    )
    # Advisory display must still render (intl markets stay in the report).
    assert report.outcomes, "outcomes must be non-empty so advisory still renders"
    assert report.mu is not None, "mu must be set so advisory still renders"
    # Recommended must be off for every outcome, both YES and NO sides.
    assert all(not o.recommended for o in report.outcomes), (
        f"intl market must not recommend any outcome; got {report.outcomes}"
    )


# ---------------------------------------------------------------------------
# Station-policy gate: a per-station exclusion (#302, the #296 addendum)
# ---------------------------------------------------------------------------

_KNYC_STATION_FOR_GATE = Station(
    city="NYC",
    icao="KNYC",
    name="Central Park, New York",
    lat=40.7790,
    lon=-73.9692,
    timezone="America/New_York",
    wunderground_url="https://forecast.weather.gov/product.php?site=OKX&product=CLI&issuedby=NYC",
    ghcnd_id="USW00094728",
)


def _gate_market_knyc() -> Market:
    """KNYC-shaped market with two buckets to exercise both YES and NO recommended
    paths, mirroring _gate_market_intl's shape for the uncalibratable gate.

    Forecast centered at 75F:
    - "68F or higher": p_win ~ 1.0, best_ask=0.05 -> edge >> min_edge (YES side live)
    - "90F or higher": no best_ask so YES excluded; no_ask=0.05 -> p_no ~ 1.0,
      edge_no >> min_edge (NO side live).
    Both sides would be recommended absent the station policy.
    """
    target = Target(station=_KNYC_STATION_FOR_GATE, variable="TMAX", local_date=date(2026, 6, 15))
    return Market(
        id="gate_knyc",
        slug="gate-knyc",
        title="Highest temperature in NYC on Jun 15?",
        target=target,
        buckets=[
            _bucket("68°F or higher", "above", threshold=68, best_ask=0.05),
            _bucket("90°F or higher", "above", threshold=90, no_ask=0.05),
        ],
    )


def _two_source_knyc(target: Target) -> ForecastSet:
    """Two live sources, forecast centered at 75F, comfortably between the 68F
    and 90F thresholds so both sides of _gate_market_knyc clear every other
    gate; only the station policy is left to isolate."""
    samples = [
        ForecastSample(
            source=src,
            model="m",
            member=None,
            station=target.station.icao,
            variable="TMAX",
            target_date=target.local_date,
            lead_time_days=1,
            value_f=75.0 + offset,
            issued_at=None,
        )
        for src in ("open-meteo", "nws")
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


def test_knyc_excluded_from_recommendations() -> None:
    """A KNYC-shaped market with full calibration and all other gates passing
    must never produce recommended=True, on any side, when station_policy marks
    it excluded (#302, the #296 addendum's KNYC forecast-skill verdict).

    Mirrors test_intl_market_never_recommended's structure for the
    uncalibratable gate: advisory display is unaffected (outcomes non-empty,
    mu set), only recommended is forced off, and MarketReport.policy_exclusion
    carries the policy's reason.
    """
    market = _gate_market_knyc()
    fs = _two_source_knyc(market.target)
    policy = STATION_POLICIES["KNYC"]

    report = evaluate_market(
        market,
        fs,
        floor=CONFIDENCE_FLOOR,
        min_sources=MIN_SOURCES,
        min_sigma=MIN_SIGMA_F,
        min_edge=MIN_EDGE,
        calibration=_full_cal(),
        station_policy=policy,
    )
    assert report.outcomes, "outcomes must be non-empty so advisory still renders"
    assert report.mu is not None, "mu must be set so advisory still renders"
    assert all(not o.recommended for o in report.outcomes), (
        f"KNYC market must not recommend any outcome; got {report.outcomes}"
    )
    assert report.policy_exclusion == policy.reason


def test_knyc_market_without_policy_is_recommendable() -> None:
    """Control: the same KNYC-shaped market with no station_policy passed clears
    every other gate, proving the exclusion above is what blocks it and not an
    incidental gate failure."""
    market = _gate_market_knyc()
    fs = _two_source_knyc(market.target)

    report = evaluate_market(
        market,
        fs,
        floor=CONFIDENCE_FLOOR,
        min_sources=MIN_SOURCES,
        min_sigma=MIN_SIGMA_F,
        min_edge=MIN_EDGE,
        calibration=_full_cal(),
    )
    assert any(o.recommended for o in report.outcomes), (
        f"control market should have at least one recommended outcome; got {report.outcomes}"
    )
    assert report.policy_exclusion is None


# ---------------------------------------------------------------------------
# KHOU (Houston Hobby) exclusion (#372, batch #370)
# ---------------------------------------------------------------------------

_KHOU_STATION_FOR_GATE = Station(
    city="Houston",
    icao="KHOU",
    name="Houston William P. Hobby Airport",
    lat=29.6459,
    lon=-95.2821,
    timezone="America/Chicago",
    wunderground_url="https://www.wunderground.com/history/daily/us/tx/houston/KHOU",
    ghcnd_id="USW00012918",
)


def _gate_market_hou() -> Market:
    """KHOU-shaped market mirroring _gate_market_knyc's two-bucket structure.

    Forecast centered at 85F (typical Houston summer):
    - "78F or higher": p_win ~ 1.0, best_ask=0.05 -> edge >> min_edge (YES side live)
    - "100F or higher": no best_ask so YES excluded; no_ask=0.05 -> p_no ~ 1.0,
      edge_no >> min_edge (NO side live).

    Both sides would be recommended absent the station policy.
    """
    target = Target(station=_KHOU_STATION_FOR_GATE, variable="TMAX", local_date=date(2026, 7, 15))
    return Market(
        id="gate_hou",
        slug="gate-hou",
        title="Highest temperature in Houston on Jul 15?",
        target=target,
        buckets=[
            _bucket("78°F or higher", "above", threshold=78, best_ask=0.05),
            _bucket("100°F or higher", "above", threshold=100, no_ask=0.05),
        ],
    )


def _two_source_hou(target: Target) -> ForecastSet:
    """Two live sources, forecast centered at 85F, comfortably between the 78F
    and 100F thresholds so both sides of _gate_market_hou clear every other
    gate; only the station policy is left to isolate."""
    samples = [
        ForecastSample(
            source=src,
            model="m",
            member=None,
            station=target.station.icao,
            variable="TMAX",
            target_date=target.local_date,
            lead_time_days=1,
            value_f=85.0 + offset,
            issued_at=None,
        )
        for src in ("open-meteo", "nws")
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


def test_khou_excluded_from_recommendations() -> None:
    """A KHOU-shaped market with full calibration and all other gates passing
    must never produce recommended=True, on any side, when station_policy marks
    it excluded (#372, the venue-decomposition diagnostic's Houston verdict).

    Mirrors test_knyc_excluded_from_recommendations: advisory display is
    unaffected (outcomes non-empty, mu set), only recommended is forced off,
    and MarketReport.policy_exclusion carries the policy's reason.
    """
    market = _gate_market_hou()
    fs = _two_source_hou(market.target)
    policy = STATION_POLICIES["KHOU"]

    report = evaluate_market(
        market,
        fs,
        floor=CONFIDENCE_FLOOR,
        min_sources=MIN_SOURCES,
        min_sigma=MIN_SIGMA_F,
        min_edge=MIN_EDGE,
        calibration=_full_cal(),
        station_policy=policy,
    )
    assert report.outcomes, "outcomes must be non-empty so advisory still renders"
    assert report.mu is not None, "mu must be set so advisory still renders"
    assert all(not o.recommended for o in report.outcomes), (
        f"KHOU market must not recommend any outcome; got {report.outcomes}"
    )
    assert report.policy_exclusion == policy.reason


def test_khou_market_without_policy_is_recommendable() -> None:
    """Control: the same KHOU-shaped market with no station_policy passed clears
    every other gate, proving the exclusion above is what blocks it and not an
    incidental gate failure."""
    market = _gate_market_hou()
    fs = _two_source_hou(market.target)

    report = evaluate_market(
        market,
        fs,
        floor=CONFIDENCE_FLOOR,
        min_sources=MIN_SOURCES,
        min_sigma=MIN_SIGMA_F,
        min_edge=MIN_EDGE,
        calibration=_full_cal(),
    )
    assert any(o.recommended for o in report.outcomes), (
        f"control market should have at least one recommended outcome; got {report.outcomes}"
    )
    assert report.policy_exclusion is None


# ---------------------------------------------------------------------------
# Per-(station, variable) edge-floor delta gate (#303, the #296 addendum)
# ---------------------------------------------------------------------------


def _delta_market(city: str, variable: str, *, best_ask: float) -> Market:
    """A single-bucket market whose threshold sits far below the forecast mean,
    so p_win is effectively 1.0 regardless of station or variable: the edge is
    then controlled entirely by best_ask, isolating the edge-floor-delta gate
    from every other gate (confidence floor, min sources, calibration)."""
    target = build_target(city, variable, date(2026, 6, 15))
    return Market(
        id="delta-market",
        slug="delta-market",
        title=f"Highest temperature in {city} on Jun 15?",
        target=target,
        buckets=[_bucket("50°F or higher", "above", threshold=50, best_ask=best_ask)],
    )


def _two_source_75(target: Target) -> ForecastSet:
    """Two live sources, forecast centered at 75F with a tight spread, far above
    the 50F threshold in _delta_market."""
    samples = [
        ForecastSample(
            source=src,
            model="m",
            member=None,
            station=target.station.icao,
            variable=target.variable,
            target_date=target.local_date,
            lead_time_days=1,
            value_f=75.0 + offset,
            issued_at=None,
        )
        for src in ("open-meteo", "nws")
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


def _evaluate_delta(city: str, variable: str, *, best_ask: float) -> MarketReport:
    """Evaluate _delta_market/_two_source_75 through the live-run lookup pattern:
    the caller (here, the test) looks up STATION_EDGE_DELTA by (icao, variable)
    and passes it in, mirroring cli.py's STATION_EDGE_DELTA.get(...) call sites."""
    market = _delta_market(city, variable, best_ask=best_ask)
    fs = _two_source_75(market.target)
    delta = STATION_EDGE_DELTA.get((market.target.station.icao, variable), 0.0)
    return evaluate_market(
        market,
        fs,
        floor=CONFIDENCE_FLOOR,
        min_sources=MIN_SOURCES,
        min_sigma=MIN_SIGMA_F,
        min_edge=MIN_EDGE,
        calibration=_full_cal(),
        min_edge_delta=delta,
    )


def test_ksfo_tmax_edge_below_delta_floor_not_recommended() -> None:
    """KSFO TMAX, full calibration, floor and sources cleared, edge in
    [0.07, 0.12): MIN_EDGE alone would recommend it, but the +0.05 KSFO TMAX
    delta raises the bar to 0.12, so it must not be recommended."""
    report = _evaluate_delta("San Francisco", "TMAX", best_ask=0.92)
    outcome = report.outcomes[0]
    assert 0.07 <= outcome.edge < 0.12, f"edge={outcome.edge} must land in [0.07, 0.12)"
    assert outcome.recommended is False


def test_klax_byte_equivalent_market_recommended() -> None:
    """The byte-equivalent market at KLAX (no station-edge-delta entry) with the
    same edge that KSFO TMAX suppressed must be recommended: the delta is
    per-(station, variable), not a blanket edge-floor raise."""
    ksfo = _evaluate_delta("San Francisco", "TMAX", best_ask=0.92)
    klax = _evaluate_delta("Los Angeles", "TMAX", best_ask=0.92)
    assert klax.outcomes[0].edge == pytest.approx(ksfo.outcomes[0].edge)
    assert klax.outcomes[0].recommended is True


def test_ksfo_tmin_same_edge_recommended() -> None:
    """KSFO TMIN, same setup as the suppressed KSFO TMAX case: the delta key is
    (icao, variable), so TMIN is untouched and clears the plain MIN_EDGE bar."""
    report = _evaluate_delta("San Francisco", "TMIN", best_ask=0.92)
    outcome = report.outcomes[0]
    assert 0.07 <= outcome.edge < 0.12
    assert outcome.recommended is True


def test_ksfo_tmax_edge_at_delta_floor_recommended() -> None:
    """KSFO TMAX with edge >= 0.12 clears the raised bar: this is a raised
    floor, not a blanket exclusion."""
    report = _evaluate_delta("San Francisco", "TMAX", best_ask=0.83)
    outcome = report.outcomes[0]
    assert outcome.edge >= 0.12, f"edge={outcome.edge} must clear the raised 0.12 bar"
    assert outcome.recommended is True


def _gate_market_ksfo_delta(*, no_ask: float) -> Market:
    """KSFO TMAX market with two buckets, mirroring _gate_market_knyc's shape:
    one best_ask-only bucket (YES side, not asserted on here) and one
    no_ask-only bucket (NO side, isolated from best_ask). Forecast centered at
    75F:
    - "50F or higher": best_ask=0.05, present only so the market has a normal
      YES outcome too, not itself asserted on.
    - "110F or higher": no best_ask so YES is excluded; p_win ~ 0, so
      p_no ~ 1.0 and edge_no is controlled entirely by no_ask.
    """
    target = build_target("San Francisco", "TMAX", date(2026, 6, 15))
    return Market(
        id="gate-ksfo-delta",
        slug="gate-ksfo-delta",
        title="Highest temperature in San Francisco on Jun 15?",
        target=target,
        buckets=[
            _bucket("50°F or higher", "above", threshold=50, best_ask=0.05),
            _bucket("110°F or higher", "above", threshold=110, no_ask=no_ask),
        ],
    )


def test_ksfo_tmax_no_side_edge_below_delta_floor_not_recommended() -> None:
    """The NO-side gate (edge_no >= effective_min_edge) must respect the KSFO
    TMAX delta too, not just the YES-side gate. Every other delta test above
    builds a YES-only market (_delta_market has no no_ask bucket), so reverting
    the NO-side line alone (edge_no >= min_edge instead of effective_min_edge)
    would leave the rest of the suite green. This two-bucket market mirrors
    _gate_market_knyc's shape to isolate the NO outcome: edge_no lands in
    [0.07, 0.12), so plain MIN_EDGE would recommend it but the +0.05 KSFO TMAX
    delta must suppress it."""
    market = _gate_market_ksfo_delta(no_ask=0.92)
    fs = _two_source_75(market.target)
    delta = STATION_EDGE_DELTA.get((market.target.station.icao, "TMAX"), 0.0)

    report = evaluate_market(
        market,
        fs,
        floor=CONFIDENCE_FLOOR,
        min_sources=MIN_SOURCES,
        min_sigma=MIN_SIGMA_F,
        min_edge=MIN_EDGE,
        calibration=_full_cal(),
        min_edge_delta=delta,
    )
    no_outcomes = [o for o in report.outcomes if o.side == "NO"]
    assert len(no_outcomes) == 1, f"expected exactly one NO outcome; got {report.outcomes}"
    outcome = no_outcomes[0]
    assert 0.07 <= outcome.edge < 0.12, f"edge={outcome.edge} must land in [0.07, 0.12)"
    assert outcome.recommended is False


def test_recommended_requires_current_min_edge() -> None:
    """Regression pin for #350: a non-delta station (KLAX) with edge ~0.06
    cleared the old 0.05 MIN_EDGE but must not clear the raised live floor.
    Pins MIN_EDGE symbolically so a revert to 0.05 fails this test."""
    report = _evaluate_delta("Los Angeles", "TMAX", best_ask=0.94)
    outcome = report.outcomes[0]
    assert 0.05 < outcome.edge < MIN_EDGE, f"edge={outcome.edge} must sit below MIN_EDGE"
    assert outcome.recommended is False


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
        calibration=_full_cal(),  # isolate the min-sources gate from the calibration gate
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
        calibration=_full_cal(),  # isolate the floor gate from the calibration gate
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
        calibration=_full_cal(),
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
