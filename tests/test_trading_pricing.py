import inspect
import json
import math
from pathlib import Path
from typing import Any, get_args

import pytest

from rainmaker.polymarket.book import BookLevel, OrderBook, walk_asks
from rainmaker.store.record import PaperOrderStatus
from rainmaker.trading.pricing import FillStatus, PaperFill, price_paper_order

FIXTURES = Path(__file__).parent / "fixtures"
f = 0.05  # local test fee rate; the real constant lives in config (#491)


def _book(asks: list[tuple[float, float]], min_order_size: float = 5.0) -> OrderBook:
    return OrderBook(
        asset_id="tok",
        market="0xabc",
        timestamp_ms=1,
        min_order_size=min_order_size,
        tick_size=0.001,
        bids=[],
        asks=[BookLevel(price=p, size=s) for p, s in asks],
    )


def _fixture_book(name: str) -> OrderBook:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text())

    def level(raw: dict[str, str]) -> BookLevel:
        return BookLevel(price=float(raw["price"]), size=float(raw["size"]))

    return OrderBook(
        asset_id=data["asset_id"],
        market=data["market"],
        timestamp_ms=int(data["timestamp"]),
        min_order_size=float(data["min_order_size"]),
        tick_size=float(data["tick_size"]),
        bids=[level(lv) for lv in data["bids"]],
        asks=[level(lv) for lv in data["asks"]],
    )


def _price(book: OrderBook, stake: float, limit: float = 0.99, fee: float = f) -> PaperFill:
    return price_paper_order(book, stake_usdc=stake, limit_price=limit, fee_rate=fee)


# --- walk ---


def test_single_level_filled():
    fill = _price(_book([(0.50, 100)]), 5.125, limit=0.60)
    assert fill.status == "filled"
    assert fill.shares == pytest.approx(10.0)
    assert fill.spent_usdc == pytest.approx(5.0)
    assert fill.fee_usdc == pytest.approx(0.125)
    assert fill.avg_price == pytest.approx(0.5)


def test_multi_level_filled():
    fill = _price(_book([(0.40, 10), (0.50, 100)]), 9.245, limit=0.60)
    assert fill.status == "filled"
    assert fill.shares == pytest.approx(20.0)
    assert fill.spent_usdc == pytest.approx(9.0)
    assert fill.fee_usdc == pytest.approx(0.245)
    assert fill.avg_price == pytest.approx(0.45)


def test_insufficient_depth_is_partial():
    fill = _price(_book([(0.40, 10), (0.50, 20)]), 20.0)
    assert fill.status == "partial"
    assert fill.shares == pytest.approx(30.0)
    assert fill.spent_usdc == pytest.approx(14.0)
    assert fill.fee_usdc == pytest.approx(0.37)


def test_limit_truncates_the_walk():
    fill = _price(_book([(0.40, 10), (0.50, 20)]), 20.0, limit=0.45)
    assert fill.status == "partial"
    assert fill.shares == pytest.approx(10.0)
    assert fill.spent_usdc == pytest.approx(4.0)
    assert fill.fee_usdc == pytest.approx(0.12)


def test_limit_is_inclusive():
    book = _book([(0.40, 10), (0.50, 100)])
    at_limit = _price(book, 9.245, limit=0.50)
    above = _price(book, 9.245, limit=0.60)
    assert at_limit == above
    assert at_limit.status == "filled"
    assert at_limit.shares == pytest.approx(20.0)


def test_limit_computed_by_subtraction_still_takes_the_level():
    limit = 0.565 - 0.12  # 0.44499999999999995, one ulp under 0.445
    assert limit < 0.445
    fill = _price(_book([(0.445, 100)]), 5.0, limit=limit)
    assert fill.status == "filled"
    assert fill.shares == pytest.approx(5.0 / (0.445 * (1 + f * 0.555)))


def test_nothing_at_or_under_limit_is_no_fill():
    fill = _price(_book([(0.50, 100)]), 5.0, limit=0.40)
    assert fill == PaperFill("no_fill", 0.0, 0.0, 0.0, None)


def test_empty_book_is_no_fill():
    fill = _price(_book([]), 5.0)
    assert fill == PaperFill("no_fill", 0.0, 0.0, 0.0, None)


# --- minimum order size ---


def test_below_minimum_is_no_fill_and_keeps_walk_numbers():
    fill = _price(_book([(0.50, 4)]), 5.0)
    assert fill.status == "no_fill"
    assert fill.shares == pytest.approx(4.0)
    assert fill.spent_usdc == pytest.approx(2.0)
    assert fill.fee_usdc == pytest.approx(0.05)
    assert fill.avg_price == pytest.approx(0.5)


def test_exactly_at_minimum_is_not_no_fill():
    fill = _price(_book([(0.50, 5)]), 5.0)
    assert fill.status == "partial"
    assert fill.shares == pytest.approx(5.0)


def test_full_fill_under_minimum_is_no_fill():
    fill = _price(_book([(0.50, 100)]), 1.025)
    assert fill.shares == pytest.approx(2.0)
    assert fill.status == "no_fill"


# --- cross-checks ---


def test_zero_fee_matches_walk_asks():
    book = _book([(0.40, 10), (0.50, 20), (0.70, 50)])
    for stake in (3.0, 9.0, 20.0, 500.0):
        gross = walk_asks(book, stake)
        fill = _price(book, stake, fee=0.0)
        assert fill.shares == pytest.approx(gross.shares)
        assert fill.spent_usdc == pytest.approx(gross.spent_usdc)
        assert fill.fee_usdc == 0.0
        assert fill.avg_price == pytest.approx(gross.avg_price)


def test_exact_depth_is_filled_despite_float_drift():
    asks = [(0.1, 30), (0.2, 30), (0.7, 30)]
    stake = sum(p * (1 + f * (1 - p)) * s for p, s in asks)
    fill = _price(_book(asks), stake)
    assert fill.status == "filled"
    assert fill.shares == pytest.approx(90.0)


@pytest.mark.parametrize("fee", [0.0, 0.05, 0.5])
def test_fill_plus_fee_never_exceeds_stake(fee):
    books = [
        _book([(0.40, 10), (0.50, 20), (0.70, 50)]),
        _book([(0.01, 1000), (0.99, 1000)]),
        _book([(0.333, 7), (0.667, 3)]),
    ]
    for book in books:
        for stake in (0.5, 1.0, 5.0, 7.77, 25.0, 123.456, 10_000.0):
            for limit in (0.05, 0.45, 0.67, 0.99):
                fill = _price(book, stake, limit=limit, fee=fee)
                assert fill.spent_usdc + fill.fee_usdc <= stake + 1e-9
                assert fill.spent_usdc >= 0 and fill.fee_usdc >= 0


# --- fixtures ---


def test_no_fixture_filled_within_limit():
    fill = _price(_fixture_book("clob_book_nyc_7071_no.json"), 5.0, limit=0.02)
    assert fill.status == "filled"
    assert fill.spent_usdc == pytest.approx(4.76299, abs=1e-4)
    assert fill.fee_usdc == pytest.approx(0.23701, abs=1e-4)
    assert fill.spent_usdc + fill.fee_usdc == pytest.approx(5.0)


def test_yes_fixture_ask_above_limit_is_no_fill():
    fill = _price(_fixture_book("clob_book_nyc_7071_yes.json"), 5.0, limit=0.99)
    assert fill.status == "no_fill"
    assert fill.avg_price is None


def test_empty_fixture_is_no_fill():
    fill = _price(_fixture_book("clob_book_empty.json"), 5.0)
    assert fill.status == "no_fill"
    assert fill.shares == 0.0


# --- validation and contract ---

_NAN = float("nan")
_INF = float("inf")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"stake_usdc": 0.0},
        {"stake_usdc": -1.0},
        {"stake_usdc": _NAN},
        {"stake_usdc": _INF},
        {"limit_price": 0.0},
        {"limit_price": 1.0},
        {"limit_price": -0.1},
        {"limit_price": _NAN},
        {"limit_price": _INF},
        {"fee_rate": -0.01},
        {"fee_rate": 1.0},
        {"fee_rate": _NAN},
        {"fee_rate": _INF},
    ],
)
@pytest.mark.parametrize("book", [_book([(0.5, 100)]), _book([])])
def test_invalid_inputs_raise_before_the_walk(book, kwargs):
    args = {"stake_usdc": 5.0, "limit_price": 0.6, "fee_rate": f} | kwargs
    with pytest.raises(ValueError):
        price_paper_order(book, **args)


def test_float_arguments_are_keyword_only_without_defaults():
    params = list(inspect.signature(price_paper_order).parameters.values())
    assert params[0].name == "book"
    assert params[0].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    for p in params[1:]:
        assert p.kind is inspect.Parameter.KEYWORD_ONLY
        assert p.default is inspect.Parameter.empty
    assert [p.name for p in params[1:]] == ["stake_usdc", "limit_price", "fee_rate"]


def test_paper_fill_is_frozen():
    fill = _price(_book([(0.5, 100)]), 5.0)
    with pytest.raises(AttributeError):
        fill.shares = 1.0  # type: ignore[misc]


def test_fill_status_matches_store_paper_order_status():
    assert get_args(FillStatus) == get_args(PaperOrderStatus)


def test_avg_price_is_none_only_without_shares():
    assert _price(_book([]), 5.0).avg_price is None
    avg = _price(_book([(0.5, 100)]), 5.0).avg_price
    assert avg is not None and math.isclose(avg, 0.5)
