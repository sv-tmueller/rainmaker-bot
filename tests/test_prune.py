from rainmaker.store.db import connect, init_schema
from rainmaker.store.prune import prune_settled


def _add_market(conn, market_id, settled):
    conn.execute(
        "INSERT INTO markets (id, city, variable, settlement_date) "
        "VALUES (?, 'NYC', 'TMAX', '2026-05-30')",
        (market_id,),
    )
    if settled:
        conn.execute(
            "INSERT INTO outcomes (market_id, actual_value, settled_at) VALUES (?, 71.0, 't')",
            (market_id,),
        )


def _add_run_rows(conn, market_id, run_id, started_at):
    """One run that records a price, a prediction, and a forecast for the market."""
    conn.execute(
        "INSERT INTO runs (id, started_at, status) VALUES (?, ?, 'ok')",
        (run_id, started_at),
    )
    conn.execute(
        "INSERT INTO prices (run_id, market_id, outcome, side, price, implied_prob, captured_at) "
        "VALUES (?, ?, '70-71°F', 'YES', 0.40, 0.40, 't')",
        (run_id, market_id),
    )
    conn.execute(
        "INSERT INTO predictions "
        "(run_id, market_id, bucket, side, p_win, edge, recommended, created_at) "
        "VALUES (?, ?, '70-71°F', 'YES', 0.6, 0.2, 1, 't')",
        (run_id, market_id),
    )
    conn.execute(
        "INSERT INTO forecasts "
        "(run_id, market_id, source, model, variable, values_json, lead_time, fetched_at) "
        "VALUES (?, ?, 'nws', 'm', 'TMAX', '[70.0]', 1, 't')",
        (run_id, market_id),
    )


def _rows_for(conn, table, run_id, market_id):
    return conn.execute(
        f"SELECT count(*) AS n FROM {table} WHERE run_id = ? AND market_id = ?",
        (run_id, market_id),
    ).fetchone()["n"]


def _count(conn, table):
    return conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]


def test_prune_deletes_older_same_day_run_for_settled_market():
    conn = connect(":memory:")
    init_schema(conn)
    _add_market(conn, "m1", settled=True)
    _add_run_rows(conn, "m1", "r1", "2026-06-06T09:00:00Z")
    _add_run_rows(conn, "m1", "r2", "2026-06-06T12:00:00Z")
    conn.commit()
    deleted = prune_settled(conn)
    conn.commit()
    for table in ("prices", "predictions", "forecasts"):
        assert _rows_for(conn, table, "r1", "m1") == 0  # older same-day run pruned
        assert _rows_for(conn, table, "r2", "m1") == 1  # latest run kept
    assert deleted == 3  # one row each in prices/predictions/forecasts for r1
    # durable tables are never touched
    assert (_count(conn, "runs"), _count(conn, "markets"), _count(conn, "outcomes")) == (2, 1, 1)
    conn.close()


def test_prune_skips_unsettled_market():
    conn = connect(":memory:")
    init_schema(conn)
    _add_market(conn, "m1", settled=False)
    _add_run_rows(conn, "m1", "r1", "2026-06-06T09:00:00Z")
    _add_run_rows(conn, "m1", "r2", "2026-06-06T12:00:00Z")
    conn.commit()
    deleted = prune_settled(conn)
    for table in ("prices", "predictions", "forecasts"):
        assert _rows_for(conn, table, "r1", "m1") == 1  # unsettled market untouched
        assert _rows_for(conn, table, "r2", "m1") == 1
    assert deleted == 0
    conn.close()


def test_prune_keeps_runs_on_different_days():
    conn = connect(":memory:")
    init_schema(conn)
    _add_market(conn, "m1", settled=True)
    _add_run_rows(conn, "m1", "r1", "2026-06-05T12:00:00Z")
    _add_run_rows(conn, "m1", "r2", "2026-06-06T12:00:00Z")
    conn.commit()
    deleted = prune_settled(conn)
    for table in ("prices", "predictions", "forecasts"):
        assert _rows_for(conn, table, "r1", "m1") == 1  # different UTC day kept
        assert _rows_for(conn, table, "r2", "m1") == 1
    assert deleted == 0
    conn.close()


def _add_run_prices_only(conn, market_id, run_id, started_at):
    """A run that records only prices - no predictions or forecasts (precip market path)."""
    conn.execute(
        "INSERT INTO runs (id, started_at, status) VALUES (?, ?, 'ok')",
        (run_id, started_at),
    )
    conn.execute(
        "INSERT INTO prices (run_id, market_id, outcome, side, price, implied_prob, captured_at) "
        "VALUES (?, ?, 'under', 'YES', 0.55, 0.55, 't')",
        (run_id, market_id),
    )


def test_prune_reclaims_prices_for_settled_market_with_no_predictions():
    """A precip market writes prices but not predictions; its redundant intraday
    prices must still be pruned when the market settles."""
    conn = connect(":memory:")
    init_schema(conn)
    _add_market(conn, "m2", settled=True)
    _add_run_prices_only(conn, "m2", "r1", "2026-06-06T09:00:00Z")
    _add_run_prices_only(conn, "m2", "r2", "2026-06-06T12:00:00Z")
    conn.commit()
    deleted = prune_settled(conn)
    conn.commit()
    assert _rows_for(conn, "prices", "r1", "m2") == 0  # older run pruned
    assert _rows_for(conn, "prices", "r2", "m2") == 1  # latest run kept
    assert deleted >= 1
    conn.close()


def test_prune_is_idempotent():
    conn = connect(":memory:")
    init_schema(conn)
    _add_market(conn, "m1", settled=True)
    _add_run_rows(conn, "m1", "r1", "2026-06-06T09:00:00Z")
    _add_run_rows(conn, "m1", "r2", "2026-06-06T12:00:00Z")
    conn.commit()
    assert prune_settled(conn) == 3
    conn.commit()
    assert prune_settled(conn) == 0  # nothing left to prune
    conn.close()
