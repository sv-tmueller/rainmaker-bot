import math
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from rainmaker.store.db import READ_METER, connect, init_schema
from rainmaker.store.query import (
    new_exposure_for_day,
    open_exposure_by_outcome,
    total_open_exposure,
)
from rainmaker.store.record import record_paper_order

T0 = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def conn():
    c = connect(":memory:")
    init_schema(c)
    c.execute("INSERT INTO runs (id, started_at, status) VALUES ('r1', 't', 'ok')")
    for market_id in ("m1", "m2"):
        c.execute(
            "INSERT INTO markets (id, city, variable, settlement_date) "
            "VALUES (?, 'NYC', 'TMAX', '2026-07-02')",
            (market_id,),
        )
    c.commit()
    yield c
    c.close()


def _order(conn, **overrides):
    kwargs = {
        "run_id": "r1",
        "market_id": "m1",
        "outcome": "80-81°F",
        "side": "YES",
        "token_id": "tok-1",
        "status": "filled",
        "stake_usdc": 10.0,
        "fill_price": 0.40,
        "shares": 25.0,
        "fee_usdc": 0.5,
        "p_win": 0.6,
        "edge": 0.2,
        "quoted_ask": 0.38,
        "limit_price": 0.45,
        "book_snapshot": '{"asks": []}',
        "created_at": T0,
    }
    kwargs.update(overrides)
    record_paper_order(conn, **kwargs)


def _settle(conn, market_id):
    conn.execute(
        "INSERT INTO outcomes (market_id, actual_value, settled_at) VALUES (?, 80.0, 't')",
        (market_id,),
    )
    conn.commit()


def _count(conn):
    return conn.execute("SELECT count(*) AS n FROM paper_orders").fetchone()["n"]


def test_record_paper_order_round_trips_every_column(conn):
    _order(conn, created_at=datetime(2026, 7, 1, 14, 0, tzinfo=timezone(timedelta(hours=2))))
    row = dict(conn.execute("SELECT * FROM paper_orders").fetchone())
    assert row.pop("id") == 1
    assert row == {
        "run_id": "r1",
        "market_id": "m1",
        "outcome": "80-81°F",
        "side": "YES",
        "token_id": "tok-1",
        "status": "filled",
        "stake_usdc": 10.0,
        "fill_price": 0.40,
        "shares": 25.0,
        "fee_usdc": 0.5,
        "p_win": 0.6,
        "edge": 0.2,
        "quoted_ask": 0.38,
        "limit_price": 0.45,
        "book_snapshot": '{"asks": []}',
        "created_at": "2026-07-01T12:00:00+00:00",
        "won": None,
        "pnl_usdc": None,
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "pending"},
        {"side": "yes"},
        {"status": "filled", "fill_price": None},
        {"status": "partial", "shares": 0.0},
        {"created_at": datetime(2026, 7, 1, 12, 0)},
        {"fill_price": math.nan},
        {"fill_price": math.inf},
        {"fill_price": 0.0},
        {"fill_price": -0.4},
        {"status": "no_fill", "fill_price": math.nan},
        {"shares": math.nan},
        {"shares": math.inf},
        {"status": "no_fill", "fill_price": None, "shares": math.nan},
        {"status": "no_fill", "fill_price": None, "shares": -1.0},
        {"fee_usdc": math.nan},
        {"fee_usdc": -0.5},
        {"fee_usdc": math.inf},
    ],
)
def test_record_paper_order_rejects_values_that_would_read_as_zero_exposure(conn, overrides):
    with pytest.raises(ValueError):
        _order(conn, **overrides)
    assert _count(conn) == 0


def test_no_fill_may_have_no_price_and_no_shares(conn):
    _order(conn, status="no_fill", fill_price=None, shares=0.0, fee_usdc=None)
    assert _count(conn) == 1


def test_open_exposure_counts_filled_and_partial_with_fee(conn):
    _order(conn, status="filled", shares=25.0, fill_price=0.40, fee_usdc=0.5)  # 10.5
    _order(conn, status="partial", outcome="82-83°F", shares=10.0, fill_price=0.30, fee_usdc=0.2)
    assert open_exposure_by_outcome(conn) == {
        ("m1", "80-81°F"): pytest.approx(10.5),
        ("m1", "82-83°F"): pytest.approx(3.2),
    }


def test_open_exposure_treats_null_fee_as_zero(conn):
    _order(conn, fee_usdc=None)
    assert open_exposure_by_outcome(conn) == {("m1", "80-81°F"): pytest.approx(10.0)}


def test_open_exposure_ignores_no_fill_even_when_it_carries_shares(conn):
    # min_order_size case: the walked shares are recorded but nothing was bought.
    _order(conn, status="no_fill", shares=3.0, fill_price=0.40)
    assert open_exposure_by_outcome(conn) == {}


def test_open_exposure_drops_settled_markets(conn):
    _order(conn, market_id="m1")
    _order(conn, market_id="m2")
    _settle(conn, "m1")
    assert open_exposure_by_outcome(conn) == {("m2", "80-81°F"): pytest.approx(10.5)}


def test_open_exposure_adds_yes_and_no_on_one_outcome_under_one_key(conn):
    _order(conn, side="YES", fee_usdc=0.0)  # 10.0
    _order(conn, side="NO", token_id="tok-2", shares=5.0, fill_price=0.20, fee_usdc=0.0)  # 1.0
    assert open_exposure_by_outcome(conn) == {("m1", "80-81°F"): pytest.approx(11.0)}


def test_open_exposure_keys_two_outcomes_of_one_market_separately(conn):
    _order(conn, outcome="80-81°F")
    _order(conn, outcome="82-83°F")
    assert set(open_exposure_by_outcome(conn)) == {("m1", "80-81°F"), ("m1", "82-83°F")}


def test_open_exposure_is_empty_on_an_empty_table(conn):
    assert open_exposure_by_outcome(conn) == {}


def test_total_open_exposure_sums_the_per_outcome_dict(conn):
    _order(conn, outcome="80-81°F")
    _order(conn, outcome="82-83°F", market_id="m2")
    assert total_open_exposure(conn) == pytest.approx(sum(open_exposure_by_outcome(conn).values()))
    assert total_open_exposure(conn) == pytest.approx(21.0)


def test_total_open_exposure_is_zero_on_an_empty_table(conn):
    assert total_open_exposure(conn) == 0.0


def test_new_exposure_for_day_counts_the_utc_day_including_settled_markets(conn):
    _order(conn, created_at=datetime(2026, 7, 1, 0, 0, 0, tzinfo=UTC))
    _order(conn, market_id="m2", created_at=datetime(2026, 7, 1, 23, 59, 59, tzinfo=UTC))
    _settle(conn, "m2")
    assert new_exposure_for_day(conn, date(2026, 7, 1)) == pytest.approx(21.0)


def test_new_exposure_for_day_excludes_the_neighbouring_days(conn):
    _order(conn, created_at=datetime(2026, 6, 30, 23, 59, 59, tzinfo=UTC))
    _order(conn, created_at=datetime(2026, 7, 2, 0, 0, 0, tzinfo=UTC))
    assert new_exposure_for_day(conn, date(2026, 7, 1)) == 0.0


def test_new_exposure_for_day_excludes_no_fill(conn):
    _order(conn, status="no_fill", shares=3.0, fill_price=0.40)
    assert new_exposure_for_day(conn, date(2026, 7, 1)) == 0.0


def test_new_exposure_for_day_uses_the_utc_day_of_an_offset_timestamp(conn):
    # 23:30 at -05:00 on Jul 1 is 04:30Z on Jul 2.
    _order(conn, created_at=datetime(2026, 7, 1, 23, 30, tzinfo=timezone(timedelta(hours=-5))))
    assert new_exposure_for_day(conn, date(2026, 7, 1)) == 0.0
    assert new_exposure_for_day(conn, date(2026, 7, 2)) == pytest.approx(10.5)


def test_new_exposure_for_day_is_zero_on_an_empty_day(conn):
    assert new_exposure_for_day(conn, date(2026, 7, 1)) == 0.0


def test_exposure_queries_never_read_the_book_snapshot(conn):
    _order(conn, book_snapshot="x" * 1_000_000)
    READ_METER.reset()
    open_exposure_by_outcome(conn)
    total_open_exposure(conn)
    new_exposure_for_day(conn, date(2026, 7, 1))
    assert READ_METER.bytes < 1_000
