import json

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


def _setup_no_bet(conn, actual: float):
    init_schema(conn)
    conn.execute("INSERT INTO runs (id, started_at, status) VALUES (?, ?, ?)", ("r1", "t", "ok"))
    conn.execute(
        "INSERT INTO markets (id, city, variable, settlement_date) VALUES (?, ?, ?, ?)",
        ("m1", "NYC", "TMAX", "2026-05-30"),
    )
    # A NO bet on 80-81: sold at no_ask 0.70 (our P(no) 0.95).
    conn.execute(
        "INSERT INTO prices (run_id, market_id, outcome, side, price, implied_prob, captured_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("r1", "m1", "80-81°F", "NO", 0.70, 0.30, "t"),
    )
    conn.execute(
        "INSERT INTO predictions "
        "(run_id, market_id, bucket, side, p_win, edge, recommended, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("r1", "m1", "80-81°F", "NO", 0.95, 0.25, 1, "t"),
    )
    conn.execute(
        "INSERT INTO outcomes (market_id, actual_value, settled_at) VALUES (?, ?, ?)",
        ("m1", actual, "t"),
    )
    conn.commit()


def test_no_bet_wins_when_bucket_does_not_settle():
    conn = connect(":memory:")
    _setup_no_bet(conn, actual=71.0)  # 71 is not in 80-81 -> NO wins
    pnl = compute_pnl(conn)
    cal = compute_calibration(conn)
    conn.close()
    assert (pnl["wins"], pnl["losses"]) == (1, 0)
    assert pnl["total_pnl"] == pytest.approx(1 - 0.70)  # NO won pays 1 - no_ask
    assert cal["hit_rate"] == pytest.approx(1.0)
    assert cal["n"] == 0 and cal["brier"] is None  # no YES rows to score


def test_no_bet_loses_when_bucket_settles():
    conn = connect(":memory:")
    _setup_no_bet(conn, actual=80.0)  # 80 is in 80-81 -> NO loses
    pnl = compute_pnl(conn)
    conn.close()
    assert (pnl["wins"], pnl["losses"]) == (0, 1)
    assert pnl["total_pnl"] == pytest.approx(-0.70)  # NO lost -no_ask


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


def _setup_live(conn, city="NYC"):
    init_schema(conn)
    conn.execute(
        "INSERT INTO runs (id, started_at, status) VALUES (?, ?, ?)",
        ("r1", "2026-05-30T12:00:00+00:00", "ok"),
    )
    conn.execute(
        "INSERT INTO markets (id, city, variable, settlement_date) VALUES (?, ?, ?, ?)",
        ("m1", city, "TMAX", "2026-05-31"),
    )
    dist = json.dumps({"mu": 70.0, "sigma": 2.0, "n_sources": 2})
    for bucket in ("70-71°F", "72-73°F"):  # two buckets, same market -> one sample
        conn.execute(
            "INSERT INTO predictions "
            "(run_id, market_id, bucket, p_win, dist_params, edge, recommended, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("r1", "m1", bucket, 0.5, dist, 0.1, 1, "t"),
        )
    conn.execute(
        "INSERT INTO outcomes (market_id, actual_value, settled_at) VALUES (?, ?, ?)",
        ("m1", 73.0, "t"),
    )
    conn.commit()


def test_compute_live_accuracy_dedupes_buckets():
    from rainmaker.tracking import compute_live_accuracy

    conn = connect(":memory:")
    _setup_live(conn)
    rows = compute_live_accuracy(conn)
    conn.close()
    assert len(rows) == 1
    row = rows[0]
    assert (row["station"], row["city"], row["variable"], row["lead_time"]) == (
        "KLGA",
        "NYC",
        "TMAX",
        1,
    )
    acc = row["accuracy"]
    assert acc.n == 1  # two bucket rows collapse to one (run, market) sample
    assert acc.mae_f == pytest.approx(3.0)  # |70 - 73|
    assert acc.bias_f == pytest.approx(-3.0)  # forecast ran cold


def test_compute_live_accuracy_skips_unknown_city():
    from rainmaker.tracking import compute_live_accuracy

    conn = connect(":memory:")
    _setup_live(conn, city="Gotham")
    rows = compute_live_accuracy(conn)
    conn.close()
    assert rows == []


def test_compute_live_accuracy_empty_when_nothing_settled():
    from rainmaker.tracking import compute_live_accuracy

    conn = connect(":memory:")
    init_schema(conn)
    assert compute_live_accuracy(conn) == []
    conn.close()


def test_write_snapshot_writes_live_accuracy_rows():
    from rainmaker.tracking import write_snapshot

    conn = connect(":memory:")
    _setup_live(conn)
    write_snapshot(conn, "2026-06-04", "t")
    row = conn.execute("SELECT * FROM forecast_accuracy WHERE kind = 'live'").fetchone()
    conn.close()
    assert row is not None
    assert row["station"] == "KLGA"
    assert row["mae_f"] == pytest.approx(3.0)


def test_compute_live_accuracy_skips_bad_rows():
    from rainmaker.tracking import compute_live_accuracy

    conn = connect(":memory:")
    _setup_live(conn)
    # a second market with unparsable dist_params
    conn.execute(
        "INSERT INTO markets (id, city, variable, settlement_date) VALUES (?, ?, ?, ?)",
        ("m2", "NYC", "TMAX", "2026-05-31"),
    )
    conn.execute(
        "INSERT INTO predictions "
        "(run_id, market_id, bucket, p_win, dist_params, edge, recommended, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("r1", "m2", "70-71°F", 0.5, "not json", 0.1, 1, "t"),
    )
    conn.execute(
        "INSERT INTO outcomes (market_id, actual_value, settled_at) VALUES (?, ?, ?)",
        ("m2", 71.0, "t"),
    )
    # a third market settled with a null actual
    conn.execute(
        "INSERT INTO markets (id, city, variable, settlement_date) VALUES (?, ?, ?, ?)",
        ("m3", "NYC", "TMAX", "2026-05-31"),
    )
    conn.execute(
        "INSERT INTO predictions "
        "(run_id, market_id, bucket, p_win, dist_params, edge, recommended, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("r1", "m3", "70-71°F", 0.5, json.dumps({"mu": 70.0, "sigma": 2.0}), 0.1, 1, "t"),
    )
    conn.execute(
        "INSERT INTO outcomes (market_id, actual_value, settled_at) VALUES (?, ?, ?)",
        ("m3", None, "t"),
    )
    conn.commit()
    rows = compute_live_accuracy(conn)
    conn.close()
    # both bad rows are skipped; only the good m1 sample remains
    assert len(rows) == 1
    assert rows[0]["accuracy"].n == 1
