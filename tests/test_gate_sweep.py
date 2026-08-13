"""TDD for the gate-sweep diagnostic: pure functions over settled_rows()-shaped dicts.

No DB access anywhere in this file (per the module's contract): every row is a
synthetic dict in the shape settled_rows() returns.
"""

from typing import Any

import pytest

from rainmaker.gate_sweep import (
    LIVE_POLICY,
    MAX_EDGE_GRID,
    MIN_EDGE_GRID,
    Policy,
    as_recorded,
    replay_policy,
    run_sweep,
)
from rainmaker.tracking import _wilson_interval, compute_pnl


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
        "actual_value": actual_value,
    }


# (a) --------------------------------------------------------------------


def test_anchors_reconcile_with_track_headline_on_single_era_rows():
    """live (as recorded) and live (replayed) both equal compute_pnl's headline."""
    rows = [
        _row(market_id="m1", run_id="r1", p_win=0.85, edge=0.20, recommended=1, ask=0.60),
        _row(
            market_id="m2",
            run_id="r1",
            bucket="72-73°F",
            p_win=0.90,
            edge=0.15,
            recommended=1,
            ask=0.75,
            actual_value=72.0,
        ),
    ]
    pnl = compute_pnl(None, rows=rows)  # type: ignore[arg-type]
    recorded = as_recorded(rows)
    live = replay_policy(rows, LIVE_POLICY)

    assert recorded["n_bets"] == pnl["n_bets"] == live["n_bets"] == 2
    assert recorded["wins"] == pnl["wins"] == live["wins"]
    assert recorded["losses"] == pnl["losses"] == live["losses"]
    assert recorded["pnl"] == pytest.approx(pnl["total_pnl"])
    assert live["pnl"] == pytest.approx(pnl["total_pnl"])
    assert recorded["roi"] == pytest.approx(pnl["roi"])
    assert live["roi"] == pytest.approx(pnl["roi"])


# (b) --------------------------------------------------------------------


def test_tightening_min_edge_drops_marginal_bet_but_keeps_qualifying_bet():
    """Raising min_edge past a marginal bet's edge drops it from the sweep's bet
    count; a separate, comfortably-qualifying bet in another (market, run) is
    unaffected. Within one (market, run), a stricter threshold can only shrink
    the recommended set (edge >= new_threshold implies edge >= old_threshold),
    so the max-edge survivor never changes as long as it still survives -- this
    checks the two distinct outcomes that tightening can actually produce.
    """
    rows = [
        # m1: sole recommended bucket, edge just above the base (0.05) min_edge.
        _row(market_id="m1", run_id="r1", p_win=0.85, edge=0.06, recommended=1, ask=0.60),
        # m2: two recommended siblings; the higher-edge one is already the pick.
        _row(
            market_id="m2",
            run_id="r1",
            bucket="72-73°F",
            p_win=0.85,
            edge=0.20,
            recommended=1,
            ask=0.50,
            actual_value=72.0,
        ),
        _row(
            market_id="m2",
            run_id="r1",
            bucket="74-75°F",
            side="NO",
            p_win=0.85,
            edge=0.08,
            recommended=1,
            ask=0.20,
            actual_value=72.0,
        ),
    ]
    base = replay_policy(rows, Policy(label="base", min_edge=0.05))
    assert base["n_bets"] == 2  # m1's 0.06 bet and m2's 0.20 pick

    tightened = replay_policy(rows, Policy(label="tight", min_edge=0.10))
    assert tightened["n_bets"] == 1  # m1's marginal 0.06 bet is gone
    assert tightened["staked"] == pytest.approx(0.50)  # only m2's 0.20-edge pick remains


# (c) --------------------------------------------------------------------


def test_loosening_no_floor_admits_sibling_only_when_recommended_row_exists():
    rows = [
        # m1: YES bucket recommended at base (makes m1 sweep-eligible); NO
        # sibling failed the base NO floor (0.75), not edge.
        _row(market_id="m1", run_id="r1", p_win=0.85, edge=0.06, recommended=1, ask=0.60),
        _row(
            market_id="m1",
            run_id="r1",
            bucket="72-73°F",
            side="NO",
            p_win=0.70,
            edge=0.10,
            recommended=0,
            ask=0.20,
            actual_value=72.0,
        ),
        # m2: no row ever recommended -> ineligible regardless of floor loosening.
        _row(
            market_id="m2",
            run_id="r1",
            bucket="80-81°F",
            side="NO",
            p_win=0.70,
            edge=0.10,
            recommended=0,
            ask=0.20,
            actual_value=80.0,
        ),
    ]
    base = replay_policy(rows, Policy(label="base", floor_no=0.75))
    assert base["n_bets"] == 1  # only m1's YES bucket

    loosened = replay_policy(rows, Policy(label="loose", floor_no=0.70))
    assert loosened["n_bets"] == 1  # still only m1: its NO sibling now outranks the YES one
    assert loosened["staked"] == pytest.approx(0.20)  # the NO sibling (edge 0.10) is the new pick


# (d) --------------------------------------------------------------------


def test_ksfo_tmax_row_must_clear_base_plus_station_delta():
    row_fails = _row(
        market_id="m1",
        run_id="r1",
        city="San Francisco",
        variable="TMAX",
        p_win=0.85,
        edge=0.07,  # clears base (0.05) but not base + KSFO/TMAX delta (0.10)
        recommended=1,  # stored recommended from an earlier era, before the delta applied
        ask=0.60,
    )
    replayed = replay_policy([row_fails], LIVE_POLICY)
    assert replayed["n_bets"] == 0

    row_passes = _row(
        market_id="m2",
        run_id="r1",
        city="San Francisco",
        variable="TMAX",
        p_win=0.85,
        edge=0.12,  # clears base + delta (0.10)
        recommended=1,
        ask=0.55,
    )
    replayed_ok = replay_policy([row_passes], LIVE_POLICY)
    assert replayed_ok["n_bets"] == 1


# (e) --------------------------------------------------------------------


def test_lead_zero_exclusion_and_venue_restriction():
    rows = [
        _row(
            market_id="m1",
            run_id="r1",
            p_win=0.85,
            edge=0.20,
            recommended=1,
            ask=0.60,
            settlement_date="2026-05-29",
            started_at="2026-05-29T00:00:00",  # lead 0
        ),
        _row(
            market_id="m2",
            run_id="r1",
            bucket="72-73°F",
            p_win=0.85,
            edge=0.20,
            recommended=1,
            ask=0.55,
            actual_value=72.0,
            venue="kalshi",
            settlement_date="2026-05-31",
            started_at="2026-05-29T00:00:00",  # lead 2
        ),
    ]
    all_leads = replay_policy(rows, Policy(label="all", lead="all"))
    assert all_leads["n_bets"] == 2

    no_lead0 = replay_policy(rows, Policy(label="excl0", lead="exclude_0"))
    assert no_lead0["n_bets"] == 1
    assert no_lead0["staked"] == pytest.approx(0.55)

    polymarket_only = replay_policy(rows, Policy(label="poly", venue="polymarket"))
    assert polymarket_only["n_bets"] == 1
    assert polymarket_only["staked"] == pytest.approx(0.60)

    kalshi_only = replay_policy(rows, Policy(label="kalshi", venue="kalshi"))
    assert kalshi_only["n_bets"] == 1
    assert kalshi_only["staked"] == pytest.approx(0.55)


# (f) --------------------------------------------------------------------


def test_ungradable_bucket_label_is_skipped_and_counted_not_raised():
    rows = [
        _row(
            market_id="m1",
            run_id="r1",
            bucket="inches",  # unparsable: no digits, no matching outcome_spec
            variable="PRCP",
            p_win=0.85,
            edge=0.20,
            recommended=1,
            ask=0.60,
        ),
        _row(
            market_id="m2",
            run_id="r1",
            bucket="72-73°F",
            p_win=0.85,
            edge=0.20,
            recommended=1,
            ask=0.55,
            actual_value=72.0,
        ),
    ]
    result = replay_policy(rows, LIVE_POLICY)
    assert result["n_bets"] == 1  # m2 graded fine
    assert result["skipped"] == 1  # m1's 'inches' label counted, not raised

    recorded = as_recorded(rows)
    assert recorded["n_bets"] == 1
    assert recorded["skipped"] == 1


# (g) --------------------------------------------------------------------


def test_since_filters_post_dedup_by_run_start_string_compare():
    rows = [
        _row(
            market_id="m1",
            run_id="r1",
            p_win=0.85,
            edge=0.20,
            recommended=1,
            ask=0.60,
            started_at="2026-05-01T00:00:00",
        ),
        _row(
            market_id="m2",
            run_id="r2",
            bucket="72-73°F",
            p_win=0.85,
            edge=0.20,
            recommended=1,
            ask=0.55,
            actual_value=72.0,
            started_at="2026-06-01T00:00:00",
        ),
    ]
    full = run_sweep(rows)
    assert full["anchors"]["as_recorded"]["n_bets"] == 2

    since = run_sweep(rows, since="2026-05-15")
    assert since["anchors"]["as_recorded"]["n_bets"] == 1
    assert since["anchors"]["as_recorded"]["staked"] == pytest.approx(0.55)


# (h) --------------------------------------------------------------------


def test_wilson_interval_matches_hand_computed_value():
    rows = [
        _row(
            market_id=f"m{i}",
            run_id="r1",
            p_win=0.85,
            edge=0.20,
            recommended=1,
            ask=0.50,
            actual_value=71.0 if won else 60.0,
        )
        for i, won in enumerate([True, True, True, False])
    ]
    result = replay_policy(rows, LIVE_POLICY)
    assert result["n_bets"] == 4
    assert result["wins"] == 3
    expected_lo, expected_hi = _wilson_interval(3, 4)
    assert result["wilson_lo"] == pytest.approx(expected_lo)
    assert result["wilson_hi"] == pytest.approx(expected_hi)


# (i) --------------------------------------------------------------------


def test_max_edge_cap_flips_pick_to_under_cap_sibling():
    """A cap can create sibling reversal: the higher-edge pick is capped out,
    promoting the next-best under-cap sibling. #336's sub-plan noted this case
    was unreachable for min_edge tightenings; a max_edge cap makes it reachable.
    min_edge is passed explicitly (not the live default) so the test does not
    depend on the sibling #350 package's MIN_EDGE change.
    """
    rows = [
        _row(market_id="m1", run_id="r1", p_win=0.85, edge=0.25, recommended=1, ask=0.50),
        _row(
            market_id="m1",
            run_id="r1",
            bucket="72-73°F",
            p_win=0.85,
            edge=0.10,
            recommended=1,
            ask=0.20,
            actual_value=72.0,
        ),
    ]
    capped = replay_policy(rows, Policy(label="cap 0.20", min_edge=0.01, max_edge=0.20))
    assert capped["n_bets"] == 1
    assert capped["staked"] == pytest.approx(0.20)


def test_max_edge_cap_over_only_recommended_row_produces_no_bet():
    rows = [
        _row(market_id="m2", run_id="r1", p_win=0.85, edge=0.30, recommended=1, ask=0.40),
    ]
    capped = replay_policy(rows, Policy(label="cap 0.20", min_edge=0.01, max_edge=0.20))
    assert capped["n_bets"] == 0


def test_max_edge_cap_applies_to_raw_edge_not_delta_adjusted():
    """KSFO TMAX's station-delta bump raises the effective min_edge floor, but
    the cap compares against the raw stored edge (the #205 backtest-pnl
    --max-edge precedent), so a raw edge of 0.22 is dropped by a 0.20 cap even
    though its delta-relative margin (0.22 - 0.10 effective min_edge) is small.
    """
    row = _row(
        market_id="m1",
        run_id="r1",
        city="San Francisco",
        variable="TMAX",
        p_win=0.85,
        edge=0.22,
        recommended=1,
        ask=0.55,
    )
    capped = replay_policy([row], Policy(label="cap 0.20", min_edge=0.01, max_edge=0.20))
    assert capped["n_bets"] == 0


def test_max_edge_is_a_pure_tightening_not_a_loosening():
    assert Policy(label="cap 0.20", max_edge=0.20).is_loosening is False


def test_max_edge_ofat_none_row_matches_replayed_live_anchor():
    rows = [
        _row(market_id="m1", run_id="r1", p_win=0.85, edge=0.20, recommended=1, ask=0.60),
    ]
    result = run_sweep(rows)
    none_row = next(r for r in result["max_edge"] if r["label"] == "none")
    anchor = result["anchors"]["replayed_live"]
    for key, value in anchor.items():
        if key in ("label", "lower_bound"):
            continue
        assert none_row[key] == value


def test_max_edge_and_combined_grid_shapes():
    rows = [
        _row(market_id="m1", run_id="r1", p_win=0.85, edge=0.20, recommended=1, ask=0.60),
    ]
    result = run_sweep(rows)
    assert len(result["max_edge"]) == len(MAX_EDGE_GRID)
    assert len(result["combined_min_edge_max_edge"]) == len(MIN_EDGE_GRID) * len(MAX_EDGE_GRID)


# Grid shape ---------------------------------------------------------------


def test_run_sweep_builds_the_full_ofat_grid_and_combined_table():
    rows = [
        _row(market_id="m1", run_id="r1", p_win=0.85, edge=0.20, recommended=1, ask=0.60),
    ]
    result = run_sweep(rows)
    assert set(result["anchors"]) == {"as_recorded", "replayed_live"}
    assert len(result["min_edge"]) == len(MIN_EDGE_GRID)
    assert len(result["floor_yes"]) == 3
    assert len(result["floor_no"]) == 4
    assert len(result["lead"]) == 3
    assert len(result["venue"]) == 3
    assert len(result["combined_min_edge_lead0"]) == len(MIN_EDGE_GRID)


def test_loosening_no_floor_grid_row_is_flagged_as_a_lower_bound():
    loose_no_floor = Policy(label="floor_no=0.70", floor_no=0.70)
    assert loose_no_floor.is_loosening is True
    live_row = Policy(label="live")
    assert live_row.is_loosening is False
