import json
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

import rainmaker.pnl_backtest as pnl_backtest_mod
from rainmaker.backfill import (
    HISTORICAL_FORECAST_URL,
    fetch_historical_forecasts,
    fetch_historical_samples,
)
from rainmaker.config import (
    INTL_STATIONS,
    MIN_CAL_BIAS_SAMPLES,
    MIN_CAL_SAMPLES,
    MIN_SIGMA_F,
    OPENMETEO_MODELS,
    STATIONS,
    build_target,
)
from rainmaker.domain import Bucket, Market
from rainmaker.forecasts.asos import MESONET_ASOS_URL
from rainmaker.forecasts.base import ForecastSample, ForecastSet, SourceCoverage
from rainmaker.pnl_backtest import (
    Bet,
    FillCoverage,
    LeadPnl,
    PnlBacktestResult,
    backtest_pnl,
    forecast_set_from_samples,
    market_at_lead,
    render_pnl_report,
    replay_market,
    score,
)
from rainmaker.polymarket.markets import parse_market
from rainmaker.polymarket.prices import CLOB_PRICES_URL, PricePoint
from rainmaker.polymarket.trades import TRADES_URL, FillPoint
from rainmaker.probability.calibration import Calibration
from rainmaker.probability.distribution import fit_gaussian

FIXTURES = Path(__file__).parent / "fixtures"
KLGA = STATIONS["NYC"]


def _full_cal(
    *, bias: float = 0.0, var_a: float = 0.0, var_b: float = 1.0, n_samples: int = MIN_CAL_SAMPLES
) -> Calibration:
    """A calibration cell for replay tests.

    Defaults (bias=0, var_a=0, var_b=1) are the identity transform: apply_calibration
    then returns mu and sigma unchanged (mu - 0 = mu; sqrt(0 + 1*sigma^2) = sigma), so
    passing this reaches the "full" tier (clearing the #225 gate) without moving any
    numeric expectation in tests that only care about the gate, not the calibration math.
    """
    return Calibration(
        station="KLGA",
        variable="TMAX",
        lead_time=1,
        bias=bias,
        var_a=var_a,
        var_b=var_b,
        n_samples=n_samples,
    )


def _hist_fixture() -> dict[str, Any]:
    return json.loads((FIXTURES / "openmeteo_hist_multimodel_klga.json").read_text())


def _bucket(label: str, kind: str, **kw: Any) -> Bucket:
    return Bucket(
        label=label,
        kind=kind,
        lo=kw.get("lo"),
        hi=kw.get("hi"),
        threshold=kw.get("threshold"),
        yes_token_id=kw.get("yes_token_id", "y"),
        best_ask=None,
        best_bid=None,
        yes_price=0.0,
        no_token_id=kw.get("no_token_id", "n"),
        no_ask=None,
    )


def _market(buckets: list[Bucket]) -> Market:
    return Market(
        id="m1",
        slug="s",
        title="Highest temperature in NYC on March 2?",
        target=build_target("NYC", "TMAX", date(2026, 3, 2)),
        buckets=buckets,
    )


# Phase B1


def test_fetch_historical_samples_one_per_model_per_date(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(re.escape(HISTORICAL_FORECAST_URL)), json=_hist_fixture()
    )
    with httpx.Client() as client:
        by_date = fetch_historical_samples(KLGA, date(2026, 3, 1), date(2026, 3, 5), client)
    assert set(by_date) == {date(2026, 3, i) for i in range(1, 6)}
    samples = by_date[date(2026, 3, 1)]
    assert len(samples) == len(OPENMETEO_MODELS)
    assert {s.model for s in samples} == set(OPENMETEO_MODELS)
    assert all(s.source == "open-meteo" and s.variable == "TMAX" for s in samples)
    assert all(s.station == "KLGA" and s.target_date == date(2026, 3, 1) for s in samples)
    by_model = {s.model: s.value_f for s in samples}
    assert by_model["gfs_seamless"] == pytest.approx(43.6)
    assert by_model["ecmwf_ifs025"] == pytest.approx(37.6)


# Phase B2


def test_forecast_set_from_samples_reproduces_archive_gaussian(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(re.escape(HISTORICAL_FORECAST_URL)), json=_hist_fixture()
    )
    httpx_mock.add_response(
        url=re.compile(re.escape(HISTORICAL_FORECAST_URL)), json=_hist_fixture()
    )
    with httpx.Client() as client:
        samples = fetch_historical_samples(KLGA, date(2026, 3, 1), date(2026, 3, 1), client)[
            date(2026, 3, 1)
        ]
        forecasts = fetch_historical_forecasts(KLGA, date(2026, 3, 1), date(2026, 3, 1), client)
    target = build_target("NYC", "TMAX", date(2026, 3, 1))
    fs = forecast_set_from_samples(target, samples)
    assert fs.target == target
    assert fs.samples == samples
    assert [c.source for c in fs.coverage] == ["open-meteo"]
    assert all(c.ok for c in fs.coverage)
    # the pooled fit equals the archive multi-model Gaussian for that date
    g = fit_gaussian(fs.samples, min_sigma=MIN_SIGMA_F)
    archive = forecasts[date(2026, 3, 1)]
    assert g.mu == pytest.approx(archive.mu)
    assert g.sigma == pytest.approx(archive.sigma, abs=1e-6)


# Phase B3


def test_market_at_lead_prices_buckets_from_mids():
    market = _market(
        [
            _bucket("36-37°F", "range", lo=36, hi=37, yes_token_id="c0"),
            _bucket("38°F or higher", "above", threshold=38, yes_token_id="d0"),
        ]
    )
    out = market_at_lead(market, {"36-37°F": 0.2, "38°F or higher": None})
    by_label = {b.label: b for b in out.buckets}
    assert by_label["36-37°F"].best_ask == pytest.approx(0.2)
    assert by_label["36-37°F"].no_ask == pytest.approx(0.8)
    # a None mid leaves no fillable price on either side
    assert by_label["38°F or higher"].best_ask is None
    assert by_label["38°F or higher"].no_ask is None
    # the rest of the bucket is preserved
    assert by_label["36-37°F"].yes_token_id == "c0"
    assert out.id == market.id and out.target == market.target


def test_market_at_lead_applies_spread_haircut_to_both_sides():
    market = _market([_bucket("36-37°F", "range", lo=36, hi=37, yes_token_id="c0")])
    out = market_at_lead(market, {"36-37°F": 0.20}, spread=0.04)
    b = out.buckets[0]
    assert b.best_ask == pytest.approx(0.22)  # mid 0.20 + spread/2
    assert b.no_ask == pytest.approx(0.82)  # (1 - 0.20) + spread/2
    # spread defaults to 0 -> raw mid, unchanged behavior
    out0 = market_at_lead(market, {"36-37°F": 0.20})
    assert out0.buckets[0].best_ask == pytest.approx(0.20)
    assert out0.buckets[0].no_ask == pytest.approx(0.80)


# Phase C1


def _tight_forecast_set() -> ForecastSet:
    target = build_target("NYC", "TMAX", date(2026, 3, 2))
    samples = [
        ForecastSample(
            source="open-meteo",
            model=m,
            member=None,
            station="KLGA",
            variable="TMAX",
            target_date=date(2026, 3, 2),
            lead_time_days=1,
            value_f=70.0,
            issued_at=None,
        )
        for m in OPENMETEO_MODELS
    ]
    # One source, so n_sources == 1 and min_sources=1 is the only gate that passes.
    return ForecastSet(
        target=target,
        samples=samples,
        coverage=[SourceCoverage(source="open-meteo", ok=True, n_samples=len(samples))],
    )


def test_replay_market_collapses_per_lead_and_settles():
    market = _market(
        [
            _bucket("79-80°F", "range", lo=79, hi=80, yes_token_id="y1"),
            _bucket("85-86°F", "range", lo=85, hi=86, yes_token_id="y2"),
        ]
    )
    settlement = datetime(2026, 3, 2, 12, tzinfo=UTC)
    x = int(settlement.timestamp())
    day = 86400
    hour = 3600
    # Prices per lead steer which bucket is the best-edge bet at each lead. Each
    # point sits 1h before its lead's target timestamp (strictly past, within
    # SNAP_TOLERANCE_S), so look-back-only snapping still lands on it.
    histories = {
        "y1": [
            PricePoint(t=x - hour, p=0.20),
            PricePoint(t=x - day - hour, p=0.20),
            PricePoint(t=x - 2 * day - hour, p=0.04),
            PricePoint(t=x - 3 * day - hour, p=0.04),
        ],
        "y2": [
            PricePoint(t=x - hour, p=0.10),
            PricePoint(t=x - day - hour, p=0.04),
            PricePoint(t=x - 2 * day - hour, p=0.20),
            PricePoint(t=x - 3 * day - hour, p=0.20),
        ],
    }
    bets, _fills_used = replay_market(
        market,
        _tight_forecast_set(),
        actual=80.0,
        histories=histories,
        settlement_dt=settlement,
        leads=(0, 1, 2, 3),
        floor=0.90,
        min_sources=1,
        min_sigma=1.5,
        min_edge=0.05,
        calibrations={lead: _full_cal() for lead in (0, 1, 2, 3)},
    )
    assert isinstance(bets[0], Bet)
    assert [b.lead for b in bets] == [0, 1, 2, 3]  # one collapsed bet per lead
    assert all(b.side == "NO" for b in bets)
    assert all(b.p_win > 0.99 for b in bets)  # NO on a far-off bucket is near-certain
    # leads 0-1 take 79-80 (cheaper-but-wrong: actual is 80, so NO loses)
    assert [b.bucket_label for b in bets[:2]] == ["79-80°F", "79-80°F"]
    assert [b.won for b in bets[:2]] == [False, False]
    # leads 2-3 take 85-86 (NO wins: actual 80 is not in it)
    assert [b.bucket_label for b in bets[2:]] == ["85-86°F", "85-86°F"]
    assert [b.won for b in bets[2:]] == [True, True]
    assert bets[0].ask == pytest.approx(0.80)  # no_ask = 1 - mid(0.20)
    assert bets[0].edge == pytest.approx(bets[0].p_win - 0.80)


# Phase H - replay the production policy: calibration and the NO floor (#226)


def test_replay_market_missing_calibration_cell_suppresses_bets():
    """No calibrations mapping, an explicit None cell, or a bias-only cell all
    suppress every bet for that lead (the #225 full-calibration gate); a
    full-tier cell for the same setup produces a bet, confirming the setup
    is otherwise valid.
    """
    market = _market([_bucket("80°F or higher", "above", threshold=80, yes_token_id="y1")])
    settlement = datetime(2026, 3, 2, 12, tzinfo=UTC)
    histories = {"y1": [PricePoint(t=int(settlement.timestamp()) - 3600, p=0.10)]}
    shifted = _full_cal(bias=-20.0)  # mu 70 -> 90: well clear of threshold=80

    def _replay(calibrations: dict[int, Calibration | None] | None) -> list[Bet]:
        bets, _ = replay_market(
            market,
            _tight_forecast_set(),
            actual=90.0,
            histories=histories,
            settlement_dt=settlement,
            leads=(0,),
            floor=0.80,
            min_sources=1,
            min_sigma=1.5,
            min_edge=0.05,
            calibrations=calibrations,
        )
        return bets

    assert _replay(None) == []  # no calibrations mapping at all
    assert _replay({0: None}) == []  # lookup explicitly returned no cell
    bias_only = _full_cal(bias=-20.0, n_samples=MIN_CAL_BIAS_SAMPLES)
    assert _replay({0: bias_only}) == []  # below full tier

    bets_with_cal = _replay({0: shifted})
    assert len(bets_with_cal) == 1
    assert bets_with_cal[0].side == "YES"


def test_replay_market_applies_per_lead_calibration_cell():
    """Each lead uses its own calibration cell; different cells at lead 0 and
    lead 1 change which side is recommended at each lead.
    """
    market = _market([_bucket("80°F or higher", "above", threshold=80, yes_token_id="y1")])
    settlement = datetime(2026, 3, 2, 12, tzinfo=UTC)
    x = int(settlement.timestamp())
    day = 86400
    hour = 3600
    histories = {"y1": [PricePoint(t=x - hour, p=0.10), PricePoint(t=x - day - hour, p=0.10)]}
    identity = _full_cal()  # mu stays 70: threshold 80 is far above -> YES not recommended
    shifted = _full_cal(bias=-20.0)  # mu shifts to 90: threshold 80 well below -> YES recommended

    bets, _ = replay_market(
        market,
        _tight_forecast_set(),
        actual=90.0,
        histories=histories,
        settlement_dt=settlement,
        leads=(0, 1),
        floor=0.80,
        min_sources=1,
        min_sigma=1.5,
        min_edge=0.05,
        calibrations={0: identity, 1: shifted},
    )
    by_lead = {b.lead: b for b in bets}
    assert set(by_lead) == {0, 1}
    # lead 0 (identity, mu=70): YES far below floor, but NO (p_no~1) clears it.
    assert by_lead[0].side == "NO"
    # lead 1 (shifted, mu=90): YES clears the floor -> the cell changed the side.
    assert by_lead[1].side == "YES"
    assert by_lead[0].p_win != by_lead[1].p_win


def test_replay_market_floor_no_places_bet_between_075_and_080():
    """A NO bet with p_no in [0.75, 0.80) is placed at floor_no=0.75 and
    suppressed at the flat floor 0.80 - the per-side NO floor, threaded
    end-to-end alongside the calibration gate.
    """
    from scipy.stats import norm

    mu, threshold = 70.0, 72
    target_p_no = 0.78
    z = float(norm.ppf(target_p_no))
    sigma = (threshold - 0.5 - mu) / z  # exact sigma so bucket_probability gives p_no=0.78

    market = _market([_bucket("72°F or higher", "above", threshold=threshold, yes_token_id="y1")])
    settlement = datetime(2026, 3, 2, 12, tzinfo=UTC)
    histories = {"y1": [PricePoint(t=int(settlement.timestamp()) - 3600, p=0.5)]}
    cal = _full_cal()

    def _replay(*, floor: float, floor_no: float | None) -> list[Bet]:
        bets, _ = replay_market(
            market,
            _tight_forecast_set(),
            actual=72.0,
            histories=histories,
            settlement_dt=settlement,
            leads=(0,),
            floor=floor,
            floor_no=floor_no,
            min_sources=1,
            min_sigma=sigma,
            min_edge=0.05,
            calibrations={0: cal},
        )
        return bets

    assert _replay(floor=0.80, floor_no=None) == []  # p_no 0.78 < flat floor 0.80
    bets_split = _replay(floor=0.80, floor_no=0.75)
    assert len(bets_split) == 1
    assert bets_split[0].side == "NO"
    assert bets_split[0].p_win == pytest.approx(target_p_no, abs=1e-3)


# Phase H2 - per-(station, variable) edge-floor delta, the replay mirrors
# production's STATION_EDGE_DELTA lookup (#226, #303)


def _market_at(city: str, buckets: list[Bucket]) -> Market:
    return Market(
        id="m1",
        slug="s",
        title=f"Highest temperature in {city} on March 2?",
        target=build_target(city, "TMAX", date(2026, 3, 2)),
        buckets=buckets,
    )


def _tight_forecast_set_at(city: str) -> ForecastSet:
    """Same shape as _tight_forecast_set (one source, tight spread at 70F), for
    a station other than the fixed KLGA target that helper uses."""
    target = build_target(city, "TMAX", date(2026, 3, 2))
    samples = [
        ForecastSample(
            source="open-meteo",
            model=m,
            member=None,
            station=target.station.icao,
            variable="TMAX",
            target_date=date(2026, 3, 2),
            lead_time_days=1,
            value_f=70.0,
            issued_at=None,
        )
        for m in OPENMETEO_MODELS
    ]
    return ForecastSet(
        target=target,
        samples=samples,
        coverage=[SourceCoverage(source="open-meteo", ok=True, n_samples=len(samples))],
    )


def _replay_edge_delta_market(city: str, ask: float) -> list[Bet]:
    market = _market_at(city, [_bucket("50°F or higher", "above", threshold=50, yes_token_id="y1")])
    settlement = datetime(2026, 3, 2, 12, tzinfo=UTC)
    histories = {"y1": [PricePoint(t=int(settlement.timestamp()) - 3600, p=ask)]}
    bets, _ = replay_market(
        market,
        _tight_forecast_set_at(city),
        actual=70.0,
        histories=histories,
        settlement_dt=settlement,
        leads=(0,),
        floor=0.90,
        min_sources=1,
        min_sigma=1.5,
        min_edge=0.05,
        calibrations={0: _full_cal()},
    )
    return bets


def test_replay_market_suppresses_ksfo_tmax_edge_below_delta_floor():
    """An ask that produces edge in [0.05, 0.10) (below plain MIN_EDGE alone
    would place it) is suppressed at KSFO TMAX: replay_market applies the same
    STATION_EDGE_DELTA lookup the live run makes in cli.py (#226, #303)."""
    assert _replay_edge_delta_market("San Francisco", ask=0.92) == []


def test_replay_market_klax_byte_equivalent_not_suppressed():
    """The byte-equivalent market at KLAX (no STATION_EDGE_DELTA entry) with
    the same edge places a bet: the delta is per-(station, variable)."""
    bets = _replay_edge_delta_market("Los Angeles", ask=0.92)
    assert len(bets) == 1
    assert bets[0].bucket_label == "50°F or higher"


# Phase I - look-back-only pricing, no look-ahead (#227)


def test_replay_market_skips_lead_when_only_future_price_available():
    """A price point strictly after the decision time must not be used: the
    lead gets no bet rather than a look-ahead price."""
    market = _market([_bucket("80°F or higher", "above", threshold=80, yes_token_id="y1")])
    settlement = datetime(2026, 3, 2, 12, tzinfo=UTC)
    x = int(settlement.timestamp())
    # Only a point one hour after the lead-0 target (=x): no past price to snap to.
    histories = {"y1": [PricePoint(t=x + 3600, p=0.10)]}
    bets, _ = replay_market(
        market,
        _tight_forecast_set(),
        actual=90.0,
        histories=histories,
        settlement_dt=settlement,
        leads=(0,),
        floor=0.80,
        min_sources=1,
        min_sigma=1.5,
        min_edge=0.05,
        calibrations={0: _full_cal(bias=-20.0)},
    )
    assert bets == []


def test_replay_market_ignores_nearer_future_price_uses_past_price():
    """A future point nearer to the target than a past point must still be
    ignored: the ask is snapped from the past point, not the closer future one."""
    market = _market([_bucket("80°F or higher", "above", threshold=80, yes_token_id="y1")])
    settlement = datetime(2026, 3, 2, 12, tzinfo=UTC)
    x = int(settlement.timestamp())
    histories = {
        "y1": [
            PricePoint(t=x - 3600, p=0.10),  # 1h before: the only usable price
            PricePoint(t=x + 60, p=0.90),  # 1min after: nearer, but future - must be ignored
        ]
    }
    bets, _ = replay_market(
        market,
        _tight_forecast_set(),
        actual=90.0,
        histories=histories,
        settlement_dt=settlement,
        leads=(0,),
        floor=0.80,
        min_sources=1,
        min_sigma=1.5,
        min_edge=0.05,
        calibrations={0: _full_cal(bias=-20.0)},
    )
    assert len(bets) == 1
    assert bets[0].side == "YES"
    assert bets[0].ask == pytest.approx(0.10)


def test_replay_market_fills_ignore_future_fill_uses_past_fill():
    """Fill snapping is look-back-only too: a nearer future fill must not be
    used as the ask; the past fill is used instead."""
    market = _market(
        [_bucket("80°F or higher", "above", threshold=80, yes_token_id="y1", no_token_id="n1")]
    )
    settlement = datetime(2026, 3, 2, 12, tzinfo=UTC)
    x = int(settlement.timestamp())
    histories = {"y1": [PricePoint(t=x - 3600, p=0.50)]}
    fill_histories = {
        "y1": [
            FillPoint(t=x - 3600, p=0.10),  # past: usable
            FillPoint(t=x + 60, p=0.90),  # future: nearer, must be ignored
        ]
    }
    bets, fills_used = replay_market(
        market,
        _tight_forecast_set(),
        actual=90.0,
        histories=histories,
        settlement_dt=settlement,
        leads=(0,),
        floor=0.80,
        min_sources=1,
        min_sigma=1.5,
        min_edge=0.05,
        fill_histories=fill_histories,
        calibrations={0: _full_cal(bias=-20.0)},
    )
    assert fills_used == 1
    assert len(bets) == 1
    assert bets[0].ask == pytest.approx(0.10)  # from the past fill, not the future one


# Phase C2


def _bet(lead: int, ask: float, edge: float, won: bool) -> Bet:
    return Bet(lead=lead, bucket_label="x", side="NO", p_win=0.95, ask=ask, edge=edge, won=won)


def test_score_aggregates_pnl_per_lead_and_overall():
    bets = [
        _bet(0, ask=0.80, edge=0.15, won=True),
        _bet(0, ask=0.60, edge=0.10, won=False),
        _bet(1, ask=0.50, edge=0.45, won=True),
    ]
    per_lead, overall = score(bets, leads=(0, 1, 2))
    by_lead = {lp.lead: lp for lp in per_lead}
    assert set(by_lead) == {0, 1, 2}

    l0 = by_lead[0]  # one win (+0.20), one loss (-0.60) over 1.40 staked
    assert (l0.n_bets, l0.wins, l0.losses) == (2, 1, 1)
    assert l0.total_pnl == pytest.approx(-0.40)
    assert l0.roi == pytest.approx(-0.40 / 1.40)
    assert l0.win_rate == pytest.approx(0.5)
    assert l0.mean_edge == pytest.approx(0.125)

    l1 = by_lead[1]
    assert (l1.n_bets, l1.wins) == (1, 1)
    assert l1.total_pnl == pytest.approx(0.50)
    assert l1.roi == pytest.approx(1.0)

    l2 = by_lead[2]  # no bets at this lead -> zeroed, not dropped
    assert (l2.n_bets, l2.wins, l2.losses) == (0, 0, 0)
    assert (l2.total_pnl, l2.roi, l2.win_rate, l2.mean_edge) == (0.0, 0.0, 0.0, 0.0)

    assert isinstance(overall, LeadPnl)
    assert overall.lead == -1  # sentinel: all leads pooled
    assert (overall.n_bets, overall.wins, overall.losses) == (3, 2, 1)
    assert overall.total_pnl == pytest.approx(0.10)
    assert overall.roi == pytest.approx(0.10 / 1.90)
    assert overall.win_rate == pytest.approx(2 / 3)
    assert overall.mean_edge == pytest.approx((0.15 + 0.10 + 0.45) / 3)


# Phase C3


def _asos_fixture() -> str:
    # Same daily TMAX (43/34/35/50/45F for 03-01..03-05) as ncei_actuals_klga.json,
    # so venue_actuals routes KLGA (a Polymarket station) to ASOS and every
    # downstream bet/P/L assertion below holds unchanged.
    return (FIXTURES / "mesonet_asos_klga_2026-03-01_05.csv").read_text()


def _closed_events() -> list[dict[str, Any]]:
    return json.loads((FIXTURES / "polymarket_closed_weather_events.json").read_text())


def _clob_history() -> dict[str, Any]:
    return json.loads((FIXTURES / "clob_prices_history.json").read_text())


def _clob_callback(request: httpx.Request) -> httpx.Response:
    # The same flat 0.15 series for any requested token; the per-bucket forecast,
    # not the price, decides which buckets clear the gates.
    # the price-history request must carry the token in the 'market' param
    assert "market" in request.url.params
    assert request.url.params["market"]
    return httpx.Response(200, json=_clob_history())


def test_backtest_pnl_replays_closed_markets_end_to_end(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(re.escape(HISTORICAL_FORECAST_URL)), json=_hist_fixture()
    )
    httpx_mock.add_response(url=re.compile(re.escape(MESONET_ASOS_URL)), text=_asos_fixture())
    httpx_mock.add_callback(
        _clob_callback, url=re.compile(re.escape(CLOB_PRICES_URL)), is_reusable=True
    )
    with httpx.Client() as client:
        result = backtest_pnl(
            _closed_events(),
            client,
            on_or_after=date(2026, 3, 1),
            leads=(0, 1, 2, 3),
            calibration_lookup=lambda icao, lead: _full_cal(),
        )
    assert result is not None
    # The Feb market is date-filtered and London is dropped; two March markets remain.
    assert result.n_markets == 2
    by_lead = {lp.lead: lp for lp in result.per_lead}
    assert set(by_lead) == {0, 1, 2, 3}
    # Each March market sells the 38F+ bucket (NO) once per lead -> two bets a lead.
    assert all(by_lead[lead].n_bets == 2 for lead in (0, 1, 2, 3))
    assert result.overall.n_bets == 8
    # Both actuals (34, 35) miss the 38F-or-higher bucket, so every NO bet wins.
    assert result.overall.wins == 8
    assert result.overall.total_pnl > 0


def test_backtest_pnl_returns_none_when_all_filtered():
    with httpx.Client() as client:
        result = backtest_pnl(_closed_events(), client, on_or_after=date(2027, 1, 1))
    assert result is None


# Phase D1


def _sample_result() -> PnlBacktestResult:
    return PnlBacktestResult(
        n_markets=2,
        floor=0.90,
        min_sources=1,
        min_edge=0.05,
        per_lead=[
            LeadPnl(
                lead=0,
                n_bets=2,
                wins=2,
                losses=0,
                total_pnl=0.30,
                roi=0.176,
                win_rate=1.0,
                mean_edge=0.12,
            ),
            LeadPnl(
                lead=1,
                n_bets=2,
                wins=1,
                losses=1,
                total_pnl=-0.10,
                roi=-0.06,
                win_rate=0.5,
                mean_edge=0.10,
            ),
        ],
        overall=LeadPnl(
            lead=-1,
            n_bets=4,
            wins=3,
            losses=1,
            total_pnl=0.20,
            roi=0.06,
            win_rate=0.75,
            mean_edge=0.11,
        ),
    )


def test_render_pnl_report_table_and_disclosures():
    md, payload = render_pnl_report(_sample_result())
    assert "# Betting P/L backtest" in md
    assert "| 0 |" in md and "| 1 |" in md  # per-lead rows
    assert "| ALL |" in md  # pooled row
    assert "2-0" in md  # lead 0 win-loss
    # pricing and gate-relaxation disclosures
    lowered = md.lower()
    assert "mid" in lowered and "optimistic" in lowered
    assert "min_sources" in lowered and "two-source" in lowered
    # JSON payload round-trips the model
    assert payload == _sample_result().model_dump(mode="json")


def test_render_pnl_report_discloses_floor_no_calibration_coverage_and_look_ahead():
    result = _sample_result().model_copy(
        update={
            "floor_no": 0.75,
            "calibration_cells_checked": 8,
            "calibration_cells_found": 5,
        }
    )
    md, _payload = render_pnl_report(result)
    lowered = md.lower()
    assert "no floor" in lowered
    assert "75%" in md  # floor_no disclosed
    assert "5 of 8" in md  # calibration coverage
    assert "look-ahead" in lowered
    assert "current calibration table" in lowered


# Phase E - trades-based asks


def test_market_at_lead_uses_fills_when_provided_ignoring_spread():
    """Fill prices bypass the mid+spread path and are used directly as the ask.

    YES ask = fill for yes_token; NO ask = fill for no_token.
    Spread is not added on top of a fill price (a fill IS the paid ask).
    """
    market = _market(
        [
            _bucket("36-37°F", "range", lo=36, hi=37, yes_token_id="c0", no_token_id="c1"),
        ]
    )
    # mid would give 0.22 with spread=0.04; fill overrides this
    fills: dict[str, tuple[float | None, float | None]] = {"36-37°F": (0.25, 0.76)}
    out = market_at_lead(market, {"36-37°F": 0.20}, spread=0.04, fills=fills)
    b = out.buckets[0]
    assert b.best_ask == pytest.approx(0.25)  # from fill, not mid+spread
    assert b.no_ask == pytest.approx(0.76)  # from fill, not (1-mid)+spread


def test_market_at_lead_falls_back_to_mid_when_fill_is_none():
    """When both fill sides are None, fall back to mid+spread normally."""
    market = _market(
        [
            _bucket("36-37°F", "range", lo=36, hi=37, yes_token_id="c0", no_token_id="c1"),
        ]
    )
    fills: dict[str, tuple[float | None, float | None]] = {"36-37°F": (None, None)}
    out = market_at_lead(market, {"36-37°F": 0.20}, spread=0.04, fills=fills)
    b = out.buckets[0]
    assert b.best_ask == pytest.approx(0.22)  # mid 0.20 + spread/2
    assert b.no_ask == pytest.approx(0.82)  # (1-0.20) + spread/2


def test_market_at_lead_partial_fill_applies_per_side():
    """YES fill present but NO fill absent - YES uses fill, NO uses mid+spread."""
    market = _market(
        [
            _bucket("36-37°F", "range", lo=36, hi=37, yes_token_id="c0", no_token_id="c1"),
        ]
    )
    fills: dict[str, tuple[float | None, float | None]] = {"36-37°F": (0.25, None)}
    out = market_at_lead(market, {"36-37°F": 0.20}, spread=0.04, fills=fills)
    b = out.buckets[0]
    assert b.best_ask == pytest.approx(0.25)  # from fill
    assert b.no_ask == pytest.approx(0.82)  # fallback: (1-0.20) + spread/2


def _trades_fixture_raw() -> list[dict[str, Any]]:
    return json.loads((FIXTURES / "polymarket_trades_weather.json").read_text())


def _closed_events_with_condids() -> list[dict[str, Any]]:
    """Load the closed events fixture and inject conditionId into sub-markets.

    The fixture polymarket_closed_weather_events.json uses simplified token ids
    ("a0", "b0" etc). We add conditionIds so the trades path can look them up.
    """
    events = json.loads((FIXTURES / "polymarket_closed_weather_events.json").read_text())
    # Add conditionId to the "38F or higher" sub-market in the first two NYC events
    # matching the trades fixture (which uses conditionId "0xcond_d" for bucket "d0")
    for ev in events:
        if ev.get("slug", "").startswith("highest-temperature-in-nyc"):
            for m in ev.get("markets", []):
                tokens = json.loads(m["clobTokenIds"])
                if tokens[0] == "d0":
                    m["conditionId"] = "0xcond_d"
    return events


def test_backtest_pnl_trades_mode_uses_fill_as_ask(httpx_mock):
    """In trades mode, fill prices from the trades endpoint replace mid+spread asks.

    The trades fixture has fills for the "38F or higher" bucket (d0=YES, d1=NO):
    - Market 1 (March 2, settlement_ts=1772452800):
      lead 0 (target=1772452800): d0 fill at 0.11, d1 fill at 0.90 -> NO ask = 0.90
      lead 1 (target=1772366400): d0 fill at 0.12, d1 no fill -> NO ask = 0.85 (fallback)
      leads 2, 3: no fills -> NO ask = 0.85 (fallback)
    - Market 2 (March 3, settlement_ts=1772539200):
      lead 0 (target=1772539200): no fills -> NO ask = 0.85 (fallback)
      lead 1 (target=1772452800): d0 fill at 0.11, d1 fill at 0.90 -> NO ask = 0.90
      lead 2 (target=1772366400): d0 fill at 0.12, d1 no fill -> NO ask = 0.85 (fallback)
      lead 3: no fills -> NO ask = 0.85 (fallback)

    Fill coverage: market 1 uses fills at leads {0,1}, market 2 at leads {1,2} -> 4 total.
    The recommended "38F or higher" NO bet wins for both markets (actuals 34F/35F).
    Trades-mode total_pnl = 1.10 vs mid-mode 1.20 because two bets pay the higher
    0.90 fill ask instead of the 0.85 mid complement, reducing payout from 0.15 to 0.10.
    """
    httpx_mock.add_response(
        url=re.compile(re.escape(HISTORICAL_FORECAST_URL)), json=_hist_fixture()
    )
    httpx_mock.add_response(url=re.compile(re.escape(MESONET_ASOS_URL)), text=_asos_fixture())
    httpx_mock.add_callback(
        _clob_callback, url=re.compile(re.escape(CLOB_PRICES_URL)), is_reusable=True
    )
    httpx_mock.add_callback(
        lambda req: httpx.Response(200, json=_trades_fixture_raw()),
        url=re.compile(re.escape(TRADES_URL)),
        is_reusable=True,
    )
    with httpx.Client() as client:
        result = backtest_pnl(
            _closed_events_with_condids(),
            client,
            on_or_after=date(2026, 3, 1),
            leads=(0, 1, 2, 3),
            ask_source="trades",
            calibration_lookup=lambda icao, lead: _full_cal(),
        )
    assert result is not None
    assert result.ask_source == "trades"
    assert result.fill_coverage is not None
    # two markets, four leads each = 8 total lead-market combinations
    assert result.fill_coverage.n_leads == 8
    # market 1: fills at leads {0,1}; market 2: fills at leads {1,2} -> 4 total
    assert result.fill_coverage.fills_used == 4
    # End-to-end: fills flow into asks -> lower payouts when NO ask is 0.90 (d1 fill)
    # vs. mid complement 0.85. Two such leads (market1-lead0, market2-lead1) each
    # reduce payout by 0.05, so total_pnl = 1.20 - 0.10 = 1.10 (not 1.20 as in mid mode).
    assert result.overall.total_pnl == pytest.approx(1.10)


def test_replay_market_fill_coverage_distribution_per_market(httpx_mock):
    """replay_market fill coverage differs by market: March 2 gets fills at leads {0,1},
    March 3 at leads {1,2} - same fixture trades, different settlement offsets.

    Both markets share the same fill timestamps (d0 at 1772452750/1772366350,
    d1 at 1772452600). Against March 2 settlement_ts=1772452800, those land on
    leads 0 and 1. Against March 3 settlement_ts=1772539200, leads 1 and 2.
    Each market therefore has fills_used == 2.
    """
    # Two separate forecast fetches: one per market date.
    httpx_mock.add_response(
        url=re.compile(re.escape(HISTORICAL_FORECAST_URL)), json=_hist_fixture()
    )
    httpx_mock.add_response(
        url=re.compile(re.escape(HISTORICAL_FORECAST_URL)), json=_hist_fixture()
    )

    # Parse both NYC March markets from the fixture with conditionIds injected.
    events = _closed_events_with_condids()
    markets_march = [
        parse_market(ev)
        for ev in events
        if ev.get("slug", "").startswith("highest-temperature-in-nyc-on-march")
    ]
    # Settlement timestamps: March 2 = 1772452800, March 3 = 1772539200.
    settlement_dts = [
        datetime.fromisoformat(ev["endDate"].replace("Z", "+00:00"))
        for ev in events
        if ev.get("slug", "").startswith("highest-temperature-in-nyc-on-march")
    ]

    # Build fill_histories directly from the trades fixture (no HTTP needed).
    d0_fills = [
        FillPoint(t=f["timestamp"], p=f["price"])
        for f in _trades_fixture_raw()
        if f["side"] == "BUY" and f["asset"] == "d0"
    ]
    d1_fills = [
        FillPoint(t=f["timestamp"], p=f["price"])
        for f in _trades_fixture_raw()
        if f["side"] == "BUY" and f["asset"] == "d1"
    ]
    # Actuals: March 2 = 34F, March 3 = 35F (from the NCEI fixture).
    actuals_by_date = {"2026-03-02": 34.0, "2026-03-03": 35.0}
    clob_series = [PricePoint(t=p["t"], p=p["p"]) for p in _clob_history()["history"]]

    per_market_fills_used = []
    with httpx.Client() as client:
        for market, settlement_dt in zip(markets_march, settlement_dts, strict=True):
            samples = fetch_historical_samples(
                market.target.station,
                market.target.local_date,
                market.target.local_date,
                client,
            )[market.target.local_date]
            actual = actuals_by_date[str(market.target.local_date)]
            histories = {b.yes_token_id: clob_series for b in market.buckets}
            fill_histories: dict[str, list[FillPoint]] = {}
            for b in market.buckets:
                if b.label == "38°F or higher":
                    fill_histories[b.yes_token_id] = d0_fills
                    fill_histories[b.no_token_id] = d1_fills

            _, fills_used = replay_market(
                market,
                forecast_set_from_samples(market.target, samples),
                actual,
                histories,
                settlement_dt,
                leads=(0, 1, 2, 3),
                floor=0.80,
                min_sources=1,
                min_sigma=MIN_SIGMA_F,
                min_edge=0.05,
                fill_histories=fill_histories,
            )
            per_market_fills_used.append(fills_used)

    # Market 1 (March 2): fills land at leads 0 and 1.
    # Market 2 (March 3): same fill timestamps land at leads 1 and 2.
    # Each market contributes fills_used == 2.
    assert per_market_fills_used == [2, 2]


def test_backtest_pnl_mid_mode_no_fill_coverage(httpx_mock):
    """Mid mode (the default) leaves fill_coverage as None."""
    httpx_mock.add_response(
        url=re.compile(re.escape(HISTORICAL_FORECAST_URL)), json=_hist_fixture()
    )
    httpx_mock.add_response(url=re.compile(re.escape(MESONET_ASOS_URL)), text=_asos_fixture())
    httpx_mock.add_callback(
        _clob_callback, url=re.compile(re.escape(CLOB_PRICES_URL)), is_reusable=True
    )
    with httpx.Client() as client:
        result = backtest_pnl(
            _closed_events(), client, on_or_after=date(2026, 3, 1), leads=(0, 1, 2, 3)
        )
    assert result is not None
    assert result.ask_source == "mid"
    assert result.fill_coverage is None


def test_render_pnl_report_shows_trades_source_and_coverage():
    """Trades mode report mentions fills and coverage."""
    result = PnlBacktestResult(
        n_markets=2,
        floor=0.90,
        min_sources=1,
        min_edge=0.05,
        ask_source="trades",
        fill_coverage=FillCoverage(n_leads=8, fills_used=4),
        per_lead=[
            LeadPnl(
                lead=0,
                n_bets=2,
                wins=2,
                losses=0,
                total_pnl=0.30,
                roi=0.176,
                win_rate=1.0,
                mean_edge=0.12,
            ),
        ],
        overall=LeadPnl(
            lead=-1,
            n_bets=2,
            wins=2,
            losses=0,
            total_pnl=0.30,
            roi=0.176,
            win_rate=1.0,
            mean_edge=0.12,
        ),
    )
    md, _payload = render_pnl_report(result)
    lowered = md.lower()
    assert "trades" in lowered or "fill" in lowered
    # coverage should appear as "4 of 8 lead-market slots..."
    assert "4 of 8" in md


# Phase F - upper edge / confidence cap (#205)
#
# All cap tests use a forecast centered at 70F (tight cluster of samples).
# Two far-off buckets are used as NO bets: "79-80F" and "85-86F".
# At 70F mean the p_yes for those is tiny, so p_no ~ 1. Edge = p_no - no_ask.
# mid=0.20 -> no_ask=0.80 -> edge ~ 0.20 (higher edge, cheaper NO)
# mid=0.10 -> no_ask=0.90 -> edge ~ 0.10 (lower edge, pricier NO)


def _settlement_ts() -> tuple[datetime, int]:
    dt = datetime(2026, 3, 2, 12, tzinfo=UTC)
    return dt, int(dt.timestamp())


def _far_histories_two_buckets() -> dict[str, list[PricePoint]]:
    """Price histories for 79-80F (higher edge) and 85-86F (lower edge) NO bets."""
    _, x = _settlement_ts()
    return {
        "ya": [PricePoint(t=x - 3600, p=0.20)],  # 79-80F: no_ask=0.80, edge~0.20
        "yb": [PricePoint(t=x - 3600, p=0.10)],  # 85-86F: no_ask=0.90, edge~0.10
    }


def _far_market_two_buckets() -> Market:
    """Market with two far-off buckets; both produce recommended NO bets at 70F center."""
    return _market(
        [
            _bucket("79-80°F", "range", lo=79, hi=80, yes_token_id="ya", no_token_id="na"),
            _bucket("85-86°F", "range", lo=85, hi=86, yes_token_id="yb", no_token_id="nb"),
        ]
    )


def _replay_far(
    market: Market,
    histories: dict[str, list[PricePoint]],
    actual: float = 72.0,
    **kwargs: Any,
) -> tuple[list[Bet], int]:
    settlement, _ = _settlement_ts()
    kwargs.setdefault("calibrations", {0: _full_cal()})
    return replay_market(
        market,
        _tight_forecast_set(),
        actual,
        histories,
        settlement,
        leads=(0,),
        floor=0.80,
        min_sources=1,
        min_sigma=1.5,
        min_edge=0.05,
        **kwargs,
    )


# F1: max_edge=None reproduces the unbounded result


def test_cap_max_edge_none_reproduces_unbounded():
    market = _far_market_two_buckets()
    histories = _far_histories_two_buckets()

    bets_unbounded, _ = _replay_far(market, histories)
    bets_capped, _ = _replay_far(market, histories, max_edge=None, max_p_win=None)

    assert len(bets_unbounded) == len(bets_capped)
    if bets_unbounded:
        assert bets_unbounded[0].edge == pytest.approx(bets_capped[0].edge)
        assert bets_unbounded[0].bucket_label == bets_capped[0].bucket_label


# F2: max_edge=1.0 also reproduces the unbounded result (nothing excluded)


def test_cap_max_edge_1_reproduces_unbounded():
    market = _far_market_two_buckets()
    histories = _far_histories_two_buckets()

    bets_unbounded, _ = _replay_far(market, histories)
    bets_capped, _ = _replay_far(market, histories, max_edge=1.0)

    assert len(bets_unbounded) == len(bets_capped)
    if bets_unbounded:
        assert bets_unbounded[0].edge == pytest.approx(bets_capped[0].edge)


# F3: cap excludes the only recommended bet -> lead drops to no bet


def test_cap_excludes_only_bet_lead_drops():
    """Single bucket market; tight cap below the bet's edge -> no bet."""
    _, x = _settlement_ts()
    market = _market(
        [_bucket("79-80°F", "range", lo=79, hi=80, yes_token_id="yb", no_token_id="nb")]
    )
    histories = {"yb": [PricePoint(t=x - 3600, p=0.20)]}  # no_ask=0.80, edge~0.20

    bets_no_cap, _ = _replay_far(market, histories)
    assert len(bets_no_cap) == 1, "pre-condition: unbounded replay produces a bet"
    top_edge = bets_no_cap[0].edge

    # Cap strictly below the only bet's edge.
    bets_capped, _ = _replay_far(market, histories, max_edge=top_edge * 0.5)
    assert len(bets_capped) == 0


# F4: cap excludes top bet but lower-edge bucket survives -> substitution


def test_cap_substitutes_lower_edge_bet():
    """Fall-through: top bet capped, lower-edge recommended bet substitutes.

    Bet count stays 1 for the lead; bucket and edge change to the survivor.
    """
    market = _far_market_two_buckets()
    _, x = _settlement_ts()
    # "ya" (79-80F) has no_ask=0.80, so edge ~ p_no - 0.80 ~ 0.20.
    # "yb" (85-86F) has no_ask=0.90, so edge ~ p_no - 0.90 ~ 0.10.
    # Unbounded picks ya (higher edge). Cap just below ya's edge -> yb substitutes.
    histories = _far_histories_two_buckets()

    bets_unbounded, _ = _replay_far(market, histories)
    assert len(bets_unbounded) == 1
    assert bets_unbounded[0].bucket_label == "79-80°F"
    edge_a = bets_unbounded[0].edge

    # Verify "yb" is also independently recommended.
    _, x2 = _settlement_ts()
    settlement2, _ = _settlement_ts()
    bets_b_only, _ = replay_market(
        _market([_bucket("85-86°F", "range", lo=85, hi=86, yes_token_id="yb", no_token_id="nb")]),
        _tight_forecast_set(),
        72.0,
        {"yb": [PricePoint(t=x2 - 3600, p=0.10)]},
        settlement2,
        leads=(0,),
        floor=0.80,
        min_sources=1,
        min_sigma=1.5,
        min_edge=0.05,
        calibrations={0: _full_cal()},
    )
    assert len(bets_b_only) == 1, "pre-condition: 85-86F bucket is independently recommended"

    # Cap strictly below ya's edge: ya excluded, yb substitutes.
    bets_capped, _ = _replay_far(market, histories, max_edge=edge_a * 0.9)
    assert len(bets_capped) == 1  # still one bet (substitution, not deletion)
    assert bets_capped[0].bucket_label == "85-86°F"  # yb took over
    assert bets_capped[0].edge < edge_a  # lower edge


# F5: max_p_win is side-agnostic
#
# Market has two buckets:
#   "85-86°F" (range, far-off) -> NO bet: p_no ~ 1, edge ~ 0.20 (BEST uncapped)
#   "68°F or higher" (above, near-mean) -> YES bet: p_yes ~ 0.95, edge ~ 0.13
# Uncapped: "85-86°F" NO wins (higher edge). Capped at max_p_win=0.97:
#   NO excluded (p_no > 0.97), YES survives (p_yes < 0.97) -> side flips to YES.
# This proves the filter keys on RankedOutcome.p_win (which IS p_no for NO bets),
# not on p_yes, i.e. it is side-agnostic.


def test_cap_max_p_win_side_agnostic():
    """max_p_win=0.97 excludes a NO bet (p_no>0.97) while a YES bet (p_yes<0.97) survives.

    The resulting bet must switch side from NO to YES. That confirms the filter
    operates on o.p_win regardless of which side it encodes.
    """
    _, x = _settlement_ts()
    # "85-86°F": mid=0.20 -> no_ask=0.80, p_no~1, edge~0.20 (best uncapped; NO side)
    # "68°F or higher": mid=0.82 -> best_ask=0.82, p_yes~0.95, edge~0.13 (YES side)
    market = _market(
        [
            _bucket("85-86°F", "range", lo=85, hi=86, yes_token_id="yn", no_token_id="nn"),
            _bucket("68°F or higher", "above", threshold=68, yes_token_id="yy", no_token_id="ny"),
        ]
    )
    histories = {
        "yn": [PricePoint(t=x - 3600, p=0.20)],  # 85-86°F: no_ask=0.80
        "yy": [PricePoint(t=x - 3600, p=0.82)],  # 68°F or higher: yes_ask=0.82
    }

    # Pre-condition: uncapped, the NO bet is the best-edge bet.
    bets_uncapped, _ = _replay_far(market, histories)
    assert len(bets_uncapped) == 1, "pre-condition: exactly one bet uncapped"
    assert bets_uncapped[0].side == "NO"
    assert bets_uncapped[0].bucket_label == "85-86°F"
    assert bets_uncapped[0].p_win > 0.97

    # Pre-condition: the YES bucket is independently recommended (survives alone).
    market_yes_only = _market(
        [_bucket("68°F or higher", "above", threshold=68, yes_token_id="yy", no_token_id="ny")]
    )
    bets_yes_only, _ = _replay_far(market_yes_only, {"yy": [PricePoint(t=x - 3600, p=0.82)]})
    assert len(bets_yes_only) == 1, "pre-condition: YES bucket independently recommended"
    assert bets_yes_only[0].side == "YES"
    assert bets_yes_only[0].p_win < 0.97, (
        f"pre-condition: YES p_win={bets_yes_only[0].p_win} must be < 0.97"
    )

    # With max_p_win=0.97 the NO bet is excluded; the YES bet substitutes.
    bets_capped, _ = _replay_far(market, histories, max_p_win=0.97)
    assert len(bets_capped) == 1, "substitution: one bet survives (not zero)"
    assert bets_capped[0].side == "YES"
    assert bets_capped[0].bucket_label == "68°F or higher"
    assert bets_capped[0].p_win < 0.97


# F6: PnlBacktestResult carries caps and render discloses them; JSON round-trips


def test_pnl_backtest_result_carries_caps_and_render_discloses():
    result = PnlBacktestResult(
        n_markets=2,
        floor=0.80,
        min_sources=1,
        min_edge=0.05,
        max_edge=0.30,
        max_p_win=0.97,
        per_lead=[
            LeadPnl(
                lead=0,
                n_bets=1,
                wins=1,
                losses=0,
                total_pnl=0.10,
                roi=0.20,
                win_rate=1.0,
                mean_edge=0.10,
            ),
        ],
        overall=LeadPnl(
            lead=-1,
            n_bets=1,
            wins=1,
            losses=0,
            total_pnl=0.10,
            roi=0.20,
            win_rate=1.0,
            mean_edge=0.10,
        ),
    )
    # JSON round-trip preserves the new fields.
    dumped = result.model_dump(mode="json")
    assert dumped["max_edge"] == pytest.approx(0.30)
    assert dumped["max_p_win"] == pytest.approx(0.97)
    loaded = PnlBacktestResult.model_validate(dumped)
    assert loaded.max_edge == pytest.approx(0.30)
    assert loaded.max_p_win == pytest.approx(0.97)

    # render_pnl_report discloses the caps.
    md, payload = render_pnl_report(result)
    lowered = md.lower()
    assert "max_edge" in lowered or "edge cap" in lowered or "upper" in lowered
    assert "0.30" in md or "30%" in md  # max_edge value disclosed
    assert "0.97" in md or "97%" in md  # max_p_win value disclosed
    # payload round-trips via model_dump
    assert payload == result.model_dump(mode="json")


def test_pnl_backtest_result_no_caps_render_no_crash():
    """When no cap is set, render produces the standard table without extra disclosure."""
    result = _sample_result()  # no max_edge/max_p_win set (defaults to None)
    md, _ = render_pnl_report(result)
    assert "# Betting P/L backtest" in md
    assert "| ALL |" in md
    # No cap disclosure line expected.
    assert "upper" not in md.lower() or "edge cap" not in md.lower()


def test_backtest_pnl_threads_caps_through(httpx_mock):
    """backtest_pnl accepts max_edge/max_p_win and stores them in the result."""
    httpx_mock.add_response(
        url=re.compile(re.escape(HISTORICAL_FORECAST_URL)), json=_hist_fixture()
    )
    httpx_mock.add_response(url=re.compile(re.escape(MESONET_ASOS_URL)), text=_asos_fixture())
    httpx_mock.add_callback(
        _clob_callback, url=re.compile(re.escape(CLOB_PRICES_URL)), is_reusable=True
    )
    with httpx.Client() as client:
        result = backtest_pnl(
            _closed_events(),
            client,
            on_or_after=date(2026, 3, 1),
            leads=(0, 1, 2, 3),
            max_edge=None,
            max_p_win=None,
        )
    assert result is not None
    assert result.max_edge is None
    assert result.max_p_win is None

    # Re-use the same mocked responses for second call.
    httpx_mock.add_response(
        url=re.compile(re.escape(HISTORICAL_FORECAST_URL)), json=_hist_fixture()
    )
    httpx_mock.add_response(url=re.compile(re.escape(MESONET_ASOS_URL)), text=_asos_fixture())
    with httpx.Client() as client:
        result2 = backtest_pnl(
            _closed_events(),
            client,
            on_or_after=date(2026, 3, 1),
            leads=(0, 1, 2, 3),
            max_edge=0.50,
        )
    assert result2 is not None
    assert result2.max_edge == pytest.approx(0.50)
    assert result2.max_p_win is None


# Phase G - intl station skip (#218)


def test_backtest_pnl_skips_intl_stations_before_venue_actuals(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: Any
) -> None:
    """backtest_pnl skips stations with ghcnd_id=None before calling venue_actuals.

    An intl station (ghcnd_id=None) has no ASOS or NCEI proxy, so venue_actuals
    would raise ValueError for it. The guard skips the whole group so
    venue_actuals is never called for an intl station. US stations are unaffected.
    """
    london = INTL_STATIONS["London"]
    assert london.ghcnd_id is None, "pre-condition: London has no NCEI proxy"

    us_target = build_target("NYC", "TMAX", date(2026, 3, 2))
    intl_target = us_target.model_copy(update={"station": london})

    us_market = _market(
        [_bucket("38°F or higher", "above", threshold=38, yes_token_id="d0", no_token_id="d1")]
    )
    intl_market = us_market.model_copy(update={"id": "intl1", "target": intl_target})

    settlement_dt = datetime(2026, 3, 2, 12, tzinfo=UTC)

    # Replace _parse_closed_markets so both markets arrive in the per-station loop.
    monkeypatch.setattr(
        pnl_backtest_mod,
        "_parse_closed_markets",
        lambda *a, **k: [
            (us_market, settlement_dt, {}),
            (intl_market, settlement_dt, {}),
        ],
    )

    # Record every station passed to venue_actuals.
    fetched_stations: list[Any] = []
    original_venue_actuals = pnl_backtest_mod.venue_actuals

    def spy_venue_actuals(station: Any, *args: Any, **kwargs: Any) -> Any:
        fetched_stations.append(station)
        return original_venue_actuals(station, *args, **kwargs)

    monkeypatch.setattr(pnl_backtest_mod, "venue_actuals", spy_venue_actuals)

    # Mock HTTP calls. Both responses are reusable so pre-fix (intl station not
    # yet skipped) extra requests don't cause teardown errors; post-fix only the
    # US station's requests actually fire.
    httpx_mock.add_callback(
        lambda req: httpx.Response(200, json=_hist_fixture()),
        url=re.compile(re.escape(HISTORICAL_FORECAST_URL)),
        is_reusable=True,
    )
    httpx_mock.add_callback(
        lambda req: httpx.Response(200, text=_asos_fixture()),
        url=re.compile(re.escape(MESONET_ASOS_URL)),
        is_reusable=True,
    )
    httpx_mock.add_callback(
        _clob_callback, url=re.compile(re.escape(CLOB_PRICES_URL)), is_reusable=True
    )

    with httpx.Client() as client:
        result = backtest_pnl([], client, on_or_after=date(2026, 3, 1), leads=(0, 1, 2, 3))

    # The intl group must never have reached venue_actuals.
    assert london not in fetched_stations, (
        f"venue_actuals was called for the intl station (not skipped): {fetched_stations}"
    )
    # US station was processed: venue_actuals was called once with the US station.
    assert fetched_stations == [STATIONS["NYC"]]
    # US station contributed markets; intl contributed none.
    assert result is not None
    assert result.n_markets >= 1


# Phase H - tolerant fill fetch (#271): retry transient trades-endpoint
# failures, then degrade to no fills rather than aborting the whole replay.


def _noop_sleep(seconds: float) -> None:
    pass


def test_fetch_fills_tolerant_retries_408_then_succeeds(httpx_mock, capsys):
    """A single 408 is retried and the next attempt's fills are returned."""
    httpx_mock.add_response(url=re.compile(re.escape(TRADES_URL)), status_code=408)
    httpx_mock.add_response(url=re.compile(re.escape(TRADES_URL)), json=_trades_fixture_raw())
    with httpx.Client() as client:
        fills = pnl_backtest_mod._fetch_fills_tolerant("0xcond_d", "d0", client, sleep=_noop_sleep)
    assert len(fills) == 2
    assert len(httpx_mock.get_requests()) == 2
    # Recovery within the retry budget is silent: no degrade warning is printed.
    assert capsys.readouterr().err == ""


def test_fetch_fills_tolerant_two_408s_degrades_to_empty(httpx_mock, capsys):
    """Persistent 408s exhaust the retry budget and degrade to no fills."""
    httpx_mock.add_response(url=re.compile(re.escape(TRADES_URL)), status_code=408)
    httpx_mock.add_response(url=re.compile(re.escape(TRADES_URL)), status_code=408)
    with httpx.Client() as client:
        fills = pnl_backtest_mod._fetch_fills_tolerant("0xcond_d", "d0", client, sleep=_noop_sleep)
    assert fills == []
    assert len(httpx_mock.get_requests()) == 2
    # httpx.HTTPStatusError's own str() spans a continuation line ("For more
    # information check: ..."), so filter to the warning's own prefixed line
    # rather than counting every raw line in stderr.
    warn_lines = [
        line
        for line in capsys.readouterr().err.splitlines()
        if line.startswith("fills unavailable for market")
    ]
    assert len(warn_lines) == 1
    assert "0xcond_d" in warn_lines[0]
    assert "408" in warn_lines[0]


def test_fetch_fills_tolerant_404_raises(httpx_mock):
    """A non-retryable 4xx (contract regression, bad conditionId, IP block) surfaces."""
    httpx_mock.add_response(url=re.compile(re.escape(TRADES_URL)), status_code=404)
    with httpx.Client() as client:
        with pytest.raises(httpx.HTTPStatusError):
            pnl_backtest_mod._fetch_fills_tolerant("0xcond_d", "d0", client, sleep=_noop_sleep)
    # No retry spent on a non-retryable status: exactly one request made.
    assert len(httpx_mock.get_requests()) == 1


def test_fetch_fills_tolerant_read_timeout_degrades_to_empty(httpx_mock):
    """A transport-level failure (httpx.TransportError) also retries then degrades."""
    httpx_mock.add_exception(
        httpx.ReadTimeout("timed out"),
        url=re.compile(re.escape(TRADES_URL)),
        is_reusable=True,
    )
    with httpx.Client() as client:
        fills = pnl_backtest_mod._fetch_fills_tolerant("0xcond_d", "d0", client, sleep=_noop_sleep)
    assert fills == []
    assert len(httpx_mock.get_requests()) == 2


def _closed_events_one_market_persistently_408() -> list[dict[str, Any]]:
    """Both March markets carry conditionIds, but only March 2's is fetchable.

    March 3's "38F or higher" sub-market is keyed to a distinct conditionId
    ("0xcond_e") that the trades mock always 408s, so its fills degrade to []
    while March 2 ("0xcond_d") keeps its real fixture fills.
    """
    events = _closed_events_with_condids()
    for ev in events:
        if ev.get("slug", "") == "highest-temperature-in-nyc-on-march-3-2026":
            for m in ev.get("markets", []):
                tokens = json.loads(m["clobTokenIds"])
                if tokens[0] == "d0":
                    m["conditionId"] = "0xcond_e"
    return events


def _trades_partial_degrade_callback(request: httpx.Request) -> httpx.Response:
    if request.url.params.get("market") == "0xcond_e":
        return httpx.Response(408)
    return httpx.Response(200, json=_trades_fixture_raw())


def test_backtest_pnl_trades_mode_one_market_degrades_other_keeps_fills(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: Any
) -> None:
    """One market's fill fetches persistently 408; the other's succeed.

    The degraded market (March 3, conditionId 0xcond_e) falls all the way
    back to mid pricing (fill_coverage.fills_used contributes 0 for it); the
    healthy market (March 2, conditionId 0xcond_d) keeps its real fills
    exactly as in test_backtest_pnl_trades_mode_uses_fill_as_ask. The replay
    completes rather than aborting.
    """
    monkeypatch.setattr(pnl_backtest_mod, "_FILL_FETCH_BACKOFF_S", 0.0)
    httpx_mock.add_response(
        url=re.compile(re.escape(HISTORICAL_FORECAST_URL)), json=_hist_fixture()
    )
    httpx_mock.add_response(url=re.compile(re.escape(MESONET_ASOS_URL)), text=_asos_fixture())
    httpx_mock.add_callback(
        _clob_callback, url=re.compile(re.escape(CLOB_PRICES_URL)), is_reusable=True
    )
    httpx_mock.add_callback(
        _trades_partial_degrade_callback,
        url=re.compile(re.escape(TRADES_URL)),
        is_reusable=True,
    )
    with httpx.Client() as client:
        result = backtest_pnl(
            _closed_events_one_market_persistently_408(),
            client,
            on_or_after=date(2026, 3, 1),
            leads=(0, 1, 2, 3),
            ask_source="trades",
            calibration_lookup=lambda icao, lead: _full_cal(),
        )
    assert result is not None
    assert result.fill_coverage is not None
    # Two markets, four leads each; the fetch outcome does not change this count.
    assert result.fill_coverage.n_leads == 8
    # Only March 2 (0xcond_d) contributes fill coverage; March 3 degrades to 0.
    assert result.fill_coverage.fills_used == 2
    # March 2's lead-0 NO ask is still the real 0.90 fill; March 3 is all-mid,
    # so only one 0.05 reduction (vs 0.10 in the healthy-both-markets test)
    # applies to the 1.20 mid-mode baseline.
    assert result.overall.total_pnl == pytest.approx(1.15)


def test_backtest_pnl_trades_mode_all_408_degrades_to_mid_totals(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every trades fetch persistently 408s: the whole replay degrades to mid.

    fills_used is 0 everywhere and the total_pnl matches the mid-mode result
    for the same events (1.20), because every ask falls back to mid+spread.
    """
    monkeypatch.setattr(pnl_backtest_mod, "_FILL_FETCH_BACKOFF_S", 0.0)
    httpx_mock.add_response(
        url=re.compile(re.escape(HISTORICAL_FORECAST_URL)), json=_hist_fixture()
    )
    httpx_mock.add_response(url=re.compile(re.escape(MESONET_ASOS_URL)), text=_asos_fixture())
    httpx_mock.add_callback(
        _clob_callback, url=re.compile(re.escape(CLOB_PRICES_URL)), is_reusable=True
    )
    httpx_mock.add_response(
        url=re.compile(re.escape(TRADES_URL)), status_code=408, is_reusable=True
    )
    with httpx.Client() as client:
        result = backtest_pnl(
            _closed_events_with_condids(),
            client,
            on_or_after=date(2026, 3, 1),
            leads=(0, 1, 2, 3),
            ask_source="trades",
            calibration_lookup=lambda icao, lead: _full_cal(),
        )
    assert result is not None
    assert result.fill_coverage is not None
    assert result.fill_coverage.fills_used == 0
    assert result.overall.total_pnl == pytest.approx(1.20)
    # Every degraded fill fetch warns once on stderr with the market conditionId
    # (the fixture's shared "0xcond_d") and the specific token, so the two
    # degraded tokens (d0/d1, the "38F or higher" bucket) are distinguishable
    # without hardcoding how many (market, token) fetches occurred.
    warn_lines = {
        line
        for line in capsys.readouterr().err.splitlines()
        if line.startswith("fills unavailable for market")
    }
    assert warn_lines, "expected at least one fill-degrade warning"
    assert all("0xcond_d" in line for line in warn_lines)
    tokens_seen = {line.split("token ")[1].split(",")[0] for line in warn_lines}
    assert tokens_seen == {"d0", "d1"}
