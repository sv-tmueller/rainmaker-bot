"""Flat-stake sizing and risk caps: synthetic inputs with known answers (#478)."""

import itertools
import math
from collections import Counter

import pytest

from rainmaker.trading.sizing import (
    Candidate,
    SizingResult,
    size_orders,
)


def cand(market: str, outcome: str, edge: float, side: str = "YES") -> Candidate:
    return Candidate(market_id=market, outcome=outcome, side=side, edge=edge)  # type: ignore[arg-type]


def run(candidates, **overrides) -> SizingResult:
    kwargs = {
        "open_positions": frozenset(),
        "open_exposure_usdc": 0.0,
        "new_exposure_today_usdc": 0.0,
        "halted": False,
    }
    kwargs.update(overrides)
    return size_orders(candidates, **kwargs)


def order_keys(result: SizingResult) -> list[tuple[str, str, str]]:
    return [(o.candidate.market_id, o.candidate.outcome, o.candidate.side) for o in result.orders]


def blocked_reasons(result: SizingResult) -> list[str]:
    return [b.reason for b in result.blocked]


# --- Ordering ---


def test_empty_candidates_give_empty_result():
    result = run([])
    assert result.orders == ()
    assert result.blocked == ()


def test_orders_sorted_by_edge_descending_at_default_stake():
    result = run([cand("m1", "a", 0.05), cand("m2", "b", 0.30), cand("m3", "c", 0.10)])
    assert [o.candidate.edge for o in result.orders] == [0.30, 0.10, 0.05]
    assert [o.stake_usdc for o in result.orders] == [5.0, 5.0, 5.0]
    assert result.blocked == ()


def test_edge_tie_breaks_on_market_then_outcome_then_side():
    result = run(
        [
            cand("m2", "a", 0.1, "YES"),
            cand("m1", "b", 0.1, "YES"),
            cand("m1", "a", 0.1, "YES"),
            cand("m1", "a", 0.1, "NO"),
        ],
        open_positions=frozenset(),
        max_positions_per_outcome=1,
    )
    # (m1, a) arrives YES first, then NO: side sorts alphabetically, so NO wins.
    assert order_keys(result) == [("m1", "a", "NO"), ("m1", "b", "YES"), ("m2", "a", "YES")]
    assert [(b.candidate.side, b.reason) for b in result.blocked] == [("YES", "duplicate_in_batch")]


def test_every_permutation_gives_identical_result():
    cands = [
        cand("m1", "a", 0.2),
        cand("m1", "b", 0.2),
        cand("m2", "a", 0.2, "NO"),
        cand("m2", "a", 0.1, "YES"),
        cand("m3", "c", 0.3, "YES"),
        cand("m3", "c", 0.3, "NO"),
    ]
    results = {
        run(list(p), max_new_exposure_per_day_usdc=10.0) for p in itertools.permutations(cands)
    }
    assert len(results) == 1


# --- Duplicates ---


def test_open_position_blocks_both_sides_but_not_other_outcomes():
    result = run(
        [cand("m1", "o1", 0.3, "YES"), cand("m1", "o1", 0.2, "NO"), cand("m1", "o2", 0.1)],
        open_positions=frozenset({("m1", "o1")}),
    )
    assert order_keys(result) == [("m1", "o2", "YES")]
    assert blocked_reasons(result) == ["open_position", "open_position"]


def test_same_outcome_twice_in_batch_keeps_higher_edge():
    result = run([cand("m1", "o1", 0.1), cand("m1", "o1", 0.4)])
    assert [o.candidate.edge for o in result.orders] == [0.4]
    assert [(b.candidate.edge, b.reason) for b in result.blocked] == [(0.1, "duplicate_in_batch")]


def test_open_position_takes_precedence_over_open_cap():
    result = run(
        [cand("m1", "o1", 0.3)],
        open_positions=frozenset({("m1", "o1")}),
        open_exposure_usdc=150.0,
    )
    assert result.orders == ()
    assert blocked_reasons(result) == ["open_position"]


def test_duplicate_in_batch_takes_precedence_over_open_cap():
    # Open 145 leaves room for one 10 stake (cap 150). The second candidate on
    # the same outcome must be a duplicate, not an open_cap block.
    result = run(
        [cand("m1", "A", 0.5, "YES"), cand("m1", "A", 0.4, "NO")],
        open_exposure_usdc=145.0,
    )
    assert order_keys(result) == [("m1", "A", "YES")]
    assert blocked_reasons(result) == ["duplicate_in_batch"]


def test_cap_blocked_candidate_is_not_counted_as_planned():
    result = run(
        [cand("m1", "o1", 0.5, "YES"), cand("m1", "o1", 0.4, "NO")],
        open_exposure_usdc=150.0,
    )
    assert result.orders == ()
    assert blocked_reasons(result) == ["open_cap", "open_cap"]


def test_blocked_duplicate_uses_no_cap_room():
    result = run(
        [cand("m1", "A", 0.5), cand("m1", "A", 0.4), cand("m1", "B", 0.3)],
        max_new_exposure_per_day_usdc=10.0,
    )
    assert order_keys(result) == [("m1", "A", "YES"), ("m1", "B", "YES")]
    assert blocked_reasons(result) == ["duplicate_in_batch"]


# --- Caps ---


def _many(n: int) -> list[Candidate]:
    return [cand(f"m{i:02d}", "o", 0.5 - i * 0.01) for i in range(n)]


def test_daily_cap_binds_after_room_is_used():
    result = run(_many(5), new_exposure_today_usdc=40.0)
    assert len(result.orders) == 2
    assert blocked_reasons(result) == ["daily_cap"] * 3


def test_cap_boundary_is_inclusive():
    result = run(_many(3), new_exposure_today_usdc=45.0)
    assert len(result.orders) == 1
    assert blocked_reasons(result) == ["daily_cap"] * 2


def test_partial_room_skips_instead_of_shrinking():
    result = run(_many(3), new_exposure_today_usdc=47.0)
    assert result.orders == ()
    assert blocked_reasons(result) == ["daily_cap"] * 3


def test_open_cap_binds_after_room_is_used():
    result = run(_many(5), open_exposure_usdc=140.0)
    assert len(result.orders) == 2
    assert blocked_reasons(result) == ["open_cap"] * 3


def test_open_cap_takes_precedence_over_daily_cap():
    result = run(_many(2), open_exposure_usdc=150.0, new_exposure_today_usdc=50.0)
    assert result.orders == ()
    assert blocked_reasons(result) == ["open_cap"] * 2


def test_exposure_already_over_cap_blocks_everything():
    result = run(_many(3), open_exposure_usdc=160.0)
    assert result.orders == ()
    assert blocked_reasons(result) == ["open_cap"] * 3


def test_caps_bind_in_edge_order():
    low_first = [cand("m1", "low", 0.01), cand("m2", "high", 0.9), cand("m3", "mid", 0.5)]
    result = run(low_first, new_exposure_today_usdc=40.0)
    assert [o.candidate.outcome for o in result.orders] == ["high", "mid"]
    assert [b.candidate.outcome for b in result.blocked] == ["low"]


# --- Kill switch and accounting ---


def test_halted_blocks_every_candidate_in_sorted_order():
    result = run([cand("m1", "a", 0.1), cand("m2", "b", 0.3), cand("m3", "c", 0.2)], halted=True)
    assert result.orders == ()
    assert [b.candidate.market_id for b in result.blocked] == ["m2", "m3", "m1"]
    assert blocked_reasons(result) == ["halted"] * 3


def test_every_candidate_lands_in_orders_or_blocked():
    cands = [
        cand("m1", "a", 0.9),
        cand("m1", "a", 0.8, "NO"),
        cand("m2", "b", 0.7),
        cand("m3", "c", 0.6),
        cand("m4", "d", 0.5),
    ]
    result = run(cands, open_positions=frozenset({("m2", "b")}), new_exposure_today_usdc=45.0)
    assert len(result.orders) + len(result.blocked) == len(cands)
    assert Counter(blocked_reasons(result)) == {
        "duplicate_in_batch": 1,
        "open_position": 1,
        "daily_cap": 2,
    }


# --- Defaults and validation ---


def test_defaults_bind_to_config_constants():
    result = run(_many(11))
    assert len(result.orders) == 10
    assert all(o.stake_usdc == 5.0 for o in result.orders)
    assert blocked_reasons(result) == ["daily_cap"]


@pytest.mark.parametrize("stake", [0.0, -5.0])
def test_non_positive_stake_raises(stake):
    with pytest.raises(ValueError):
        run([cand("m1", "a", 0.1)], stake_usdc=stake)


@pytest.mark.parametrize("edge", [math.nan, math.inf, -math.inf])
def test_non_finite_edge_raises(edge):
    with pytest.raises(ValueError):
        run([cand("m1", "a", edge)])


_FLOAT_INPUTS = [
    "stake_usdc",
    "open_exposure_usdc",
    "new_exposure_today_usdc",
    "max_new_exposure_per_day_usdc",
    "max_open_exposure_usdc",
]


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
@pytest.mark.parametrize("name", _FLOAT_INPUTS)
def test_non_finite_float_input_raises(name, bad):
    with pytest.raises(ValueError, match=name):
        run([cand("m1", "a", 0.1)], **{name: bad})


@pytest.mark.parametrize(
    "name",
    [
        "open_exposure_usdc",
        "new_exposure_today_usdc",
        "max_new_exposure_per_day_usdc",
        "max_open_exposure_usdc",
    ],
)
def test_negative_exposure_or_cap_raises(name):
    with pytest.raises(ValueError, match=name):
        run([cand("m1", "a", 0.1)], **{name: -1000.0})


@pytest.mark.parametrize("positions", [0, -1])
def test_max_positions_below_one_raises(positions):
    with pytest.raises(ValueError, match="max_positions_per_outcome"):
        run([cand("m1", "a", 0.1)], max_positions_per_outcome=positions)


@pytest.mark.parametrize("positions", [2, 3])
def test_max_positions_above_one_raises(positions):
    with pytest.raises(ValueError, match="spec decision and a count query"):
        run([cand("m1", "a", 0.1)], max_positions_per_outcome=positions)


@pytest.mark.parametrize("positions", [1.0, True, None])
def test_max_positions_must_be_int(positions):
    with pytest.raises(ValueError, match="max_positions_per_outcome"):
        run([cand("m1", "a", 0.1)], max_positions_per_outcome=positions)


@pytest.mark.parametrize(
    "overrides",
    [
        {"stake_usdc": math.nan},
        {"open_exposure_usdc": math.nan},
        {"new_exposure_today_usdc": -1.0},
        {"max_open_exposure_usdc": math.inf},
        {"max_positions_per_outcome": 0},
    ],
)
def test_invalid_input_raises_even_when_halted(overrides):
    with pytest.raises(ValueError):
        run([cand("m1", "a", 0.1)], halted=True, **overrides)


def test_zero_exposure_and_zero_caps_are_valid():
    result = run([cand("m1", "a", 0.1)], max_open_exposure_usdc=0.0)
    assert result.orders == ()
    assert blocked_reasons(result) == ["open_cap"]
