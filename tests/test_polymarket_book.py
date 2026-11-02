import pytest

from rainmaker.polymarket.book import BookLevel, OrderBook, walk_asks


def _book(
    asks: list[tuple[float, float]], bids: list[tuple[float, float]] | None = None
) -> OrderBook:
    return OrderBook(
        asset_id="tok",
        market="0xabc",
        timestamp_ms=1,
        bids=[BookLevel(price=p, size=s) for p, s in bids or []],
        asks=[BookLevel(price=p, size=s) for p, s in asks],
    )


def test_walk_single_level():
    fill = walk_asks(_book([(0.40, 100)]), 10.0)
    assert fill.shares == pytest.approx(25.0)
    assert fill.spent_usdc == pytest.approx(10.0)
    assert fill.avg_price == pytest.approx(0.40)
    assert fill.sufficient
    assert fill.token_id == "tok"
    assert fill.stake_usdc == 10.0


def test_walk_multi_level_spans_levels():
    # 0.40 x 10 costs 4.0, the remaining 5.0 buys 10 shares at 0.50.
    fill = walk_asks(_book([(0.40, 10), (0.50, 20)]), 9.0)
    assert fill.shares == pytest.approx(20.0)
    assert fill.spent_usdc == pytest.approx(9.0)
    assert fill.avg_price == pytest.approx(0.45)
    assert fill.sufficient


def test_walk_exact_depth_is_sufficient_despite_float_drift():
    asks = [(0.1, 3), (0.2, 3), (0.7, 3)]
    stake = 0.1 * 3 + 0.2 * 3 + 0.7 * 3
    fill = walk_asks(_book(asks), stake)
    assert fill.sufficient
    assert fill.shares == pytest.approx(9.0)


def test_walk_insufficient_depth_returns_partial_fill():
    fill = walk_asks(_book([(0.40, 10), (0.50, 20)]), 20.0)
    assert fill.shares == pytest.approx(30.0)
    assert fill.spent_usdc == pytest.approx(14.0)
    assert fill.avg_price == pytest.approx(14.0 / 30.0)
    assert not fill.sufficient
    assert fill.stake_usdc == 20.0


def test_walk_empty_asks_is_empty_fill():
    fill = walk_asks(_book([]), 5.0)
    assert fill.shares == 0
    assert fill.spent_usdc == 0
    assert fill.avg_price is None
    assert not fill.sufficient


def test_walk_ignores_bids():
    fill = walk_asks(_book([], bids=[(0.9, 1000), (0.8, 1000)]), 5.0)
    assert fill.shares == 0
    assert fill.avg_price is None
    assert not fill.sufficient


def test_walk_fills_cheapest_ask_first_when_given_descending():
    asks = [(0.50, 20), (0.40, 10)]
    fill = walk_asks(_book(asks), 9.0)
    assert fill.avg_price == pytest.approx(0.45)


def test_book_sorts_asks_ascending_and_bids_descending():
    book = _book([(0.5, 1), (0.4, 1)], bids=[(0.1, 1), (0.3, 1), (0.2, 1)])
    assert [lv.price for lv in book.asks] == [0.4, 0.5]
    assert [lv.price for lv in book.bids] == [0.3, 0.2, 0.1]


@pytest.mark.parametrize("stake", [0.0, -1.0])
def test_walk_rejects_non_positive_stake(stake):
    with pytest.raises(ValueError):
        walk_asks(_book([(0.4, 10)]), stake)
