import json
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from rainmaker.backfill import (
    HISTORICAL_FORECAST_URL,
    NCEI_URL,
    fetch_historical_forecasts,
    fetch_historical_samples,
)
from rainmaker.config import MIN_SIGMA_F, OPENMETEO_MODELS, STATIONS, build_target
from rainmaker.domain import Bucket, Market
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
from rainmaker.polymarket.prices import CLOB_PRICES_URL, PricePoint
from rainmaker.polymarket.trades import TRADES_URL
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


def _actuals_fixture() -> list[dict[str, Any]]:
    return json.loads((FIXTURES / "ncei_actuals_klga.json").read_text())


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
    httpx_mock.add_response(url=re.compile(re.escape(NCEI_URL)), json=_actuals_fixture())
    httpx_mock.add_callback(
        _clob_callback, url=re.compile(re.escape(CLOB_PRICES_URL)), is_reusable=True
    )
    with httpx.Client() as client:
        result = backtest_pnl(
            _closed_events(), client, on_or_after=date(2026, 3, 1), leads=(0, 1, 2, 3)
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

    The trades fixture has:
    - lead 0: fill at ts 1772452750 (50s before settlement 1772452800) -> price 0.11
    - lead 1: fill at ts 1772366350 (50s before lead-1 target 1772366400) -> price 0.12
    - lead 2, lead 3: no fills -> fallback to mid (0.15 from CLOB history)

    Both the mid path and the trades path should produce bets, but the ask differs:
    mid path: 0.15; trades path for leads 0,1: 0.11 / 0.12 (the fills).
    """
    httpx_mock.add_response(
        url=re.compile(re.escape(HISTORICAL_FORECAST_URL)), json=_hist_fixture()
    )
    httpx_mock.add_response(url=re.compile(re.escape(NCEI_URL)), json=_actuals_fixture())
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
        )
    assert result is not None
    assert result.ask_source == "trades"
    assert result.fill_coverage is not None
    # two markets, four leads each = 8 total lead-market combinations
    assert result.fill_coverage.n_leads == 8
    # leads 0 and 1 have fills in the trades fixture (per market)
    assert result.fill_coverage.fills_used == 4  # 2 markets * 2 leads with fills


def test_backtest_pnl_mid_mode_no_fill_coverage(httpx_mock):
    """Mid mode (the default) leaves fill_coverage as None."""
    httpx_mock.add_response(
        url=re.compile(re.escape(HISTORICAL_FORECAST_URL)), json=_hist_fixture()
    )
    httpx_mock.add_response(url=re.compile(re.escape(NCEI_URL)), json=_actuals_fixture())
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
                lead=0, n_bets=2, wins=2, losses=0, total_pnl=0.30,
                roi=0.176, win_rate=1.0, mean_edge=0.12,
            ),
        ],
        overall=LeadPnl(
            lead=-1, n_bets=2, wins=2, losses=0, total_pnl=0.30,
            roi=0.176, win_rate=1.0, mean_edge=0.12,
        ),
    )
    md, _payload = render_pnl_report(result)
    lowered = md.lower()
    assert "trades" in lowered or "fill" in lowered
    # coverage should appear
    assert "4" in md and "8" in md
