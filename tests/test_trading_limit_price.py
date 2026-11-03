"""Fee-aware limit price: the highest ask whose net edge still meets the floor."""

import itertools
import math

import pytest

from rainmaker.fees import taker_fee_per_share
from rainmaker.polymarket.book import BookLevel, OrderBook
from rainmaker.trading.pricing import limit_price, price_paper_order

P_WINS = (0.55, 0.7, 0.8, 0.95, 0.99)
FLOORS = (0.12, 0.17)
RATES = (0.0, 0.05)
GRID = list(itertools.product(P_WINS, FLOORS, RATES))
TICK = 0.001


def _net_edge(p_win: float, ask: float, rate: float) -> float:
    return p_win - ask - taker_fee_per_share(ask, rate)


@pytest.mark.parametrize(("p_win", "floor", "rate"), GRID)
def test_net_edge_at_the_limit_equals_the_floor(p_win, floor, rate):
    limit = limit_price(p_win=p_win, floor=floor, fee_rate=rate)
    assert limit is not None
    assert _net_edge(p_win, limit, rate) == pytest.approx(floor, abs=1e-12)


@pytest.mark.parametrize(("p_win", "floor", "rate"), GRID)
def test_one_tick_either_side_of_the_limit_is_on_the_correct_side_of_the_floor(p_win, floor, rate):
    limit = limit_price(p_win=p_win, floor=floor, fee_rate=rate)
    assert limit is not None
    assert _net_edge(p_win, limit - TICK, rate) > floor
    assert _net_edge(p_win, limit + TICK, rate) < floor


@pytest.mark.parametrize(("p_win", "floor"), itertools.product(P_WINS, FLOORS))
def test_zero_fee_rate_gives_the_gross_limit(p_win, floor):
    assert limit_price(p_win=p_win, floor=floor, fee_rate=0.0) == pytest.approx(p_win - floor)


def test_fee_aware_limit_is_below_the_gross_limit():
    limit = limit_price(p_win=0.80, floor=0.12, fee_rate=0.05)
    assert limit == pytest.approx(0.66893, abs=1e-5)
    assert limit < 0.80 - 0.12


@pytest.mark.parametrize(("p_win", "floor"), [(0.12, 0.12), (0.10, 0.12), (1.0, 0.0), (0.0, 0.0)])
def test_no_valid_limit_returns_none(p_win, floor):
    assert limit_price(p_win=p_win, floor=floor, fee_rate=0.05) is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"p_win": math.nan, "floor": 0.12, "fee_rate": 0.05},
        {"p_win": 0.8, "floor": math.inf, "fee_rate": 0.05},
        {"p_win": 0.8, "floor": 0.12, "fee_rate": math.nan},
        {"p_win": 1.1, "floor": 0.12, "fee_rate": 0.05},
        {"p_win": -0.1, "floor": 0.12, "fee_rate": 0.05},
        {"p_win": 0.8, "floor": -0.01, "fee_rate": 0.05},
        {"p_win": 0.8, "floor": 0.12, "fee_rate": -0.01},
        {"p_win": 0.8, "floor": 0.12, "fee_rate": 1.0},
    ],
)
def test_invalid_input_raises(kwargs):
    with pytest.raises(ValueError):
        limit_price(**kwargs)


def _book(ask: float) -> OrderBook:
    return OrderBook(
        asset_id="tok",
        market="0xm",
        timestamp_ms=1,
        min_order_size=5.0,
        tick_size=0.001,
        bids=[],
        asks=[BookLevel(price=ask, size=1000.0)],
    )


@pytest.mark.parametrize(("p_win", "floor", "rate"), GRID)
def test_price_paper_order_accepts_the_returned_limit(p_win, floor, rate):
    limit = limit_price(p_win=p_win, floor=floor, fee_rate=rate)
    assert limit is not None
    fill = price_paper_order(_book(limit), stake_usdc=5.0, limit_price=limit, fee_rate=rate)
    assert fill.status == "filled"


@pytest.mark.parametrize(("p_win", "floor", "rate"), GRID)
def test_limit_is_at_least_every_ask_that_passes_the_floor(p_win, floor, rate):
    limit = limit_price(p_win=p_win, floor=floor, fee_rate=rate)
    assert limit is not None
    for step in range(1, 1000):
        ask = step / 1000
        if _net_edge(p_win, ask, rate) >= floor:
            assert limit >= ask - 1e-12


def test_fee_helper_is_shared_with_the_ranking_module():
    from rainmaker.ranking import edge

    assert taker_fee_per_share is edge.taker_fee_per_share
