"""TDD for the venue-decomposition diagnostic: pure functions over
settled_rows()-shaped dicts.

No DB access anywhere in this file (per the module's contract): every row is a
synthetic dict in the shape settled_rows() returns, plus the implied_prob field
added alongside ask.
"""

from typing import Any

import pytest

from rainmaker.tracking import compute_pnl
from rainmaker.venue_decomp import (
    decompose_by_city_venue,
    reproject_at_implied,
    run_venue_decomp,
)


def _row(
    *,
    market_id: str,
    run_id: str,
    bucket: str = "70-71°F",
    side: str | None = "YES",
    p_win: float,
    edge: float | None,
    recommended: bool | int,
    variable: str = "TMAX",
    venue: str = "polymarket",
    outcome_spec: str | None = None,
    city: str = "NYC",
    settlement_date: str = "2026-05-30",
    started_at: str = "2026-05-29T00:00:00",
    ask: float = 0.40,
    implied_prob: float | None = 0.40,
    actual_value: float = 71.0,
) -> dict[str, Any]:
    return {
        "market_id": market_id,
        "run_id": run_id,
        "bucket": bucket,
        "side": side,
        "p_win": p_win,
        "edge": edge,
        "recommended": recommended,
        "variable": variable,
        "venue": venue,
        "outcome_spec": outcome_spec,
        "city": city,
        "settlement_date": settlement_date,
        "started_at": started_at,
        "ask": ask,
        "implied_prob": implied_prob,
        "actual_value": actual_value,
    }


# (a) city x venue decomposition ------------------------------------------


def test_decompose_groups_by_city_and_venue():
    """Three distinct (city, venue) pairs produce three groups, each scored
    independently with the correct bet count."""
    rows = [
        _row(
            market_id="m1",
            run_id="r1",
            city="NYC",
            venue="polymarket",
            p_win=0.85,
            edge=0.20,
            recommended=1,
            ask=0.60,
        ),
        _row(
            market_id="m2",
            run_id="r1",
            city="NYC",
            venue="kalshi",
            p_win=0.80,
            edge=0.15,
            recommended=1,
            ask=0.60,
        ),
        _row(
            market_id="m3",
            run_id="r1",
            city="Chicago",
            venue="polymarket",
            p_win=0.90,
            edge=0.10,
            recommended=1,
            ask=0.75,
            actual_value=72.0,
            bucket="72-73°F",
        ),
    ]
    result = decompose_by_city_venue(rows)
    assert len(result) == 3
    by_key = {(r["city"], r["venue"]): r for r in result}
    assert by_key[("NYC", "polymarket")]["n_bets"] == 1
    assert by_key[("NYC", "kalshi")]["n_bets"] == 1
    assert by_key[("Chicago", "polymarket")]["n_bets"] == 1


def test_decompose_totals_reconcile_with_compute_pnl():
    """Summing every city x venue group's n_bets, wins, pnl reproduces
    compute_pnl's headline over the same rows."""
    rows = [
        _row(
            market_id="m1",
            run_id="r1",
            city="NYC",
            venue="polymarket",
            p_win=0.85,
            edge=0.20,
            recommended=1,
            ask=0.60,
        ),
        _row(
            market_id="m2",
            run_id="r1",
            city="NYC",
            venue="kalshi",
            p_win=0.80,
            edge=0.15,
            recommended=1,
            ask=0.60,
            actual_value=70.0,
        ),
        _row(
            market_id="m3",
            run_id="r1",
            city="Miami",
            venue="polymarket",
            p_win=0.90,
            edge=0.10,
            recommended=1,
            ask=0.75,
            actual_value=72.0,
            bucket="72-73°F",
        ),
        _row(
            market_id="m4",
            run_id="r1",
            city="Miami",
            venue="kalshi",
            p_win=0.70,
            edge=0.05,
            recommended=1,
            ask=0.55,
            actual_value=70.0,
        ),
    ]
    pnl = compute_pnl(None, rows=rows)  # type: ignore[arg-type]
    result = decompose_by_city_venue(rows)
    assert sum(g["n_bets"] for g in result) == pnl["n_bets"]
    assert sum(g["wins"] for g in result) == pnl["wins"]
    assert sum(g["pnl"] for g in result) == pytest.approx(pnl["total_pnl"])


def test_decompose_picks_best_edge_bet_per_market_run():
    """Multiple recommended buckets on one (market, run) collapse to the
    highest-edge bet, same as compute_pnl's _best_per_market_run."""
    rows = [
        _row(
            market_id="m1",
            run_id="r1",
            city="NYC",
            venue="kalshi",
            bucket="70-71°F",
            p_win=0.80,
            edge=0.15,
            recommended=1,
            ask=0.60,
        ),
        _row(
            market_id="m1",
            run_id="r1",
            city="NYC",
            venue="kalshi",
            bucket="72-73°F",
            p_win=0.70,
            edge=0.05,
            recommended=1,
            ask=0.60,
            actual_value=72.0,
        ),
    ]
    result = decompose_by_city_venue(rows)
    assert len(result) == 1
    assert result[0]["n_bets"] == 1  # not 2


# (b) price-proxy reprojection --------------------------------------------


def test_reproject_at_implied_matches_ask_when_equal():
    """When implied_prob == ask for every row, the two scorings agree."""
    rows = [
        _row(
            market_id="m1",
            run_id="r1",
            venue="kalshi",
            p_win=0.85,
            edge=0.20,
            recommended=1,
            ask=0.60,
            implied_prob=0.60,
        ),
        _row(
            market_id="m2",
            run_id="r1",
            venue="kalshi",
            p_win=0.80,
            edge=0.15,
            recommended=1,
            ask=0.55,
            implied_prob=0.55,
            actual_value=70.0,
        ),
    ]
    result = reproject_at_implied(rows)
    kalshi = result["kalshi"]
    assert kalshi["at_ask"]["pnl"] == pytest.approx(kalshi["at_implied"]["pnl"])
    assert kalshi["at_ask"]["roi"] == pytest.approx(kalshi["at_implied"]["roi"])


def test_reproject_shrinks_edge_when_implied_below_ask():
    """Kalshi rows where implied_prob (last-trade/mid) sits below the best ask:
    repricing at implied cuts the win payout and the edge, lowering ROI."""
    rows = [
        # Winning YES bet: ask 0.60, implied 0.55 -> at_ask wins 0.40, at_impl wins 0.45
        _row(
            market_id="m1",
            run_id="r1",
            venue="kalshi",
            p_win=0.85,
            edge=0.20,
            recommended=1,
            ask=0.60,
            implied_prob=0.55,
        ),
    ]
    result = reproject_at_implied(rows)
    kalshi = result["kalshi"]
    # At implied 0.55 the win pays 1 - 0.55 = 0.45; at ask 0.60 it pays 0.40.
    assert kalshi["at_ask"]["pnl"] == pytest.approx(0.40)
    assert kalshi["at_implied"]["pnl"] == pytest.approx(0.45)
    # But staked is lower at implied, so ROI is HIGHER here (one-win sample).
    # The point of the diagnostic is the delta, not the direction in a 1-bet toy.


def test_reproject_reports_both_venues_and_aggregate():
    """The reprojection covers polymarket, kalshi, and the all-venue aggregate."""
    rows = [
        _row(
            market_id="m1",
            run_id="r1",
            venue="polymarket",
            p_win=0.85,
            edge=0.20,
            recommended=1,
            ask=0.60,
            implied_prob=0.58,
        ),
        _row(
            market_id="m2",
            run_id="r1",
            venue="kalshi",
            p_win=0.80,
            edge=0.15,
            recommended=1,
            ask=0.60,
            implied_prob=0.50,
            actual_value=70.0,
        ),
    ]
    result = reproject_at_implied(rows)
    assert set(result.keys()) == {"polymarket", "kalshi", "all"}
    for entry in result.values():
        assert "at_ask" in entry
        assert "at_implied" in entry


def test_reproject_skips_rows_with_null_implied_prob():
    """A row whose implied_prob is NULL is skipped and counted, not crashed."""
    rows = [
        _row(
            market_id="m1",
            run_id="r1",
            venue="kalshi",
            p_win=0.85,
            edge=0.20,
            recommended=1,
            ask=0.60,
            implied_prob=None,
        ),
        _row(
            market_id="m2",
            run_id="r1",
            venue="kalshi",
            p_win=0.80,
            edge=0.15,
            recommended=1,
            ask=0.55,
            implied_prob=0.55,
            actual_value=70.0,
        ),
    ]
    result = reproject_at_implied(rows)
    kalshi = result["kalshi"]
    assert kalshi["at_implied"]["n_bets"] == 1
    assert kalshi["at_implied"]["skipped"] == 1


# (c) run_venue_decomp wiring ---------------------------------------------


def test_run_venue_decomp_returns_both_sections():
    """The top-level result carries the city x venue table and the price-proxy
    reprojection."""
    rows = [
        _row(
            market_id="m1",
            run_id="r1",
            city="NYC",
            venue="polymarket",
            p_win=0.85,
            edge=0.20,
            recommended=1,
            ask=0.60,
            implied_prob=0.58,
        ),
        _row(
            market_id="m2",
            run_id="r1",
            city="NYC",
            venue="kalshi",
            p_win=0.80,
            edge=0.15,
            recommended=1,
            ask=0.60,
            implied_prob=0.50,
            actual_value=70.0,
        ),
    ]
    result = run_venue_decomp(rows)
    assert "city_venue" in result
    assert "price_proxy" in result
    assert len(result["city_venue"]) == 2


def test_since_filter_restricts_rows():
    """since drops rows whose run started before that date, same as gate_sweep."""
    rows = [
        _row(
            market_id="m1",
            run_id="r1",
            city="NYC",
            venue="polymarket",
            p_win=0.85,
            edge=0.20,
            recommended=1,
            started_at="2026-05-01T00:00:00",
        ),
        _row(
            market_id="m2",
            run_id="r2",
            city="NYC",
            venue="kalshi",
            p_win=0.80,
            edge=0.15,
            recommended=1,
            started_at="2026-06-01T00:00:00",
            actual_value=70.0,
        ),
    ]
    full = run_venue_decomp(rows)
    since = run_venue_decomp(rows, since="2026-05-15")
    assert len(full["city_venue"]) == 2
    assert len(since["city_venue"]) == 1
    assert since["city_venue"][0]["venue"] == "kalshi"


def test_empty_rows_return_empty_tables():
    """No rows -> empty decomposition, zero-scored reprojection (no crash)."""
    result = run_venue_decomp([])
    assert result["city_venue"] == []
    assert result["price_proxy"]["all"]["at_ask"]["n_bets"] == 0
    assert result["price_proxy"]["all"]["at_implied"]["n_bets"] == 0
