"""Polymarket fee-schedule drift guard (#504).

The fee-aware gate (#491) assumes `rate x p x (1 - p)` at
POLYMARKET_TAKER_FEE_RATE. A market whose Gamma fee block differs is excluded
from recommendations, with the reason in the report.
"""

import copy
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest

from rainmaker.config import (
    KALSHI_API_BASE,
    KALSHI_HIGH_SERIES,
    KALSHI_LOW_SERIES,
    POLYMARKET_TAKER_FEE_RATE,
    build_target,
)
from rainmaker.domain import Bucket, Market, PrecipMonthlyMarket
from rainmaker.forecasts.base import ForecastSample, ForecastSet, SourceCoverage
from rainmaker.forecasts.precip import PrecipForecastSet
from rainmaker.kalshi.client import discover_kalshi_markets
from rainmaker.polymarket.client import (
    discover_markets,
    discover_precip_markets,
    fee_schedule_exclusion,
)
from rainmaker.polymarket.precip_markets import parse_precip_event
from rainmaker.ranking.edge import (
    MarketReport,
    RankedOutcome,
    evaluate_market,
    evaluate_precip_market,
)
from rainmaker.report.render import Report, render_markdown, render_terminal

FIXTURES = Path(__file__).parent / "fixtures"
INTL_FIXTURES = [
    "polymarket_intl_london.json",
    "polymarket_intl_paris.json",
    "polymarket_intl_helsinki.json",
    "polymarket_intl_sao_paulo.json",
]
GOOD_SCHEDULE: dict[str, Any] = {
    "exponent": 1,
    "rate": POLYMARKET_TAKER_FEE_RATE,
    "takerOnly": True,
    "rebateRate": 0.25,
}


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def _with_fees(event: dict[str, Any], **schedule_over: Any) -> dict[str, Any]:
    """Deep copy of an event with the fee block injected on every bucket."""
    ev = copy.deepcopy(event)
    for m in ev["markets"]:
        m["feesEnabled"] = True
        m["feeType"] = "weather_fees"
        m["feeSchedule"] = {**GOOD_SCHEDULE, **schedule_over}
    return ev


def _london() -> dict[str, Any]:
    return _load("polymarket_intl_london.json")


def _us_event() -> dict[str, Any]:
    return next(e for e in _load("polymarket_weather_events.json") if "NYC" in e["title"])


# --- A. parse -------------------------------------------------------------


@pytest.mark.parametrize("name", INTL_FIXTURES)
def test_matching_schedule_is_kept(name):
    assert fee_schedule_exclusion(_load(name)) is None


@pytest.mark.parametrize("rate", [0.04, 0.06, 0.0, 0.5])
def test_other_rate_is_excluded_including_lower(rate):
    reason = fee_schedule_exclusion(_with_fees(_london(), rate=rate))
    assert reason is not None
    assert str(rate) in reason


@pytest.mark.parametrize("exponent", [0, 0.5, 2])
def test_exponent_drift_is_excluded_in_either_direction(exponent):
    # Below 1 the real fee exceeds what the gate assumes, the harmful side.
    reason = fee_schedule_exclusion(_with_fees(_london(), exponent=exponent))
    assert reason is not None
    assert "exponent" in reason


def test_taker_only_false_is_excluded():
    reason = fee_schedule_exclusion(_with_fees(_london(), takerOnly=False))
    assert reason is not None
    assert "takerOnly" in reason


def test_us_fixture_without_fee_block_is_excluded():
    reason = fee_schedule_exclusion(_us_event())
    assert reason is not None
    assert "missing" in reason


def test_deleted_or_non_object_schedule_is_excluded():
    deleted = _with_fees(_london())
    for m in deleted["markets"]:
        del m["feeSchedule"]
    assert fee_schedule_exclusion(deleted) is not None
    for junk in ("0.05", 0.05, ["rate"], None):
        ev = _with_fees(_london())
        for m in ev["markets"]:
            m["feeSchedule"] = junk
        assert fee_schedule_exclusion(ev) is not None


def test_schedule_without_rate_is_excluded():
    ev = _with_fees(_london())
    for m in ev["markets"]:
        del m["feeSchedule"]["rate"]
    assert fee_schedule_exclusion(ev) is not None


def test_fees_disabled_is_kept_even_with_a_changed_rate():
    ev = _with_fees(_london(), rate=0.9)
    for m in ev["markets"]:
        m["feesEnabled"] = False
    assert fee_schedule_exclusion(ev) is None
    for m in ev["markets"]:
        del m["feeSchedule"]
    assert fee_schedule_exclusion(ev) is None


def test_one_failing_bucket_excludes_the_market_and_reason_names_it():
    ev = _with_fees(_london())
    ev["markets"][3]["feeSchedule"]["rate"] = 0.07
    ev["markets"][5]["feeSchedule"]["rate"] = 0.07
    reason = fee_schedule_exclusion(ev)
    assert reason is not None
    assert ev["markets"][3]["groupItemTitle"] in reason
    assert "0.07" in reason
    assert "2 of 11" in reason


def test_one_disabled_bucket_does_not_hide_a_failing_one():
    ev = _with_fees(_london())
    ev["markets"][0]["feesEnabled"] = False
    ev["markets"][1]["feeSchedule"]["rate"] = 0.07
    assert fee_schedule_exclusion(ev) is not None


@pytest.mark.parametrize(
    "field,value",
    [
        ("exponent", True),
        ("exponent", "1"),
        ("rate", True),
        ("rate", "0.05"),
        ("rate", float("nan")),
        ("takerOnly", "true"),
        ("takerOnly", 1),
    ],
)
def test_malformed_schedule_types_are_excluded(field, value):
    assert fee_schedule_exclusion(_with_fees(_london(), **{field: value})) is not None


@pytest.mark.parametrize("value", ["true", 1, None])
def test_fees_enabled_must_be_a_real_bool(value):
    ev = _with_fees(_london())
    for m in ev["markets"]:
        m["feesEnabled"] = value
    assert fee_schedule_exclusion(ev) is not None


@pytest.mark.parametrize("markets", [None, [], "x", [None], [1]])
def test_never_raises_on_odd_events(markets):
    assert fee_schedule_exclusion({"markets": markets}) is not None
    assert fee_schedule_exclusion({}) is not None


# --- B. discovery ---------------------------------------------------------


def test_discover_markets_sets_fee_exclusion_and_logs(capsys):
    kept = _london()
    drifted = _with_fees(_london(), rate=0.06)
    drifted["id"] = "drift-1"
    markets = discover_markets([kept, drifted])
    by_id = {m.id: m for m in markets}
    assert by_id[kept["id"]].fee_exclusion is None
    drift_reason = by_id["drift-1"].fee_exclusion
    assert drift_reason is not None
    assert "0.06" in drift_reason
    err = capsys.readouterr().err
    assert "drift-1" in err
    assert "fee" in err


def test_discover_markets_us_without_block_is_excluded():
    (market,) = discover_markets([_us_event()])
    assert market.fee_exclusion is not None


def _precip_event() -> dict[str, Any]:
    return _load("polymarket_precip_monthly_nyc.json")


def test_discover_precip_markets_sets_fee_exclusion(capsys):
    (market,) = discover_precip_markets([_with_fees(_precip_event())])
    assert market.fee_exclusion is None
    (market,) = discover_precip_markets([_with_fees(_precip_event(), exponent=2)])
    assert market.fee_exclusion is not None
    assert "exponent" in market.fee_exclusion
    assert "fee" in capsys.readouterr().err


def test_discover_precip_markets_without_block_is_excluded():
    (market,) = discover_precip_markets([_precip_event()])
    assert market.fee_exclusion is not None


# --- C. gate --------------------------------------------------------------


def _bucket(label, *, lo, hi, best_ask=None, no_ask=None) -> Bucket:
    return Bucket(
        label=label,
        kind="range",
        lo=lo,
        hi=hi,
        threshold=None,
        yes_token_id="t",
        best_ask=best_ask,
        best_bid=None,
        yes_price=0.0,
        no_ask=no_ask,
    )


def _temp_market(fee_exclusion: str | None) -> Market:
    return Market(
        id="m1",
        slug="s",
        title="Highest temperature in NYC on May 31?",
        target=build_target("NYC", "TMAX", date(2026, 5, 31)),
        buckets=[
            _bucket("70-71°F", lo=70, hi=71, best_ask=0.20),
            _bucket("90-91°F", lo=90, hi=91, no_ask=0.30),
        ],
        fee_exclusion=fee_exclusion,
    )


def _temp_forecast(values: tuple[int, ...] = (69, 70, 71, 72)) -> ForecastSet:
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
    return ForecastSet(
        target=build_target("NYC", "TMAX", date(2026, 5, 31)),
        samples=samples,
        coverage=[
            SourceCoverage(source=s, ok=True, n_samples=len(values)) for s in ("nws", "open-meteo")
        ],
    )


def _eval_temp(fee_exclusion: str | None) -> MarketReport:
    return evaluate_market(
        _temp_market(fee_exclusion),
        _temp_forecast(),
        floor=0.0,
        min_sources=2,
        min_sigma=1.5,
        min_edge=-1.0,
        require_calibration=False,
    )


def _precip_market(fee_exclusion: str | None) -> PrecipMonthlyMarket:
    return parse_precip_event(_precip_event()).model_copy(update={"fee_exclusion": fee_exclusion})


def _eval_precip(fee_exclusion: str | None) -> MarketReport:
    market = _precip_market(fee_exclusion)
    fs = PrecipForecastSet(
        target=market.target,
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
    return evaluate_precip_market(
        market, fs, floor=0.0, min_sources=2, min_edge=-1.0, var_floor=0.01
    )


def _numbers(report: MarketReport) -> list[tuple[str, str, float, float, float]]:
    return [(o.bucket_label, o.side, o.p_win, o.edge, o.net_edge) for o in report.outcomes]


@pytest.mark.parametrize("evaluate", [_eval_temp, _eval_precip])
def test_fee_exclusion_forces_every_recommendation_off(evaluate):
    base = evaluate(None)
    assert {o.side for o in base.outcomes if o.recommended} == {"YES", "NO"}
    assert base.fee_exclusion is None

    excluded = evaluate("fee drift: test reason")
    assert excluded.fee_exclusion == "fee drift: test reason"
    assert not any(o.recommended for o in excluded.outcomes)
    # forecast and advisory display stay intact: only `recommended` moves
    assert _numbers(excluded) == _numbers(base)
    assert excluded.mu == base.mu and excluded.sigma == base.sigma


def test_fee_exclusion_survives_the_empty_forecast_early_return():
    report = evaluate_market(
        _temp_market("fee drift: test reason"),
        _temp_forecast(()),
        floor=0.0,
        min_sources=2,
        min_sigma=1.5,
        min_edge=-1.0,
    )
    assert report.fee_exclusion == "fee drift: test reason"


# --- D. render ------------------------------------------------------------


def _rendered_report(fee_exclusion: str | None, policy_exclusion: str | None = None) -> Report:
    market_report = MarketReport(
        market_id="m1",
        title="Highest temperature in NYC on May 31?",
        city="NYC",
        station="KLGA",
        variable="TMAX",
        settlement_date=date(2026, 5, 31),
        mu=70.5,
        sigma=2.0,
        n_sources=2,
        calibrated="full",
        coverage=[SourceCoverage(source="nws", ok=True, n_samples=1)],
        outcomes=[
            RankedOutcome(
                bucket_label="70-71°F",
                p_win=0.93,
                best_ask=0.40,
                edge=0.53,
                net_edge=0.52,
                recommended=False,
            )
        ],
        excluded_no_ask=[],
        policy_exclusion=policy_exclusion,
        fee_exclusion=fee_exclusion,
    )
    return Report(run_date=date(2026, 5, 31), markets=[market_report])


def test_render_shows_fee_exclusion_in_terminal_and_markdown():
    report = _rendered_report("fee schedule rate 0.06 differs")
    terminal = render_terminal(report)
    markdown = render_markdown(report)
    assert "EXCLUDED from recommendations: fee schedule rate 0.06 differs" in terminal
    assert "- EXCLUDED from recommendations: fee schedule rate 0.06 differs" in markdown


def test_render_shows_policy_and_fee_exclusion_lines_together():
    report = _rendered_report("fee reason", policy_exclusion="policy reason")
    terminal = render_terminal(report)
    markdown = render_markdown(report)
    for line in (
        "EXCLUDED from recommendations: policy reason",
        "EXCLUDED from recommendations: fee reason",
    ):
        assert line in terminal
        assert line in markdown


def test_render_omits_fee_exclusion_when_unset():
    report = _rendered_report(None)
    assert "EXCLUDED from recommendations" not in render_terminal(report)
    assert "EXCLUDED from recommendations" not in render_markdown(report)


# --- E. kalshi ------------------------------------------------------------


def test_kalshi_markets_are_not_fee_excluded(httpx_mock):
    high = _load("kalshi_high_temp_nyc.json")
    empty = {"cursor": "", "markets": []}
    url = re.compile(re.escape(KALSHI_API_BASE))
    for city in KALSHI_HIGH_SERIES:
        httpx_mock.add_response(url=url, json=high if city == "NYC" else empty)
    for _ in KALSHI_LOW_SERIES:
        httpx_mock.add_response(url=url, json=empty)
    with httpx.Client() as client:
        markets = discover_kalshi_markets(client)
    assert markets
    assert all(m.venue == "kalshi" and m.fee_exclusion is None for m in markets)


# --- F. old rows ----------------------------------------------------------


def test_rows_without_fee_exclusion_still_validate():
    raw = _temp_market(None).model_dump(mode="json")
    raw.pop("fee_exclusion")
    assert Market.model_validate(raw).fee_exclusion is None
    praw = _precip_market(None).model_dump(mode="json")
    praw.pop("fee_exclusion")
    assert PrecipMonthlyMarket.model_validate(praw).fee_exclusion is None
