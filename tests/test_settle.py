import re
from datetime import date

import httpx
import pytest

from rainmaker.backfill import NCEI_URL
from rainmaker.settle import run_settlement
from rainmaker.store.db import connect, init_schema
from rainmaker.store.query import unsettled_markets
from rainmaker.store.record import record_outcome


def _market(conn, market_id, city, variable, settlement_date):
    conn.execute(
        "INSERT INTO markets (id, city, variable, settlement_date) VALUES (?, ?, ?, ?)",
        (market_id, city, variable, settlement_date),
    )
    conn.commit()


def test_unsettled_markets_returns_past_markets_without_outcomes():
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "past", "NYC", "TMAX", "2026-05-30")
    _market(conn, "future", "NYC", "TMAX", "2030-01-01")
    _market(conn, "settled", "NYC", "TMAX", "2026-05-29")
    record_outcome(conn, "settled", 71.0, "2026-05-31T00:00:00Z")
    rows = unsettled_markets(conn, date(2026, 6, 3))
    conn.close()
    assert [r["market_id"] for r in rows] == ["past"]


def test_record_outcome_is_idempotent_upsert():
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "m1", "NYC", "TMAX", "2026-05-30")
    record_outcome(conn, "m1", 71.0, "2026-05-31T00:00:00Z")
    record_outcome(conn, "m1", 71.0, "2026-05-31T00:00:00Z")
    rows = conn.execute("SELECT actual_value FROM outcomes WHERE market_id = ?", ("m1",)).fetchall()
    conn.close()
    assert len(rows) == 1 and rows[0]["actual_value"] == 71.0


def test_run_settlement_records_outcome(httpx_mock):
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "m1", "NYC", "TMAX", "2026-05-30")
    httpx_mock.add_response(
        url=re.compile(re.escape(NCEI_URL)),
        json=[{"DATE": "2026-05-30", "STATION": "USW00014732", "TMAX": "71"}],
    )
    with httpx.Client() as client:
        settled, waiting = run_settlement(conn, client, date(2026, 6, 3), "2026-06-03T00:00:00Z")
    row = conn.execute(
        "SELECT actual_value, settled_at FROM outcomes WHERE market_id = ?", ("m1",)
    ).fetchone()
    conn.close()
    assert (settled, waiting) == (1, 0)
    assert row["actual_value"] == 71.0
    assert row["settled_at"] == "2026-06-03T00:00:00Z"


def test_run_settlement_skips_when_no_data(httpx_mock):
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "m1", "NYC", "TMAX", "2026-05-30")
    httpx_mock.add_response(url=re.compile(re.escape(NCEI_URL)), json=[])
    with httpx.Client() as client:
        settled, waiting = run_settlement(conn, client, date(2026, 6, 3), "2026-06-03T00:00:00Z")
    n = conn.execute("SELECT count(*) AS n FROM outcomes").fetchone()["n"]
    conn.close()
    assert (settled, waiting) == (0, 1)
    assert n == 0


def test_run_settlement_idempotent(httpx_mock):
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "m1", "NYC", "TMAX", "2026-05-30")
    httpx_mock.add_response(
        url=re.compile(re.escape(NCEI_URL)),
        json=[{"DATE": "2026-05-30", "TMAX": "71"}],
    )
    with httpx.Client() as client:
        run_settlement(conn, client, date(2026, 6, 3), "2026-06-03T00:00:00Z")
        # m1 now has an outcome, so the second pass settles nothing and makes no request
        settled, waiting = run_settlement(conn, client, date(2026, 6, 3), "2026-06-03T00:00:00Z")
    conn.close()
    assert (settled, waiting) == (0, 0)


def test_run_settlement_skips_unknown_city():
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "m1", "Atlantis", "TMAX", "2026-05-30")
    with httpx.Client() as client:
        settled, waiting = run_settlement(conn, client, date(2026, 6, 3), "2026-06-03T00:00:00Z")
    conn.close()
    assert (settled, waiting) == (0, 0)


def test_run_settlement_settles_precip_via_gsom(httpx_mock):
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "p1", "NYC", "PRCP", "2026-06-30")
    httpx_mock.add_response(
        url=re.compile(re.escape(NCEI_URL)),
        json=[{"DATE": "2026-06", "STATION": "USW00094728", "PRCP": "4.10"}],
    )
    with httpx.Client() as client:
        settled, waiting = run_settlement(conn, client, date(2026, 7, 3), "2026-07-03T00:00:00Z")
    row = conn.execute("SELECT actual_value FROM outcomes WHERE market_id = ?", ("p1",)).fetchone()
    conn.close()
    assert (settled, waiting) == (1, 0)
    assert row["actual_value"] == pytest.approx(4.10)


def test_run_settlement_precip_waits_when_unpublished(httpx_mock):
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "p1", "NYC", "PRCP", "2026-06-30")
    httpx_mock.add_response(url=re.compile(re.escape(NCEI_URL)), json=[])
    with httpx.Client() as client:
        settled, waiting = run_settlement(conn, client, date(2026, 7, 3), "2026-07-03T00:00:00Z")
    n = conn.execute("SELECT count(*) AS n FROM outcomes").fetchone()["n"]
    conn.close()
    assert (settled, waiting) == (0, 1)
    assert n == 0


def test_run_settlement_skips_unknown_precip_city():
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "p1", "Atlantis", "PRCP", "2026-06-30")
    with httpx.Client() as client:
        settled, waiting = run_settlement(conn, client, date(2026, 7, 3), "2026-07-03T00:00:00Z")
    conn.close()
    assert (settled, waiting) == (0, 0)
