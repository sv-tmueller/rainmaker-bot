import json

import httpx
import pytest

from rainmaker.store.db import connect, init_schema
from rainmaker.tracking import (
    _wilson_interval,
    compute_attribution,
    compute_calibration,
    compute_pnl,
)


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
    for bucket, p_win, edge in (("70-71°F", 0.93, 0.20), ("72-73°F", 0.50, 0.10)):
        conn.execute(
            "INSERT INTO predictions "
            "(run_id, market_id, bucket, p_win, edge, recommended, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("r1", "m1", bucket, p_win, edge, 1, "t"),
        )
    # actual 71 -> 70-71 wins, 72-73 loses
    conn.execute(
        "INSERT INTO outcomes (market_id, actual_value, settled_at) VALUES (?, ?, ?)",
        ("m1", 71.0, "t"),
    )
    conn.commit()


def test_compute_pnl_collapses_correlated_bets_to_best_edge():
    conn = connect(":memory:")
    _setup(conn)  # m1/r1: 70-71 (edge .20, ask .40, wins) and 72-73 (edge .10, loses)
    pnl = compute_pnl(conn)
    conn.close()
    assert pnl["n_bets"] == 1  # one bet per (market, run): the best edge
    assert (pnl["wins"], pnl["losses"]) == (1, 0)
    assert pnl["total_pnl"] == pytest.approx(0.60)  # 1 - 0.40
    assert pnl["roi"] == pytest.approx(0.60 / 0.40)


def test_compute_calibration_brier_unchanged_hit_rate_collapsed():
    conn = connect(":memory:")
    _setup(conn)
    cal = compute_calibration(conn)
    conn.close()
    # Brier still over both YES bucket-predictions (calibration was never inflated).
    assert cal["n"] == 2
    assert cal["brier"] == pytest.approx(((0.93 - 1) ** 2 + (0.50 - 0) ** 2) / 2)
    # Hit rate over the single best-edge bet (70-71, which won).
    assert cal["hit_rate"] == pytest.approx(1.0)


def test_compute_pnl_filters_by_venue():
    conn = connect(":memory:")
    init_schema(conn)
    conn.execute("INSERT INTO runs (id, started_at, status) VALUES (?, ?, ?)", ("r1", "t", "ok"))
    # one Polymarket market (NULL venue) and one Kalshi market, each a winning bet
    for mid, venue in (("m_poly", None), ("m_kalshi", "kalshi")):
        conn.execute(
            "INSERT INTO markets (id, city, variable, settlement_date, venue) "
            "VALUES (?, ?, ?, ?, ?)",
            (mid, "NYC", "TMAX", "2026-05-30", venue),
        )
        conn.execute(
            "INSERT INTO prices (run_id, market_id, outcome, price, implied_prob, captured_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("r1", mid, "70-71°F", 0.40, 0.40, "t"),
        )
        conn.execute(
            "INSERT INTO predictions "
            "(run_id, market_id, bucket, p_win, edge, recommended, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("r1", mid, "70-71°F", 0.93, 0.20, 1, "t"),
        )
        conn.execute(
            "INSERT INTO outcomes (market_id, actual_value, settled_at) VALUES (?, ?, ?)",
            (mid, 71.0, "t"),
        )
    conn.commit()
    assert compute_pnl(conn)["n_bets"] == 2  # both venues
    assert compute_pnl(conn, venue="kalshi")["n_bets"] == 1
    assert compute_pnl(conn, venue="polymarket")["n_bets"] == 1  # NULL venue = polymarket
    conn.close()


def test_compute_calibration_filters_by_venue():
    conn = connect(":memory:")
    init_schema(conn)
    conn.execute("INSERT INTO runs (id, started_at, status) VALUES (?, ?, ?)", ("r1", "t", "ok"))
    # one Polymarket market (NULL venue) and one Kalshi market, each a winning bet
    for mid, venue in (("m_poly", None), ("m_kalshi", "kalshi")):
        conn.execute(
            "INSERT INTO markets (id, city, variable, settlement_date, venue) "
            "VALUES (?, ?, ?, ?, ?)",
            (mid, "NYC", "TMAX", "2026-05-30", venue),
        )
        conn.execute(
            "INSERT INTO prices (run_id, market_id, outcome, price, implied_prob, captured_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("r1", mid, "70-71°F", 0.40, 0.40, "t"),
        )
        conn.execute(
            "INSERT INTO predictions "
            "(run_id, market_id, bucket, p_win, edge, recommended, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("r1", mid, "70-71°F", 0.93, 0.20, 1, "t"),
        )
        conn.execute(
            "INSERT INTO outcomes (market_id, actual_value, settled_at) VALUES (?, ?, ?)",
            (mid, 71.0, "t"),
        )
    conn.commit()
    assert compute_calibration(conn)["n"] == 2  # both venues' YES rows
    assert compute_calibration(conn, venue="kalshi")["n"] == 1
    assert compute_calibration(conn, venue="polymarket")["n"] == 1  # NULL venue = polymarket
    conn.close()


def test_compute_pnl_settles_precip_bucket():
    conn = connect(":memory:")
    init_schema(conn)
    conn.execute("INSERT INTO runs (id, started_at, status) VALUES (?, ?, ?)", ("r1", "t", "ok"))
    conn.execute(
        "INSERT INTO markets (id, city, variable, settlement_date) VALUES (?, ?, ?, ?)",
        ("pm1", "NYC", "PRCP", "2026-06-30"),
    )
    # An open-low inch bracket the temperature parser cannot read: this only works
    # through the PRCP branch using precip_settles.
    conn.execute(
        "INSERT INTO prices (run_id, market_id, outcome, price, implied_prob, captured_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("r1", "pm1", '<2"', 0.30, 0.30, "t"),
    )
    conn.execute(
        "INSERT INTO predictions (run_id, market_id, bucket, p_win, edge, recommended, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("r1", "pm1", '<2"', 0.60, 0.30, 1, "t"),
    )
    conn.execute(
        "INSERT INTO outcomes (market_id, actual_value, settled_at) VALUES (?, ?, ?)",
        ("pm1", 1.50, "t"),
    )
    conn.commit()
    pnl = compute_pnl(conn)
    conn.close()
    # 1.50 inches lands under 2", so the YES bet on the <2" bracket wins.
    assert (pnl["wins"], pnl["losses"]) == (1, 0)
    assert pnl["total_pnl"] == pytest.approx(1 - 0.30)


def _add_market_outcome(conn, market_id, actual=71.0):
    conn.execute(
        "INSERT INTO markets (id, city, variable, settlement_date) VALUES (?, ?, ?, ?)",
        (market_id, "NYC", "TMAX", "2026-05-30"),
    )
    conn.execute(
        "INSERT INTO outcomes (market_id, actual_value, settled_at) VALUES (?, ?, 't')",
        (market_id, actual),
    )


def _add_no_bets(conn, market_id, run_id):
    conn.execute(
        "INSERT OR IGNORE INTO runs (id, started_at, status) VALUES (?, ?, ?)",
        (run_id, "t", "ok"),
    )
    # Three correlated NO bets; actual 71 lands in 70-71, so 60-61 and 80-81 NO win.
    no_bets = (
        ("60-61°F", 0.10, 0.97, 0.87),
        ("70-71°F", 0.20, 0.90, 0.70),
        ("80-81°F", 0.05, 0.99, 0.94),
    )
    for bucket, no_ask, p_no, edge in no_bets:
        conn.execute(
            "INSERT INTO prices (run_id, market_id, outcome, side, price, implied_prob, "
            "captured_at) VALUES (?, ?, ?, 'NO', ?, ?, 't')",
            (run_id, market_id, bucket, no_ask, 1 - no_ask),
        )
        conn.execute(
            "INSERT INTO predictions (run_id, market_id, bucket, side, p_win, edge, "
            "recommended, created_at) VALUES (?, ?, ?, 'NO', ?, ?, 1, 't')",
            (run_id, market_id, bucket, p_no, edge),
        )


def test_correlated_no_bets_collapse_to_one():
    conn = connect(":memory:")
    init_schema(conn)
    _add_market_outcome(conn, "m1")
    _add_no_bets(conn, "m1", "r1")
    conn.commit()
    pnl = compute_pnl(conn)
    conn.close()
    assert pnl["n_bets"] == 1  # three NO bets on one market-run -> one bet
    # Best edge is 80-81 NO (edge .94, ask .05); 71 not in 80-81, so it won.
    assert (pnl["wins"], pnl["losses"]) == (1, 0)
    assert pnl["total_pnl"] == pytest.approx(1 - 0.05)


def test_each_market_run_counted_once():
    conn = connect(":memory:")
    init_schema(conn)
    for mid in ("m1", "m2"):
        _add_market_outcome(conn, mid)
        _add_no_bets(conn, mid, "r1")
    conn.commit()
    pnl = compute_pnl(conn)
    conn.close()
    assert pnl["n_bets"] == 2  # one per distinct market in the same run


def test_same_market_across_runs_counts_separately():
    conn = connect(":memory:")
    init_schema(conn)
    _add_market_outcome(conn, "m1")
    # Two runs on different UTC days each count once. Insert the runs with real
    # dated timestamps first; _add_no_bets' INSERT OR IGNORE keeps them.
    conn.execute(
        "INSERT INTO runs (id, started_at, status) VALUES (?, ?, ?)",
        ("r1", "2026-06-05T13:00:00Z", "ok"),
    )
    conn.execute(
        "INSERT INTO runs (id, started_at, status) VALUES (?, ?, ?)",
        ("r2", "2026-06-06T13:00:00Z", "ok"),
    )
    _add_no_bets(conn, "m1", "r1")
    _add_no_bets(conn, "m1", "r2")
    conn.commit()
    pnl = compute_pnl(conn)
    conn.close()
    assert pnl["n_bets"] == 2  # different UTC days stay separate


def _add_yes_run(conn, market_id, run_id, started_at, p_win):
    """One YES bet on 70-71 for (market, run): ask 0.40, given p_win, dist_params set."""
    conn.execute(
        "INSERT INTO runs (id, started_at, status) VALUES (?, ?, ?)",
        (run_id, started_at, "ok"),
    )
    conn.execute(
        "INSERT INTO prices (run_id, market_id, outcome, side, price, implied_prob, captured_at) "
        "VALUES (?, ?, '70-71°F', 'YES', 0.40, 0.40, 't')",
        (run_id, market_id),
    )
    dist = json.dumps({"mu": 70.0, "sigma": 2.0, "n_sources": 2})
    conn.execute(
        "INSERT INTO predictions "
        "(run_id, market_id, bucket, side, p_win, dist_params, edge, recommended, created_at) "
        "VALUES (?, ?, '70-71°F', 'YES', ?, ?, 0.20, 1, 't')",
        (run_id, market_id, p_win, dist),
    )


def test_same_day_runs_collapse_to_latest():
    from rainmaker.tracking import compute_live_accuracy

    conn = connect(":memory:")
    init_schema(conn)
    _add_market_outcome(conn, "m1", actual=71.0)  # 71 in 70-71 -> YES wins
    # both runs on the settlement day (2026-05-30) -> lead 0, same UTC day
    _add_yes_run(conn, "m1", "r1", "2026-05-30T09:00:00Z", p_win=0.60)
    _add_yes_run(conn, "m1", "r2", "2026-05-30T12:00:00Z", p_win=0.93)
    conn.commit()
    pnl = compute_pnl(conn)
    cal = compute_calibration(conn)
    acc = compute_live_accuracy(conn)
    conn.close()
    assert pnl["n_bets"] == 1  # same-day runs collapse to the latest (r2)
    assert cal["n"] == 1  # one YES row scored: the latest run's
    assert cal["hit_rate"] == pytest.approx(1.0)  # 70-71 YES won
    assert cal["brier"] == pytest.approx((0.93 - 1) ** 2)  # r2's p_win, not r1's 0.60
    assert len(acc) == 1  # one (market, UTC day) sample


def test_different_day_runs_counted_separately():
    conn = connect(":memory:")
    init_schema(conn)
    _add_market_outcome(conn, "m1", actual=71.0)
    # settlement is 2026-05-30: lead 1 then lead 0, two different UTC days
    _add_yes_run(conn, "m1", "r1", "2026-05-29T12:00:00Z", p_win=0.60)
    _add_yes_run(conn, "m1", "r2", "2026-05-30T12:00:00Z", p_win=0.93)
    conn.commit()
    pnl = compute_pnl(conn)
    cal = compute_calibration(conn)
    conn.close()
    assert pnl["n_bets"] == 2  # different UTC days stay separate
    assert cal["n"] == 2  # both runs' YES rows scored


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
    assert row["n_bets"] == 1
    assert row["total_pnl"] == pytest.approx(0.60)
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


def _setup_live(conn, city="NYC", venue=None):
    init_schema(conn)
    conn.execute(
        "INSERT INTO runs (id, started_at, status) VALUES (?, ?, ?)",
        ("r1", "2026-05-30T12:00:00+00:00", "ok"),
    )
    conn.execute(
        "INSERT INTO markets (id, city, variable, settlement_date, venue) VALUES (?, ?, ?, ?, ?)",
        ("m1", city, "TMAX", "2026-05-31", venue),
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


def test_compute_live_accuracy_attributes_kalshi_to_its_station():
    from rainmaker.tracking import compute_live_accuracy

    conn = connect(":memory:")
    _setup_live(conn, venue="kalshi")  # a Kalshi NYC market settles on Central Park
    rows = compute_live_accuracy(conn)
    conn.close()
    assert len(rows) == 1
    # attributed to KNYC (Central Park), not the Polymarket KLGA (LaGuardia)
    assert rows[0]["station"] == "KNYC"
    assert rows[0]["city"] == "NYC"


def test_compute_live_accuracy_skips_unknown_city():
    from rainmaker.tracking import compute_live_accuracy

    conn = connect(":memory:")
    _setup_live(conn, city="Gotham")
    rows = compute_live_accuracy(conn)
    conn.close()
    assert rows == []


def test_compute_live_accuracy_skips_negative_lead_runs():
    from rainmaker.tracking import compute_live_accuracy

    conn = connect(":memory:")
    init_schema(conn)
    # run on 2026-06-01, market settled 2026-05-31 -> lead -1, a post-settlement
    # catch-up run, not a real forecast: excluded from accuracy.
    conn.execute(
        "INSERT INTO runs (id, started_at, status) VALUES (?, ?, ?)",
        ("r1", "2026-06-01T12:00:00+00:00", "ok"),
    )
    conn.execute(
        "INSERT INTO markets (id, city, variable, settlement_date) VALUES (?, ?, ?, ?)",
        ("m1", "NYC", "TMAX", "2026-05-31"),
    )
    dist = json.dumps({"mu": 70.0, "sigma": 2.0, "n_sources": 2})
    conn.execute(
        "INSERT INTO predictions "
        "(run_id, market_id, bucket, p_win, dist_params, edge, recommended, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("r1", "m1", "70-71°F", 0.5, dist, 0.1, 1, "t"),
    )
    conn.execute(
        "INSERT INTO outcomes (market_id, actual_value, settled_at) VALUES (?, ?, ?)",
        ("m1", 73.0, "t"),
    )
    conn.commit()
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


def test_write_snapshot_snapshot_after_accuracy(monkeypatch):
    # The tracking_snapshot INSERT must come after the save_accuracy loop so that
    # a failure mid-loop does not leave a committed snapshot without all accuracy rows.
    # If save_accuracy raises on the second call, no snapshot row should be committed.
    import rainmaker.tracking as tracking_mod
    from rainmaker.store.record import save_accuracy as real_save_accuracy
    from rainmaker.tracking import write_snapshot

    call_count = 0

    def save_accuracy_bomb(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            raise RuntimeError("simulated save_accuracy failure")
        return real_save_accuracy(*args, **kwargs)

    conn = connect(":memory:")
    _setup_live(conn)
    # Add a second accuracy group (different city, so compute_live_accuracy returns two rows).
    conn.execute(
        "INSERT INTO markets (id, city, variable, settlement_date) VALUES (?, ?, ?, ?)",
        ("m2", "Miami", "TMAX", "2026-05-31"),
    )
    dist = json.dumps({"mu": 55.0, "sigma": 2.0, "n_sources": 2})
    conn.execute(
        "INSERT INTO predictions "
        "(run_id, market_id, bucket, p_win, dist_params, edge, recommended, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("r1", "m2", "54-55°F", 0.5, dist, 0.1, 1, "t"),
    )
    conn.execute(
        "INSERT INTO outcomes (market_id, actual_value, settled_at) VALUES (?, ?, ?)",
        ("m2", 57.0, "t"),
    )
    conn.commit()

    # Patch where it is used (the name bound in tracking.py after the import).
    monkeypatch.setattr(tracking_mod, "save_accuracy", save_accuracy_bomb)
    with pytest.raises(RuntimeError, match="simulated save_accuracy failure"):
        write_snapshot(conn, "2026-06-04", "t")

    # With the fix (snapshot INSERT after the loop), the snapshot is not committed
    # when save_accuracy raises on the second call. Without the fix it would already
    # be committed (the first save_accuracy commit flushes the pending snapshot INSERT).
    n = conn.execute("SELECT count(*) AS n FROM tracking_snapshot").fetchone()["n"]
    conn.close()
    assert n == 0, "snapshot was committed before all accuracy rows - ordering bug"


# ---------------------------------------------------------------------------
# Kalshi label grading: grade from outcome_spec, not from re-parsing the label
# ---------------------------------------------------------------------------


def _setup_kalshi_temp(conn, actual: float, bucket_label: str):
    """One Kalshi YES bet on a Kalshi-format temperature bucket."""
    init_schema(conn)
    conn.execute("INSERT INTO runs (id, started_at, status) VALUES (?, ?, ?)", ("r1", "t", "ok"))
    spec = json.dumps(
        [{"label": bucket_label, "kind": "range", "lo": 74, "hi": 75, "threshold": None}]
    )
    conn.execute(
        "INSERT INTO markets (id, city, variable, settlement_date, venue, outcome_spec) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("mk1", "NYC", "TMAX", "2026-05-30", "kalshi", spec),
    )
    conn.execute(
        "INSERT INTO prices (run_id, market_id, outcome, price, implied_prob, captured_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("r1", "mk1", bucket_label, 0.40, 0.40, "t"),
    )
    conn.execute(
        "INSERT INTO predictions "
        "(run_id, market_id, bucket, p_win, edge, recommended, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("r1", "mk1", bucket_label, 0.80, 0.30, 1, "t"),
    )
    conn.execute(
        "INSERT INTO outcomes (market_id, actual_value, settled_at) VALUES (?, ?, ?)",
        ("mk1", actual, "t"),
    )
    conn.commit()


def test_kalshi_temp_label_grade_win():
    """A Kalshi '74° to 75°' bucket with actual=74 grades as a win from outcome_spec."""
    conn = connect(":memory:")
    _setup_kalshi_temp(conn, actual=74.0, bucket_label="74° to 75°")
    # Prior to the fix this raises ValueError: unrecognized bucket label: '74° to 75°'
    pnl = compute_pnl(conn)
    cal = compute_calibration(conn)
    conn.close()
    assert (pnl["wins"], pnl["losses"]) == (1, 0)
    assert pnl["total_pnl"] == pytest.approx(1 - 0.40)
    # Brier path in compute_calibration also calls _won: it must not raise.
    assert cal["brier"] == pytest.approx((0.80 - 1.0) ** 2)
    assert cal["hit_rate"] == pytest.approx(1.0)


def test_kalshi_temp_label_grade_loss():
    """A Kalshi '74° to 75°' bucket with actual=80 grades as a loss from outcome_spec."""
    conn = connect(":memory:")
    _setup_kalshi_temp(conn, actual=80.0, bucket_label="74° to 75°")
    pnl = compute_pnl(conn)
    cal = compute_calibration(conn)
    conn.close()
    assert (pnl["wins"], pnl["losses"]) == (0, 1)
    assert pnl["total_pnl"] == pytest.approx(-0.40)
    # Brier: 0.80 predicted YES but outcome was 0 (did not settle).
    assert cal["brier"] == pytest.approx((0.80 - 0.0) ** 2)
    assert cal["hit_rate"] == pytest.approx(0.0)


def test_kalshi_precip_label_grade():
    """A Kalshi '2" to 3"' precip bucket with actual=2.5 grades as a win from outcome_spec."""
    conn = connect(":memory:")
    init_schema(conn)
    conn.execute("INSERT INTO runs (id, started_at, status) VALUES (?, ?, ?)", ("r1", "t", "ok"))
    bucket_label = '2" to 3"'
    spec = json.dumps(
        [{"label": bucket_label, "kind": "range", "lo": 2.0, "hi": 3.0, "threshold": None}]
    )
    conn.execute(
        "INSERT INTO markets (id, city, variable, settlement_date, venue, outcome_spec) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("pm1", "NYC", "PRCP", "2026-06-30", "kalshi", spec),
    )
    conn.execute(
        "INSERT INTO prices (run_id, market_id, outcome, price, implied_prob, captured_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("r1", "pm1", bucket_label, 0.35, 0.35, "t"),
    )
    conn.execute(
        "INSERT INTO predictions "
        "(run_id, market_id, bucket, p_win, edge, recommended, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("r1", "pm1", bucket_label, 0.70, 0.35, 1, "t"),
    )
    conn.execute(
        "INSERT INTO outcomes (market_id, actual_value, settled_at) VALUES (?, ?, ?)",
        ("pm1", 2.5, "t"),
    )
    conn.commit()
    # Prior to the fix this raises ValueError via parse_precip_bracket_label.
    pnl = compute_pnl(conn)
    conn.close()
    # 2.5 is in [2.0, 3.0) -> YES wins
    assert (pnl["wins"], pnl["losses"]) == (1, 0)
    assert pnl["total_pnl"] == pytest.approx(1 - 0.35)


def test_legacy_fallback_null_outcome_spec():
    """When outcome_spec is NULL, the label parser fallback grades correctly (legacy rows)."""
    conn = connect(":memory:")
    init_schema(conn)
    conn.execute("INSERT INTO runs (id, started_at, status) VALUES (?, ?, ?)", ("r1", "t", "ok"))
    # NULL outcome_spec - the old rows before this fix was shipped
    conn.execute(
        "INSERT INTO markets (id, city, variable, settlement_date) VALUES (?, ?, ?, ?)",
        ("m1", "NYC", "TMAX", "2026-05-30"),
    )
    conn.execute(
        "INSERT INTO prices (run_id, market_id, outcome, price, implied_prob, captured_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("r1", "m1", "70-71°F", 0.40, 0.40, "t"),
    )
    conn.execute(
        "INSERT INTO predictions "
        "(run_id, market_id, bucket, p_win, edge, recommended, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("r1", "m1", "70-71°F", 0.80, 0.30, 1, "t"),
    )
    conn.execute(
        "INSERT INTO outcomes (market_id, actual_value, settled_at) VALUES (?, ?, ?)",
        ("m1", 71.0, "t"),  # 71 in 70-71 -> YES wins
    )
    conn.commit()
    pnl = compute_pnl(conn)
    conn.close()
    assert (pnl["wins"], pnl["losses"]) == (1, 0)


# ---------------------------------------------------------------------------
# Attribution tests (TDD: these must fail until compute_attribution is added)
# ---------------------------------------------------------------------------


def test_wilson_interval_known_value():
    # k=8 wins out of n=10: standard Wilson 95% CI at z=1.96
    # lo = (p + z^2/2n - z*sqrt(p(1-p)/n + z^2/4n^2)) / (1 + z^2/n)
    # Expected: lo ~ 0.4902, hi ~ 0.9433
    lo, hi = _wilson_interval(8, 10)
    assert lo == pytest.approx(0.4902, abs=1e-3)
    assert hi == pytest.approx(0.9433, abs=1e-3)


def test_wilson_interval_n0_edge():
    # n=0: undefined; return (0.0, 1.0) to signal full uncertainty
    lo, hi = _wilson_interval(0, 0)
    assert lo == 0.0
    assert hi == 1.0


def test_lead_bucket_boundary():
    """lead=3 must land in '3+', not a spurious standalone '3' bucket."""
    from rainmaker.tracking import _lead_bucket

    assert _lead_bucket("2026-05-30", "2026-05-27T00:00:00") == "3+"  # lead=3
    assert _lead_bucket("2026-05-30", "2026-05-28T00:00:00") == "2"  # lead=2
    assert _lead_bucket("2026-05-30", "2026-05-26T00:00:00") == "3+"  # lead=4
    assert _lead_bucket("2026-05-30", "2026-05-31T00:00:00") == "<0 (catch-up)"  # lead=-1


def _setup_attribution_fixture(conn):
    """Three bets across two cities, two venues, two variables, three lead buckets.

    Bet A: NYC, polymarket (NULL venue), TMAX
      started_at=2026-05-28T00:00:00, settlement_date=2026-05-30 -> lead=2
      edge=0.12  -> bucket [.10,.20)
      p_win=0.88 -> bucket [.80,.90)
      ask=0.40, actual=71 (70-71 wins) -> WIN, pnl=+0.60, staked=0.40

    Bet B: LAX, kalshi, TMIN
      started_at=2026-05-29T00:00:00, settlement_date=2026-05-30 -> lead=1
      edge=0.22  -> bucket [.20,inf)
      p_win=0.93 -> bucket [.90,.95)
      ask=0.35, actual=75 (70-71 does NOT win) -> LOSS, pnl=-0.35, staked=0.35

    Bet C: NYC, kalshi, TMAX
      started_at=2026-05-26T00:00:00, settlement_date=2026-05-30 -> lead=4 -> bucket 3+
      edge=0.08  -> bucket [.05,.10)
      p_win=0.78 -> bucket [.75,.80)
      ask=0.45, actual=71 (70-71 wins) -> WIN, pnl=+0.55, staked=0.45

    Overall: n=3, wins=2, losses=1, total_pnl=0.80, total_staked=1.20, roi=0.80/1.20
    """
    init_schema(conn)
    # Bet A: NYC, polymarket (NULL venue), TMAX, lead=2, edge=0.12, p_win=0.88, win
    conn.execute(
        "INSERT INTO runs (id, started_at, status) VALUES (?, ?, ?)",
        ("rA", "2026-05-28T00:00:00", "ok"),
    )
    conn.execute(
        "INSERT INTO markets (id, city, variable, settlement_date, venue) VALUES (?, ?, ?, ?, ?)",
        ("mA", "NYC", "TMAX", "2026-05-30", None),
    )
    conn.execute(
        "INSERT INTO prices (run_id, market_id, outcome, price, implied_prob, captured_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("rA", "mA", "70-71°F", 0.40, 0.40, "t"),
    )
    conn.execute(
        "INSERT INTO predictions "
        "(run_id, market_id, bucket, p_win, edge, recommended, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("rA", "mA", "70-71°F", 0.88, 0.12, 1, "t"),
    )
    conn.execute(
        "INSERT INTO outcomes (market_id, actual_value, settled_at) VALUES (?, ?, ?)",
        ("mA", 71.0, "t"),  # 71 in 70-71 -> wins
    )
    # Bet B: LAX, kalshi, TMIN, lead=1, edge=0.22, p_win=0.93, loss
    conn.execute(
        "INSERT INTO runs (id, started_at, status) VALUES (?, ?, ?)",
        ("rB", "2026-05-29T00:00:00", "ok"),
    )
    conn.execute(
        "INSERT INTO markets (id, city, variable, settlement_date, venue) VALUES (?, ?, ?, ?, ?)",
        ("mB", "LAX", "TMIN", "2026-05-30", "kalshi"),
    )
    conn.execute(
        "INSERT INTO prices (run_id, market_id, outcome, price, implied_prob, captured_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("rB", "mB", "70-71°F", 0.35, 0.35, "t"),
    )
    conn.execute(
        "INSERT INTO predictions "
        "(run_id, market_id, bucket, p_win, edge, recommended, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("rB", "mB", "70-71°F", 0.93, 0.22, 1, "t"),
    )
    conn.execute(
        "INSERT INTO outcomes (market_id, actual_value, settled_at) VALUES (?, ?, ?)",
        ("mB", 75.0, "t"),  # 75 NOT in 70-71 -> loss
    )
    # Bet C: NYC, kalshi, TMAX, lead=4 (3+ bucket), edge=0.08, p_win=0.78, win
    conn.execute(
        "INSERT INTO runs (id, started_at, status) VALUES (?, ?, ?)",
        ("rC", "2026-05-26T00:00:00", "ok"),
    )
    conn.execute(
        "INSERT INTO markets (id, city, variable, settlement_date, venue) VALUES (?, ?, ?, ?, ?)",
        ("mC", "NYC", "TMAX", "2026-05-30", "kalshi"),
    )
    conn.execute(
        "INSERT INTO prices (run_id, market_id, outcome, price, implied_prob, captured_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("rC", "mC", "70-71°F", 0.45, 0.45, "t"),
    )
    conn.execute(
        "INSERT INTO predictions "
        "(run_id, market_id, bucket, p_win, edge, recommended, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("rC", "mC", "70-71°F", 0.78, 0.08, 1, "t"),
    )
    conn.execute(
        "INSERT INTO outcomes (market_id, actual_value, settled_at) VALUES (?, ?, ?)",
        ("mC", 71.0, "t"),  # 71 in 70-71 -> wins
    )
    conn.commit()


def test_compute_attribution_per_segment_values():
    """Per-segment n/wins/losses/roi match hand-computed values from the fixture."""
    conn = connect(":memory:")
    _setup_attribution_fixture(conn)
    result = compute_attribution(conn)
    conn.close()

    # --- city dimension ---
    by_city = {s["segment"]: s for s in result["city"]}
    # NYC: bets A (win, pnl=+0.60, staked=0.40) + C (win, pnl=+0.55, staked=0.45)
    nyc = by_city["NYC"]
    assert nyc["n"] == 2
    assert nyc["wins"] == 2
    assert nyc["losses"] == 0
    assert nyc["roi"] == pytest.approx((0.60 + 0.55) / (0.40 + 0.45))
    # LAX: bet B (loss, pnl=-0.35, staked=0.35)
    lax = by_city["LAX"]
    assert lax["n"] == 1
    assert lax["wins"] == 0
    assert lax["losses"] == 1
    assert lax["roi"] == pytest.approx(-0.35 / 0.35)

    # --- venue dimension ---
    by_venue = {s["segment"]: s for s in result["venue"]}
    # polymarket: bet A (win)
    pm = by_venue["polymarket"]
    assert pm["n"] == 1
    assert pm["wins"] == 1
    assert pm["roi"] == pytest.approx(0.60 / 0.40)
    # kalshi: bets B (loss) + C (win)
    kal = by_venue["kalshi"]
    assert kal["n"] == 2
    assert kal["wins"] == 1
    assert kal["losses"] == 1
    assert kal["roi"] == pytest.approx((-0.35 + 0.55) / (0.35 + 0.45))

    # --- variable dimension ---
    by_var = {s["segment"]: s for s in result["variable"]}
    # TMAX: bets A + C (both win)
    assert by_var["TMAX"]["n"] == 2
    assert by_var["TMAX"]["wins"] == 2
    # TMIN: bet B (loss)
    assert by_var["TMIN"]["n"] == 1
    assert by_var["TMIN"]["wins"] == 0

    # --- lead dimension ---
    by_lead = {s["segment"]: s for s in result["lead"]}
    # lead=1: bet B
    assert by_lead["1"]["n"] == 1
    assert by_lead["1"]["wins"] == 0
    # lead=2: bet A
    assert by_lead["2"]["n"] == 1
    assert by_lead["2"]["wins"] == 1
    # lead=4 -> 3+ bucket: bet C
    assert by_lead["3+"]["n"] == 1
    assert by_lead["3+"]["wins"] == 1

    # --- edge bucket dimension ---
    by_edge = {s["segment"]: s for s in result["edge"]}
    # edge=0.12 -> [.10,.20): bet A (win)
    assert by_edge["[.10,.20)"]["n"] == 1
    assert by_edge["[.10,.20)"]["wins"] == 1
    # edge=0.22 -> [.20,inf): bet B (loss)
    assert by_edge["[.20,inf)"]["n"] == 1
    assert by_edge["[.20,inf)"]["wins"] == 0
    # edge=0.08 -> [.05,.10): bet C (win)
    assert by_edge["[.05,.10)"]["n"] == 1
    assert by_edge["[.05,.10)"]["wins"] == 1

    # --- p_win bucket dimension ---
    by_pw = {s["segment"]: s for s in result["p_win"]}
    # p_win=0.88 -> [.80,.90): bet A
    assert by_pw["[.80,.90)"]["n"] == 1
    assert by_pw["[.80,.90)"]["wins"] == 1
    # p_win=0.93 -> [.90,.95): bet B
    assert by_pw["[.90,.95)"]["n"] == 1
    assert by_pw["[.90,.95)"]["wins"] == 0
    # p_win=0.78 -> [.75,.80): bet C
    assert by_pw["[.75,.80)"]["n"] == 1
    assert by_pw["[.75,.80)"]["wins"] == 1


def test_compute_attribution_consistency_with_compute_pnl():
    """Each dimension's summed n/wins/losses and recomputed ROI match compute_pnl.

    Extends the shared fixture with three additional bets that exercise previously
    untested buckets:

    Bet D (catch-up): started_at after settlement_date -> lead -2 -> '<0 (catch-up)'
    Bet E (NULL-edge): edge=None -> _edge_bucket returns '<.05'
    Bet F (low p_win): p_win=0.70 -> _p_win_bucket returns '<.75'

    Three non-empty asserts confirm those buckets appear; the per-dimension
    reconciliation loop then covers all six bets.
    """
    conn = connect(":memory:")
    _setup_attribution_fixture(conn)

    # Bet D: catch-up run (started_at > settlement_date -> lead=-2 -> '<0 (catch-up)')
    # NYC, polymarket (NULL venue), TMAX, edge=0.12, p_win=0.88, ask=0.40
    # actual=71 in '70-71°F' -> WIN
    conn.execute(
        "INSERT INTO runs (id, started_at, status) VALUES (?, ?, ?)",
        ("rD", "2026-06-01T00:00:00", "ok"),
    )
    conn.execute(
        "INSERT INTO markets (id, city, variable, settlement_date, venue) VALUES (?, ?, ?, ?, ?)",
        ("mD", "NYC", "TMAX", "2026-05-30", None),
    )
    conn.execute(
        "INSERT INTO prices (run_id, market_id, outcome, price, implied_prob, captured_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("rD", "mD", "70-71°F", 0.40, 0.40, "t"),
    )
    conn.execute(
        "INSERT INTO predictions "
        "(run_id, market_id, bucket, p_win, edge, recommended, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("rD", "mD", "70-71°F", 0.88, 0.12, 1, "t"),
    )
    conn.execute(
        "INSERT INTO outcomes (market_id, actual_value, settled_at) VALUES (?, ?, ?)",
        ("mD", 71.0, "t"),
    )

    # Bet E: NULL edge -> _edge_bucket(None) returns '<.05'
    # LAX, polymarket (NULL venue), TMIN, lead=2, edge=None, p_win=0.82, ask=0.40
    # actual=75 NOT in '70-71°F' -> LOSS
    conn.execute(
        "INSERT INTO runs (id, started_at, status) VALUES (?, ?, ?)",
        ("rE", "2026-05-28T00:00:00", "ok"),
    )
    conn.execute(
        "INSERT INTO markets (id, city, variable, settlement_date, venue) VALUES (?, ?, ?, ?, ?)",
        ("mE", "LAX", "TMIN", "2026-05-30", None),
    )
    conn.execute(
        "INSERT INTO prices (run_id, market_id, outcome, price, implied_prob, captured_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("rE", "mE", "70-71°F", 0.40, 0.40, "t"),
    )
    conn.execute(
        "INSERT INTO predictions "
        "(run_id, market_id, bucket, p_win, edge, recommended, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("rE", "mE", "70-71°F", 0.82, None, 1, "t"),
    )
    conn.execute(
        "INSERT INTO outcomes (market_id, actual_value, settled_at) VALUES (?, ?, ?)",
        ("mE", 75.0, "t"),
    )

    # Bet F: p_win < 0.75 -> _p_win_bucket returns '<.75'
    # NYC, polymarket (NULL venue), TMAX, lead=2, edge=0.15, p_win=0.70, ask=0.40
    # actual=71 in '70-71°F' -> WIN
    conn.execute(
        "INSERT INTO runs (id, started_at, status) VALUES (?, ?, ?)",
        ("rF", "2026-05-28T00:00:00", "ok"),
    )
    conn.execute(
        "INSERT INTO markets (id, city, variable, settlement_date, venue) VALUES (?, ?, ?, ?, ?)",
        ("mF", "NYC", "TMAX", "2026-05-30", None),
    )
    conn.execute(
        "INSERT INTO prices (run_id, market_id, outcome, price, implied_prob, captured_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("rF", "mF", "70-71°F", 0.40, 0.40, "t"),
    )
    conn.execute(
        "INSERT INTO predictions "
        "(run_id, market_id, bucket, p_win, edge, recommended, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("rF", "mF", "70-71°F", 0.70, 0.15, 1, "t"),
    )
    conn.execute(
        "INSERT INTO outcomes (market_id, actual_value, settled_at) VALUES (?, ?, ?)",
        ("mF", 71.0, "t"),
    )

    conn.commit()

    pnl = compute_pnl(conn)
    result = compute_attribution(conn)
    conn.close()

    # Confirm the new buckets are represented before the reconciliation loop
    by_lead = {s["segment"]: s for s in result["lead"]}
    assert by_lead["<0 (catch-up)"]["n"] >= 1, "catch-up bucket must be non-empty"

    by_edge = {s["segment"]: s for s in result["edge"]}
    assert by_edge["<.05"]["n"] >= 1, "NULL-edge bucket must be non-empty"

    by_p_win = {s["segment"]: s for s in result["p_win"]}
    assert by_p_win["<.75"]["n"] >= 1, "<.75 p_win bucket must be non-empty"

    for dim in ("city", "venue", "variable", "lead", "edge", "p_win"):
        segs = result[dim]
        total_n = sum(s["n"] for s in segs)
        total_wins = sum(s["wins"] for s in segs)
        total_losses = sum(s["losses"] for s in segs)
        total_pnl_sum = sum(s["pnl"] for s in segs)
        total_staked = sum(s["staked"] for s in segs)
        roi_recomputed = total_pnl_sum / total_staked if total_staked else 0.0

        assert total_n == pnl["n_bets"], f"{dim}: n mismatch"
        assert total_wins == pnl["wins"], f"{dim}: wins mismatch"
        assert total_losses == pnl["losses"], f"{dim}: losses mismatch"
        assert total_pnl_sum == pytest.approx(pnl["total_pnl"]), f"{dim}: pnl mismatch"
        assert roi_recomputed == pytest.approx(pnl["roi"]), f"{dim}: roi mismatch"


# ---------------------------------------------------------------------------
# CLV tests
# ---------------------------------------------------------------------------

# Settlement date 2026-05-30 -> synthesized settlement_ts = 2026-05-30 12:00:00 UTC
# = 1780142400 unix seconds.
#
# At a 24h lead: reference_ts = 1780142400 - 86400 = 1780056000 (~25h before settlement).
# The day-ahead CLOB point (the "closing" price we measure against) sits at
# t = 1780052400 (1780142400 - 90000, 25h before settlement, strictly before reference_ts).
# The near-settlement point at _TS_BEFORE (1780138800, 1h before) is AFTER reference_ts
# and must be excluded (it is the convergence trap: its price differs from the day-ahead
# price, so if last_before accidentally uses it the expected CLV would not match).
#
# YES bet on market mC (city NYC, TMAX, settlement 2026-05-30):
#   yes_token_id = "token-yes-70-71"
#   ask = 0.40
#   price series (3 points):
#     t=1780052400 (25h before): p=0.80  <- day-ahead price, the CLV reference
#     t=1780138800 (1h before):  p=0.98  <- convergence trap: excluded by fixed lead
#     t=1780146000 (1h after):   p=0.95  <- after settlement: excluded
#   last_before(reference_ts=1780056000) = 0.80
#   CLV_yes = 0.80 - 0.40 = 0.40
#
# NO bet on market mD (city Los Angeles, TMAX, settlement 2026-05-30):
#   yes_token_id = "token-yes-72-73"
#   no_ask = 0.70 (stored as prices.price for the NO side)
#   price series (3 points):
#     t=1780052400 (25h before): p=0.15  <- day-ahead price, the CLV reference
#     t=1780138800 (1h before):  p=0.02  <- convergence trap: excluded by fixed lead
#     t=1780146000 (1h after):   p=0.10  <- after settlement: excluded
#   last_before(reference_ts=1780056000) = 0.15
#   CLV_no = (1 - 0.15) - 0.70 = 0.85 - 0.70 = 0.15

_SETTLEMENT_TS = 1780142400  # 2026-05-30 12:00:00 UTC
_REFERENCE_TS = _SETTLEMENT_TS - 86400  # 24h lead = 1780056000
_TS_DAY_AHEAD = _SETTLEMENT_TS - 90000  # 1780052400, 25h before; strictly before reference_ts
_TS_BEFORE = 1780138800  # 1h before settlement (convergence trap, excluded by fixed lead)
_TS_AFTER = 1780146000  # 1h after settlement (always excluded)
_START_TS = _REFERENCE_TS - 6 * 24 * 3600  # window start anchored to reference

_TOKEN_YES = "token-yes-70-71"
_TOKEN_NO_MARKET = "token-yes-72-73"  # YES token for the market that has a NO bet


def _market_raw(
    market_id: str,
    city: str,
    variable: str,
    settlement_date: str,
    yes_token_id: str,
    bucket_label: str,
    ask: float,
    side: str = "YES",
    no_ask: float | None = None,
) -> str:
    """Build a minimal Market model_dump JSON for the raw column."""
    from rainmaker.config import STATIONS, Target
    from rainmaker.domain import Bucket, Market

    station = STATIONS[city]
    from datetime import date

    local_date = date.fromisoformat(settlement_date)
    target = Target(station=station, variable=variable, local_date=local_date)
    no_token = "no-" + yes_token_id
    bucket = Bucket(
        label=bucket_label,
        kind="range",
        lo=70,
        hi=71,
        threshold=None,
        yes_token_id=yes_token_id,
        best_ask=ask if side == "YES" else None,
        best_bid=None if no_ask is None else 1 - no_ask,
        yes_price=ask,
        no_token_id=no_token,
        no_ask=no_ask,
    )
    market = Market(
        id=market_id,
        slug=f"{city.lower()}-{variable.lower()}-{settlement_date}",
        title=f"test {city} {variable}",
        target=target,
        buckets=[bucket],
    )
    import json

    return json.dumps(market.model_dump(mode="json"))


def _setup_clv_fixture(conn) -> None:
    """Two settled Polymarket bets: a YES bet (mC) and a NO bet (mD).

    mC: YES bet on 70-71 for NYC TMAX 2026-05-30, ask=0.40, yes_token=token-yes-70-71
    mD: NO bet on 70-71 for Los Angeles TMAX 2026-05-30, no_ask=0.70, yes_token=token-yes-72-73
    """
    init_schema(conn)
    conn.execute(
        "INSERT INTO runs (id, started_at, status) VALUES (?, ?, ?)",
        ("rC", "2026-05-29T00:00:00", "ok"),
    )
    conn.execute(
        "INSERT INTO runs (id, started_at, status) VALUES (?, ?, ?)",
        ("rD", "2026-05-29T00:00:00", "ok"),
    )
    raw_c = _market_raw("mC", "NYC", "TMAX", "2026-05-30", _TOKEN_YES, "70-71°F", 0.40)
    raw_d = _market_raw(
        "mD", "Los Angeles", "TMAX", "2026-05-30", _TOKEN_NO_MARKET, "70-71°F", 0.40, "NO", 0.70
    )
    conn.execute(
        "INSERT INTO markets (id, city, variable, settlement_date, venue, raw) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("mC", "NYC", "TMAX", "2026-05-30", "polymarket", raw_c),
    )
    conn.execute(
        "INSERT INTO markets (id, city, variable, settlement_date, venue, raw) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("mD", "Los Angeles", "TMAX", "2026-05-30", "polymarket", raw_d),
    )
    # mC: YES bet ask=0.40
    conn.execute(
        "INSERT INTO prices (run_id, market_id, outcome, side, price, implied_prob, captured_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("rC", "mC", "70-71°F", "YES", 0.40, 0.40, "t"),
    )
    conn.execute(
        "INSERT INTO predictions "
        "(run_id, market_id, bucket, side, p_win, edge, recommended, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("rC", "mC", "70-71°F", "YES", 0.88, 0.20, 1, "t"),
    )
    conn.execute(
        "INSERT INTO outcomes (market_id, actual_value, settled_at) VALUES (?, ?, ?)",
        ("mC", 71.0, "t"),
    )
    # mD: NO bet no_ask=0.70
    conn.execute(
        "INSERT INTO prices (run_id, market_id, outcome, side, price, implied_prob, captured_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("rD", "mD", "70-71°F", "NO", 0.70, 0.30, "t"),
    )
    conn.execute(
        "INSERT INTO predictions "
        "(run_id, market_id, bucket, side, p_win, edge, recommended, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("rD", "mD", "70-71°F", "NO", 0.88, 0.18, 1, "t"),
    )
    conn.execute(
        "INSERT INTO outcomes (market_id, actual_value, settled_at) VALUES (?, ?, ?)",
        ("mD", 75.0, "t"),  # 75 NOT in 70-71 -> NO wins
    )
    conn.commit()


def _clob_series(yes_close: float, trap: float | None = None, at_settlement: float = 0.95) -> dict:
    """CLOB history fixture with a day-ahead point, a convergence trap, and a post-settlement point.

    yes_close: the day-ahead price at _TS_DAY_AHEAD (what CLV is measured against).
    trap: the near-settlement price at _TS_BEFORE; differs from yes_close so the test
          proves the fixed lead excludes it. Defaults to a value far from yes_close.
    at_settlement: price at _TS_AFTER (after settlement, always excluded).
    """
    if trap is None:
        # Default trap: a convergence value clearly different from the day-ahead price
        # so any test that accidentally picks it would produce the wrong CLV.
        trap = 1.0 - yes_close  # guaranteed to differ from yes_close (unless 0.5)
    return {
        "history": [
            {"t": _TS_DAY_AHEAD, "p": yes_close},  # day-ahead price: the CLV reference
            {"t": _TS_BEFORE, "p": trap},  # convergence trap: excluded by 24h fixed lead
            {"t": _TS_AFTER, "p": at_settlement},  # after settlement: always excluded
        ]
    }


def _empty_series() -> dict:
    return {"history": []}


# ---------------------------------------------------------------------------
# last_before unit tests (no HTTP, no DB)
# ---------------------------------------------------------------------------


def test_last_before_returns_price_of_latest_point_strictly_before_target():
    from rainmaker.polymarket.prices import PricePoint, last_before

    points = [
        PricePoint(t=1000, p=0.10),
        PricePoint(t=2000, p=0.20),
        PricePoint(t=3000, p=0.30),
    ]
    # target=2500: points 1000 and 2000 qualify; max t is 2000, p=0.20
    assert last_before(points, 2500) == pytest.approx(0.20)


def test_last_before_excludes_point_at_exactly_target():
    from rainmaker.polymarket.prices import PricePoint, last_before

    points = [PricePoint(t=1000, p=0.10), PricePoint(t=2000, p=0.20)]
    # target=2000: only t=1000 qualifies (strict <)
    assert last_before(points, 2000) == pytest.approx(0.10)


def test_last_before_excludes_all_points_at_or_after_target():
    from rainmaker.polymarket.prices import PricePoint, last_before

    points = [PricePoint(t=3000, p=0.30), PricePoint(t=4000, p=0.40)]
    assert last_before(points, 2500) is None


def test_last_before_empty_list_is_none():
    from rainmaker.polymarket.prices import last_before

    assert last_before([], 2000) is None


# ---------------------------------------------------------------------------
# compute_clv tests (require compute_clv to exist)
# ---------------------------------------------------------------------------


def test_clv_per_bet_yes_and_no_side(httpx_mock):
    """YES CLV = yes_close - ask; NO CLV = (1 - yes_close) - no_ask."""
    import re

    from rainmaker.polymarket.prices import CLOB_PRICES_URL
    from rainmaker.tracking import compute_clv

    # YES bet market: token-yes-70-71, yes_close=0.80
    httpx_mock.add_response(
        url=re.compile(re.escape(CLOB_PRICES_URL) + r".*market=token-yes-70-71"),
        json=_clob_series(yes_close=0.80),
    )
    # NO bet market: token-yes-72-73, yes_close=0.15
    httpx_mock.add_response(
        url=re.compile(re.escape(CLOB_PRICES_URL) + r".*market=token-yes-72-73"),
        json=_clob_series(yes_close=0.15),
    )

    conn = connect(":memory:")
    _setup_clv_fixture(conn)
    with httpx.Client() as client:
        result = compute_clv(conn, client)
    conn.close()

    assert result["n_bets"] == 2
    assert result["n_clv"] == 2
    # YES CLV = 0.80 - 0.40 = 0.40; NO CLV = (1 - 0.15) - 0.70 = 0.15
    assert result["mean_clv"] == pytest.approx((0.40 + 0.15) / 2)


def test_clv_population_tiebacks_to_pnl_polymarket_filter(httpx_mock):
    """compute_clv n_bets must equal compute_pnl with venue='polymarket'."""
    import re

    from rainmaker.polymarket.prices import CLOB_PRICES_URL
    from rainmaker.tracking import compute_clv, compute_pnl

    # Mock both token fetches so compute_clv stays offline.
    httpx_mock.add_response(
        url=re.compile(re.escape(CLOB_PRICES_URL) + r".*market=token-yes-70-71"),
        json=_clob_series(yes_close=0.80),
    )
    httpx_mock.add_response(
        url=re.compile(re.escape(CLOB_PRICES_URL) + r".*market=token-yes-72-73"),
        json=_clob_series(yes_close=0.15),
    )

    conn = connect(":memory:")
    _setup_clv_fixture(conn)
    pnl_poly = compute_pnl(conn, venue="polymarket")

    with httpx.Client() as client:
        result = compute_clv(conn, client)
    conn.close()

    assert result["n_bets"] == pnl_poly["n_bets"]


def test_clv_empty_series_drops_from_n_clv_no_crash(httpx_mock):
    """A bet whose CLOB series is empty drops from n_clv but n_bets is unchanged."""
    import re

    from rainmaker.polymarket.prices import CLOB_PRICES_URL
    from rainmaker.tracking import compute_clv

    # Both the initial (60-min) and fallback (720-min) fetches return empty
    httpx_mock.add_response(
        url=re.compile(re.escape(CLOB_PRICES_URL) + r".*market=token-yes-70-71"),
        json=_empty_series(),
    )
    httpx_mock.add_response(
        url=re.compile(re.escape(CLOB_PRICES_URL) + r".*market=token-yes-70-71"),
        json=_empty_series(),
    )
    # NO bet market gets a valid series
    httpx_mock.add_response(
        url=re.compile(re.escape(CLOB_PRICES_URL) + r".*market=token-yes-72-73"),
        json=_clob_series(yes_close=0.15),
    )

    conn = connect(":memory:")
    _setup_clv_fixture(conn)
    with httpx.Client() as client:
        result = compute_clv(conn, client)
    conn.close()

    assert result["n_bets"] == 2  # unchanged
    assert result["n_clv"] == 1  # only the NO bet succeeded
    # mean_clv over the one successful bet: (1 - 0.15) - 0.70 = 0.15
    assert result["mean_clv"] == pytest.approx(0.15)


def test_clv_aggregate_and_per_segment(httpx_mock):
    """mean_clv and by_segment values match hand-computed values.

    Bets: YES (NYC, TMAX, lead=1, edge=0.20, CLV=0.40) and
          NO (Los Angeles, TMAX, lead=1, edge=0.18, CLV=0.15).
    mean_clv = (0.40 + 0.15) / 2 = 0.275
    by city: NYC -> 0.40, Los Angeles -> 0.15
    by variable: TMAX -> 0.275 (both)
    by lead: "1" -> 0.275 (both lead=1, started 2026-05-29, settled 2026-05-30)
    """
    import re

    from rainmaker.polymarket.prices import CLOB_PRICES_URL
    from rainmaker.tracking import compute_clv

    httpx_mock.add_response(
        url=re.compile(re.escape(CLOB_PRICES_URL) + r".*market=token-yes-70-71"),
        json=_clob_series(yes_close=0.80),
    )
    httpx_mock.add_response(
        url=re.compile(re.escape(CLOB_PRICES_URL) + r".*market=token-yes-72-73"),
        json=_clob_series(yes_close=0.15),
    )

    conn = connect(":memory:")
    _setup_clv_fixture(conn)
    with httpx.Client() as client:
        result = compute_clv(conn, client)
    conn.close()

    assert result["mean_clv"] == pytest.approx(0.275)

    by_city = {s["segment"]: s for s in result["by_segment"]["city"]}
    assert by_city["NYC"]["mean_clv"] == pytest.approx(0.40)
    assert by_city["NYC"]["n"] == 1
    assert by_city["Los Angeles"]["mean_clv"] == pytest.approx(0.15)

    by_var = {s["segment"]: s for s in result["by_segment"]["variable"]}
    assert by_var["TMAX"]["mean_clv"] == pytest.approx(0.275)
    assert by_var["TMAX"]["n"] == 2

    by_lead = {s["segment"]: s for s in result["by_segment"]["lead"]}
    assert by_lead["1"]["mean_clv"] == pytest.approx(0.275)
    assert by_lead["1"]["n"] == 2


def test_clv_fixed_lead_excludes_convergence_trap(httpx_mock):
    """The 24h fixed lead selects the day-ahead price, not the near-settlement trap.

    The series has 3 points:
      _TS_DAY_AHEAD (25h before): p=0.80 <- day-ahead; reference_ts=24h -> this is included
      _TS_BEFORE (1h before):     p=0.98 <- convergence trap; AFTER reference_ts -> excluded
      _TS_AFTER (1h after):       p=0.95 <- always excluded

    If the implementation still uses settlement_ts as the last_before deadline
    (old behaviour), it picks the trap (0.98) and CLV_yes = 0.98 - 0.40 = 0.58 != 0.40.
    If it correctly uses reference_ts (24h lead), it picks 0.80 and CLV_yes = 0.40.
    """
    import re

    from rainmaker.polymarket.prices import CLOB_PRICES_URL
    from rainmaker.tracking import compute_clv

    httpx_mock.add_response(
        url=re.compile(re.escape(CLOB_PRICES_URL) + r".*market=token-yes-70-71"),
        json=_clob_series(yes_close=0.80, trap=0.98),
    )
    httpx_mock.add_response(
        url=re.compile(re.escape(CLOB_PRICES_URL) + r".*market=token-yes-72-73"),
        json=_clob_series(yes_close=0.15, trap=0.02),
    )

    conn = connect(":memory:")
    _setup_clv_fixture(conn)
    with httpx.Client() as client:
        result = compute_clv(conn, client)
    conn.close()

    # Both bets have a day-ahead point strictly before reference_ts.
    assert result["n_clv"] == 2
    # YES: 0.80 - 0.40 = 0.40; NO: (1 - 0.15) - 0.70 = 0.15
    assert result["mean_clv"] == pytest.approx(0.275)
    # If the trap (0.98) were used instead: YES CLV = 0.98 - 0.40 = 0.58 != 0.40
    by_city = {s["segment"]: s for s in result["by_segment"]["city"]}
    assert by_city["NYC"]["mean_clv"] == pytest.approx(0.40)


def test_clv_n_coincident_zero_when_prices_differ(httpx_mock):
    """n_coincident is 0 when the day-ahead price differs from the advised price."""
    import re

    from rainmaker.polymarket.prices import CLOB_PRICES_URL
    from rainmaker.tracking import compute_clv

    httpx_mock.add_response(
        url=re.compile(re.escape(CLOB_PRICES_URL) + r".*market=token-yes-70-71"),
        json=_clob_series(yes_close=0.80),
    )
    httpx_mock.add_response(
        url=re.compile(re.escape(CLOB_PRICES_URL) + r".*market=token-yes-72-73"),
        json=_clob_series(yes_close=0.15),
    )

    conn = connect(":memory:")
    _setup_clv_fixture(conn)
    with httpx.Client() as client:
        result = compute_clv(conn, client)
    conn.close()

    # YES: ask=0.40, close=0.80 -> CLV=0.40 (not coincident)
    # NO: no_ask=0.70, close=0.15 -> CLV = (1-0.15) - 0.70 = 0.15 (not coincident)
    assert result["n_coincident"] == 0


def test_clv_n_coincident_one_when_close_equals_advised(httpx_mock):
    """n_coincident is 1 when the day-ahead price equals the advised price on the YES scale."""
    import re

    from rainmaker.polymarket.prices import CLOB_PRICES_URL
    from rainmaker.tracking import compute_clv

    # YES bet: ask=0.40, day-ahead price=0.40 -> CLV=0, coincident
    httpx_mock.add_response(
        url=re.compile(re.escape(CLOB_PRICES_URL) + r".*market=token-yes-70-71"),
        json=_clob_series(yes_close=0.40),
    )
    # NO bet: no_ask=0.70, day-ahead yes_close=0.15 -> CLV=(1-0.15)-0.70=0.15, not coincident
    httpx_mock.add_response(
        url=re.compile(re.escape(CLOB_PRICES_URL) + r".*market=token-yes-72-73"),
        json=_clob_series(yes_close=0.15),
    )

    conn = connect(":memory:")
    _setup_clv_fixture(conn)
    with httpx.Client() as client:
        result = compute_clv(conn, client)
    conn.close()

    # Only the YES bet is coincident (yes_close == ask == 0.40, CLV==0)
    assert result["n_coincident"] == 1


def test_clv_precip_market_drops_from_n_clv_not_n_bets(httpx_mock):
    """A PrecipMonthlyMarket.model_dump() in raw drops from n_clv; n_bets is unchanged.

    PrecipMonthlyMarket does not validate as a Market (different structure), so
    _yes_token_for_bucket returns None and the bet is excluded from n_clv coverage.
    The existing n_bets count (the deduped Polymarket bet population) is unaffected.
    """
    import re

    from rainmaker.config import PRECIP_STATIONS
    from rainmaker.domain import PrecipBracket, PrecipMonthlyMarket, PrecipTarget
    from rainmaker.polymarket.prices import CLOB_PRICES_URL
    from rainmaker.tracking import compute_clv

    # Token fetch for the valid YES bet (mC) succeeds.
    httpx_mock.add_response(
        url=re.compile(re.escape(CLOB_PRICES_URL) + r".*market=token-yes-70-71"),
        json=_clob_series(yes_close=0.80),
    )

    conn = connect(":memory:")
    _setup_clv_fixture(conn)

    # Replace mD's raw column with a PrecipMonthlyMarket.model_dump() JSON.
    # This is what the raw column looks like for a precip market bet.
    from datetime import date

    precip_station = PRECIP_STATIONS["NYC"]
    target = PrecipTarget(
        station=precip_station,
        variable="PRCP",
        year=2026,
        month=5,
        settlement_date=date(2026, 5, 31),
    )
    bracket = PrecipBracket(
        label='<2"',
        kind="below",
        lo=None,
        hi=None,
        threshold=2.0,
        yes_token_id="token-precip-yes",
        best_ask=0.30,
        best_bid=0.25,
        yes_price=0.30,
    )
    precip_market = PrecipMonthlyMarket(
        id="mD",
        slug="nyc-prcp-2026-05",
        title="NYC May 2026 precipitation",
        target=target,
        buckets=[bracket],
    )
    raw_precip = json.dumps(precip_market.model_dump(mode="json"))
    conn.execute("UPDATE markets SET raw = ? WHERE id = ?", (raw_precip, "mD"))
    conn.commit()

    with httpx.Client() as client:
        result = compute_clv(conn, client)
    conn.close()

    # n_bets = 2 (both mC and mD are valid Polymarket recommended settled bets)
    assert result["n_bets"] == 2
    # n_clv = 1: mD drops because its raw is a PrecipMonthlyMarket, not a Market
    assert result["n_clv"] == 1
    # No crash; mean_clv is over the one successful bet
    assert result["mean_clv"] == pytest.approx(0.80 - 0.40)  # YES CLV = 0.40
