import json
import re
from datetime import date
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
from rainmaker.pnl_backtest import (
    forecast_set_from_samples,
    market_at_lead,
)
from rainmaker.polymarket.markets import Bucket, Market
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
