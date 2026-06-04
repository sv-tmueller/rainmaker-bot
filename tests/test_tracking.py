import pytest

from rainmaker.store.db import connect, init_schema
from rainmaker.tracking import compute_calibration, compute_pnl


def _setup(conn):
    init_schema(conn)
    conn.execute("INSERT INTO runs (id, started_at, status) VALUES (?, ?, ?)", ("r1", "t", "ok"))
    conn.execute(
        "INSERT INTO markets (id, city, variable, settlement_date) VALUES (?, ?, ?, ?)",
        ("m1", "NYC", "TMAX", "2026-05-30"),
    )
    for outcome, price in (("70-71°F", 0.40), ("72-73°F", 0.30)):
        conn.execute(
            "INSERT INTO prices (run_id, market_id, outcome, price, implied_prob, captured_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("r1", "m1", outcome, price, price, "t"),
        )
    for bucket, p_win in (("70-71°F", 0.93), ("72-73°F", 0.50)):
        conn.execute(
            "INSERT INTO predictions "
            "(run_id, market_id, bucket, p_win, edge, recommended, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("r1", "m1", bucket, p_win, 0.1, 1, "t"),
        )
    # actual 71 -> 70-71 wins, 72-73 loses
    conn.execute(
        "INSERT INTO outcomes (market_id, actual_value, settled_at) VALUES (?, ?, ?)",
        ("m1", 71.0, "t"),
    )
    conn.commit()


def test_compute_pnl_sums_recommended_bets():
    conn = connect(":memory:")
    _setup(conn)
    pnl = compute_pnl(conn)
    conn.close()
    assert pnl["n_bets"] == 2
    assert (pnl["wins"], pnl["losses"]) == (1, 1)
    assert pnl["total_pnl"] == pytest.approx(0.30)  # (1 - 0.40) + (-0.30)
    assert pnl["roi"] == pytest.approx(0.30 / 0.70)  # staked = 0.40 + 0.30


def test_compute_calibration_brier_and_hit_rate():
    conn = connect(":memory:")
    _setup(conn)
    cal = compute_calibration(conn)
    conn.close()
    assert cal["n"] == 2
    assert cal["brier"] == pytest.approx(((0.93 - 1) ** 2 + (0.50 - 0) ** 2) / 2)
    assert cal["hit_rate"] == pytest.approx(0.5)


def test_compute_pnl_empty_when_nothing_settled():
    conn = connect(":memory:")
    init_schema(conn)
    pnl = compute_pnl(conn)
    conn.close()
    assert pnl == {"n_bets": 0, "wins": 0, "losses": 0, "total_pnl": 0.0, "roi": 0.0}


def test_write_snapshot_persists_metrics():
    from rainmaker.tracking import write_snapshot

    conn = connect(":memory:")
    _setup(conn)
    write_snapshot(conn, "2026-06-04", "2026-06-04T00:00:00Z")
    row = conn.execute(
        "SELECT * FROM tracking_snapshot WHERE snapshot_date = ?", ("2026-06-04",)
    ).fetchone()
    conn.close()
    assert row["n_bets"] == 2
    assert row["total_pnl"] == pytest.approx(0.30)
    assert row["n_scored"] == 2


def test_write_snapshot_is_idempotent_per_day():
    from rainmaker.tracking import write_snapshot

    conn = connect(":memory:")
    _setup(conn)
    write_snapshot(conn, "2026-06-04", "t1")
    write_snapshot(conn, "2026-06-04", "t2")
    n = conn.execute("SELECT count(*) AS n FROM tracking_snapshot").fetchone()["n"]
    conn.close()
    assert n == 1
