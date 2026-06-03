from datetime import date

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
