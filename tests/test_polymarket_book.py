import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from rainmaker.domain import Bucket
from rainmaker.polymarket.book import (
    CLOB_BOOK_URL,
    BookLevel,
    OrderBook,
    book_token_id,
    fetch_book,
    walk_asks,
)
from rainmaker.polymarket.markets import parse_market


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


# --- client (fixture-backed) ---

FIXTURES = Path(__file__).parent / "fixtures"
BOOK_URL = re.compile(re.escape(CLOB_BOOK_URL))
YES_TOKEN = "110481645994240177023362846492885669398895286263695488725150432539409027726065"
NO_TOKEN = "87208398688223991403533909229193642947664277381716455849307964132422500373150"


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def _nyc_7071_bucket() -> Bucket:
    events = json.loads((FIXTURES / "polymarket_weather_events.json").read_text())
    event = next(e for e in events if e["id"] == "533147")
    return next(b for b in parse_market(event).buckets if b.label == "70-71°F")


def test_fetch_book_parses_yes_fixture_best_first(httpx_mock):
    httpx_mock.add_response(url=BOOK_URL, json=_fixture("clob_book_nyc_7071_yes.json"))
    with httpx.Client() as client:
        book = fetch_book(YES_TOKEN, client)
    assert httpx_mock.get_requests()[0].url.params["token_id"] == YES_TOKEN
    assert book.asset_id == YES_TOKEN
    assert isinstance(book.timestamp_ms, int) and book.timestamp_ms == 1793647809952
    assert all(isinstance(lv.price, float) for lv in book.bids + book.asks)
    assert [lv.price for lv in book.bids] == [0.996, 0.995, 0.99]
    assert [(lv.price, lv.size) for lv in book.asks] == [(0.999, 300.0)]


def test_no_side_is_priced_from_the_no_token_book(httpx_mock):
    bucket = _nyc_7071_bucket()
    token = book_token_id(bucket, "NO")
    assert token == bucket.no_token_id == NO_TOKEN
    httpx_mock.add_response(url=BOOK_URL, json=_fixture("clob_book_nyc_7071_no.json"))
    with httpx.Client() as client:
        book = fetch_book(token, client)
    assert httpx_mock.get_requests()[0].url.params["token_id"] == NO_TOKEN
    fill = walk_asks(book, 5.0)
    assert fill.token_id == NO_TOKEN
    # 1.0 USDC buys 250 shares at 0.004, the other 4.0 buys 800 at 0.005.
    assert fill.avg_price == pytest.approx(5 / 1050)
    assert fill.avg_price != pytest.approx(1 - bucket.best_bid)
    assert fill.sufficient


def test_book_token_id_yes_returns_yes_token():
    bucket = _nyc_7071_bucket()
    assert book_token_id(bucket, "YES") == bucket.yes_token_id == YES_TOKEN


def test_book_token_id_empty_no_token_raises():
    bucket = _nyc_7071_bucket().model_copy(update={"no_token_id": ""})
    with pytest.raises(ValueError):
        book_token_id(bucket, "NO")


def test_fetch_book_asset_id_mismatch_raises(httpx_mock):
    httpx_mock.add_response(url=BOOK_URL, json=_fixture("clob_book_nyc_7071_yes.json"))
    with httpx.Client() as client, pytest.raises(ValueError):
        fetch_book(NO_TOKEN, client)


def test_fetch_book_404_raises_http_status_error(httpx_mock):
    body = {"error": "No orderbook exists for the requested token id"}
    httpx_mock.add_response(url=BOOK_URL, status_code=404, json=body)
    with httpx.Client() as client, pytest.raises(httpx.HTTPStatusError):
        fetch_book(YES_TOKEN, client)


def test_empty_book_fixture_walks_to_insufficient_fill(httpx_mock):
    httpx_mock.add_response(url=BOOK_URL, json=_fixture("clob_book_empty.json"))
    with httpx.Client() as client:
        book = fetch_book(YES_TOKEN, client)
    assert book.bids == [] and book.asks == []
    fill = walk_asks(book, 5.0)
    assert fill.shares == 0 and fill.avg_price is None and not fill.sufficient


def test_fetch_book_malformed_level_raises(httpx_mock):
    data = _fixture("clob_book_nyc_7071_yes.json")
    data["asks"] = [{"price": "abc", "size": "10"}]
    httpx_mock.add_response(url=BOOK_URL, json=data)
    with httpx.Client() as client, pytest.raises(ValueError):
        fetch_book(YES_TOKEN, client)
