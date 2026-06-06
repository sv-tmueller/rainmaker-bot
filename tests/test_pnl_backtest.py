import json
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from rainmaker.backfill import (
    HISTORICAL_FORECAST_URL,
    fetch_historical_forecasts,
    fetch_historical_samples,
)
from rainmaker.config import MIN_SIGMA_F, OPENMETEO_MODELS, STATIONS, build_target
from rainmaker.forecasts.base import ForecastSample, ForecastSet, SourceCoverage
from rainmaker.pnl_backtest import (
    Bet,
    forecast_set_from_samples,
    market_at_lead,
    replay_market,
)
from rainmaker.polymarket.markets import Bucket, Market
from rainmaker.polymarket.prices import PricePoint
from rainmaker.probability.distribution import fit_gaussian

FIXTURES = Path(__file__).parent / "fixtures"
KLGA = STATIONS["NYC"]


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
    # Prices per lead steer which bucket is the best-edge bet at each lead.
    histories = {
        "y1": [
            PricePoint(t=x, p=0.20),
            PricePoint(t=x - day, p=0.20),
            PricePoint(t=x - 2 * day, p=0.04),
            PricePoint(t=x - 3 * day, p=0.04),
        ],
        "y2": [
            PricePoint(t=x, p=0.10),
            PricePoint(t=x - day, p=0.04),
            PricePoint(t=x - 2 * day, p=0.20),
            PricePoint(t=x - 3 * day, p=0.20),
        ],
    }
    bets = replay_market(
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
