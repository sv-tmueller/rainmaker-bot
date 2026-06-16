"""TDD tests for compute_live_calibration and the migration that backs it.

Known-answer tests written BEFORE implementation. Each test must fail red
until the corresponding implementation lands.

Population definition:
- One sample per (market, UTC day): from mu/sigma (dist_params) + actual.
  Used for CRPS and coverage. Reuses _latest_run_per_market_day logic.
- Reliability uses stored p_win per YES bucket-prediction (across all
  buckets, not just the best-edge one) from settled markets.

Both populations exclude: recommended filter, price requirement.
No per-city split -- pooled per (variable, lead).
"""

import json

import pytest
from scipy.stats import norm

from rainmaker.backtest import COVERAGE_LEVELS, crps_gaussian
from rainmaker.store.db import connect, init_schema

# ---------------------------------------------------------------------------
# Helpers to build synthetic fixtures
# ---------------------------------------------------------------------------


def _run(conn, run_id, started_at):
    conn.execute(
        "INSERT INTO runs (id, started_at, status) VALUES (?, ?, ?)",
        (run_id, started_at, "ok"),
    )


def _market(conn, market_id, *, city="NYC", variable="TMAX", settlement_date, venue=None):
    conn.execute(
        "INSERT INTO markets (id, city, variable, settlement_date, venue) VALUES (?, ?, ?, ?, ?)",
        (market_id, city, variable, settlement_date, venue),
    )


def _dist(mu, sigma):
    return json.dumps({"mu": mu, "sigma": sigma, "n_sources": 2})


def _pred(conn, run_id, market_id, bucket, p_win, mu=70.0, sigma=2.0, side="YES", recommended=1):
    conn.execute(
        "INSERT INTO predictions "
        "(run_id, market_id, bucket, side, p_win, dist_params, edge, recommended, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, market_id, bucket, side, p_win, _dist(mu, sigma), 0.1, recommended, "t"),
    )


def _outcome(conn, market_id, actual_value):
    conn.execute(
        "INSERT INTO outcomes (market_id, actual_value, settled_at) VALUES (?, ?, ?)",
        (market_id, actual_value, "t"),
    )


# ---------------------------------------------------------------------------
# CRPS math: known-answer baseline (reuses backtest.crps_gaussian)
# ---------------------------------------------------------------------------


def test_crps_at_mean_known_answer():
    # CRPS(N(70,2), 70) = 2 * CRPS(N(0,1), 0) = 2 * 0.23369 = 0.46739
    # (sigma scales CRPS linearly at z=0)
    result = crps_gaussian(70.0, 2.0, 70.0)
    assert result == pytest.approx(2 * 0.23369, abs=1e-4)


def test_coverage_at_mean_all_true():
    # actual at mean -> cdf = 0.5 -> |0.5 - 0.5| = 0 <= q/2 for all q
    mu, sigma, actual = 70.0, 2.0, 70.0
    cdf_actual = float(norm.cdf(actual, loc=mu, scale=sigma))
    for q in COVERAGE_LEVELS:
        assert abs(cdf_actual - 0.5) <= q / 2


def test_coverage_at_one_sigma():
    # actual = mu + sigma -> cdf ~= 0.841 -> |0.841 - 0.5| = 0.341
    # 0.341 > 0.5/2 (0.25) so NOT in 50%; 0.341 <= 0.8/2 (0.4) and 0.9/2 (0.45) -> in 80%/90%
    mu, sigma = 70.0, 2.0
    actual = mu + sigma
    cdf_actual = float(norm.cdf(actual, loc=mu, scale=sigma))
    gap = abs(cdf_actual - 0.5)
    assert gap > 0.5 / 2  # outside 50%
    assert gap <= 0.8 / 2  # inside 80%
    assert gap <= 0.9 / 2  # inside 90%


# ---------------------------------------------------------------------------
# compute_live_calibration: basic correctness with 1 sample
# ---------------------------------------------------------------------------


def test_compute_live_calibration_single_sample():
    """One market, one run: CRPS and coverage match known-answer computation."""
    from rainmaker.tracking import compute_live_calibration

    conn = connect(":memory:")
    init_schema(conn)
    _run(conn, "r1", "2026-05-30T12:00:00+00:00")
    _market(conn, "m1", settlement_date="2026-05-31")
    # Two bucket rows -- collapse to one (run, market) sample for CRPS/coverage
    _pred(conn, "r1", "m1", "70-71°F", p_win=0.70, mu=70.0, sigma=2.0)
    _pred(conn, "r1", "m1", "72-73°F", p_win=0.20, mu=70.0, sigma=2.0)
    _outcome(conn, "m1", 70.0)
    conn.commit()

    rows = compute_live_calibration(conn)
    conn.close()

    # One (variable, lead) group: TMAX, lead 1
    assert len(rows) == 1
    row = rows[0]
    assert row["variable"] == "TMAX"
    assert row["lead_time"] == 1
    assert row["n_samples"] == 1  # one (market, UTC day) sample

    # CRPS at actual = mu: 2 * 0.23369 (sigma=2)
    assert row["crps"] == pytest.approx(2 * 0.23369, abs=1e-4)

    # Coverage at mean: all True -> coverage fractions = 1.0
    assert row["coverage_50"] == pytest.approx(1.0)
    assert row["coverage_80"] == pytest.approx(1.0)
    assert row["coverage_90"] == pytest.approx(1.0)


def test_compute_live_calibration_empty_when_nothing_settled():
    from rainmaker.tracking import compute_live_calibration

    conn = connect(":memory:")
    init_schema(conn)
    assert compute_live_calibration(conn) == []
    conn.close()


def test_compute_live_calibration_pools_cities():
    """Two cities, same variable/lead -> one pooled row."""
    from rainmaker.tracking import compute_live_calibration

    conn = connect(":memory:")
    init_schema(conn)
    for mid, city in (("m1", "NYC"), ("m2", "Miami")):
        _run(conn, f"r-{mid}", "2026-05-30T12:00:00+00:00")
        _market(conn, mid, city=city, settlement_date="2026-05-31")
        _pred(conn, f"r-{mid}", mid, "70-71°F", p_win=0.70, mu=70.0, sigma=2.0)
        _outcome(conn, mid, 70.0)
    conn.commit()

    rows = compute_live_calibration(conn)
    conn.close()

    # pooled: one row for (TMAX, lead=1)
    assert len(rows) == 1
    assert rows[0]["n_samples"] == 2


def test_compute_live_calibration_groups_by_variable_and_lead():
    """TMAX and TMIN, different leads -> separate rows."""
    from rainmaker.tracking import compute_live_calibration

    conn = connect(":memory:")
    init_schema(conn)
    # TMAX lead 1
    _run(conn, "r1", "2026-05-30T12:00:00+00:00")
    _market(conn, "m1", city="NYC", variable="TMAX", settlement_date="2026-05-31")
    _pred(conn, "r1", "m1", "70-71°F", p_win=0.70, mu=70.0, sigma=2.0)
    _outcome(conn, "m1", 70.0)
    # TMIN lead 2
    _run(conn, "r2", "2026-05-29T12:00:00+00:00")
    _market(conn, "m2", city="NYC", variable="TMIN", settlement_date="2026-05-31")
    _pred(conn, "r2", "m2", "60-61°F", p_win=0.60, mu=60.0, sigma=2.0)
    _outcome(conn, "m2", 60.0)
    conn.commit()

    rows = compute_live_calibration(conn)
    conn.close()

    keys = {(r["variable"], r["lead_time"]) for r in rows}
    assert keys == {("TMAX", 1), ("TMIN", 2)}


def test_compute_live_calibration_skips_negative_lead():
    """A run after settlement date has lead < 0 and must be excluded."""
    from rainmaker.tracking import compute_live_calibration

    conn = connect(":memory:")
    init_schema(conn)
    _run(conn, "r1", "2026-06-01T12:00:00+00:00")
    _market(conn, "m1", settlement_date="2026-05-31")
    _pred(conn, "r1", "m1", "70-71°F", p_win=0.70, mu=70.0, sigma=2.0)
    _outcome(conn, "m1", 70.0)
    conn.commit()

    rows = compute_live_calibration(conn)
    conn.close()
    assert rows == []


def test_compute_live_calibration_reliability_bins():
    """Reliability: (p_win, won) pairs per YES bucket; bins match reliability_bins."""
    from rainmaker.tracking import compute_live_calibration

    conn = connect(":memory:")
    init_schema(conn)
    _run(conn, "r1", "2026-05-30T12:00:00+00:00")
    _market(conn, "m1", settlement_date="2026-05-31")
    # Two YES buckets: 70-71 (p_win=0.70) wins (actual=70), 72-73 (p_win=0.20) loses
    _pred(conn, "r1", "m1", "70-71°F", p_win=0.70, mu=70.0, sigma=2.0, side="YES")
    _pred(conn, "r1", "m1", "72-73°F", p_win=0.20, mu=70.0, sigma=2.0, side="YES")
    _outcome(conn, "m1", 70.0)  # 70 in 70-71 -> first YES wins, second loses
    conn.commit()

    rows = compute_live_calibration(conn)
    conn.close()

    assert len(rows) == 1
    row = rows[0]
    bins = row["reliability_bins"]
    # reliability_bins from backtest.py: 0.70 -> bin [0.7, 0.8), 0.20 -> bin [0.2, 0.3)
    # bin 0.2: (0.20, False) -> observed_freq = 0.0
    # bin 0.7: (0.70, True) -> observed_freq = 1.0
    by_lo = {b["lo"]: b for b in bins}
    assert 0.2 in by_lo
    assert by_lo[0.2]["observed_freq"] == pytest.approx(0.0)
    assert by_lo[0.2]["count"] == 1
    assert 0.7 in by_lo
    assert by_lo[0.7]["observed_freq"] == pytest.approx(1.0)
    assert by_lo[0.7]["count"] == 1


def test_compute_live_calibration_same_day_collapses():
    """Two runs on the same UTC day collapse to the latest for CRPS/coverage."""
    from rainmaker.tracking import compute_live_calibration

    conn = connect(":memory:")
    init_schema(conn)
    # r2 is later (12:00 vs 09:00)
    _run(conn, "r1", "2026-05-30T09:00:00+00:00")
    _run(conn, "r2", "2026-05-30T12:00:00+00:00")
    _market(conn, "m1", settlement_date="2026-05-30")  # lead 0
    # r1: mu=70, r2: mu=73 (different sigma to detect which one is used)
    conn.execute(
        "INSERT INTO predictions "
        "(run_id, market_id, bucket, side, p_win, dist_params, edge, recommended, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("r1", "m1", "70-71°F", "YES", 0.70, json.dumps({"mu": 70.0, "sigma": 2.0}), 0.1, 1, "t"),
    )
    conn.execute(
        "INSERT INTO predictions "
        "(run_id, market_id, bucket, side, p_win, dist_params, edge, recommended, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("r2", "m1", "70-71°F", "YES", 0.70, json.dumps({"mu": 73.0, "sigma": 2.0}), 0.1, 1, "t"),
    )
    _outcome(conn, "m1", 73.0)
    conn.commit()

    rows = compute_live_calibration(conn)
    conn.close()

    assert len(rows) == 1
    row = rows[0]
    assert row["n_samples"] == 1  # same UTC day -> one sample
    # Should use r2's mu=73 (latest); actual=73 -> CRPS at mean
    assert row["crps"] == pytest.approx(2 * 0.23369, abs=1e-4)


# ---------------------------------------------------------------------------
# Migration 0009: new columns appear after migration
# ---------------------------------------------------------------------------


def test_migration_0009_adds_calibration_columns():
    """After init_schema, forecast_accuracy has the new calibration columns."""
    conn = connect(":memory:")
    init_schema(conn)
    # Verify columns exist by doing a SELECT that references them
    conn.execute(
        "SELECT crps, coverage_50, coverage_80, coverage_90, reliability "
        "FROM forecast_accuracy LIMIT 0"
    )
    conn.close()


# ---------------------------------------------------------------------------
# save_accuracy round-trip with new fields
# ---------------------------------------------------------------------------


def test_save_accuracy_persists_calibration_fields():
    """save_accuracy with calibration fields persists crps/coverage/reliability."""
    from rainmaker.probability.calibration import Accuracy
    from rainmaker.store.record import save_accuracy

    conn = connect(":memory:")
    init_schema(conn)

    rel_bins = [{"lo": 0.7, "hi": 0.8, "predicted_mean": 0.72, "observed_freq": 0.8, "count": 5}]
    acc = Accuracy(
        n=10,
        mae_f=2.5,
        bias_f=-0.5,
        crps=0.45,
        coverage_50=0.52,
        coverage_80=0.78,
        coverage_90=0.89,
        reliability_bins=rel_bins,
    )
    save_accuracy(
        conn,
        station="ALL",
        city=None,
        variable="TMAX",
        lead_time=1,
        kind="calibration",
        accuracy=acc,
        updated_at="2026-06-17T00:00:00Z",
    )

    row = conn.execute(
        "SELECT * FROM forecast_accuracy WHERE station = 'ALL' AND kind = 'calibration'"
    ).fetchone()
    conn.close()

    assert row is not None
    assert row["crps"] == pytest.approx(0.45)
    assert row["coverage_50"] == pytest.approx(0.52)
    assert row["coverage_80"] == pytest.approx(0.78)
    assert row["coverage_90"] == pytest.approx(0.89)
    assert json.loads(row["reliability"]) == rel_bins


# ---------------------------------------------------------------------------
# write_snapshot calls compute_live_calibration
# ---------------------------------------------------------------------------


def test_write_snapshot_persists_calibration_rows():
    """After write_snapshot, forecast_accuracy has a kind='calibration' row."""
    from rainmaker.tracking import write_snapshot

    conn = connect(":memory:")
    init_schema(conn)
    _run(conn, "r1", "2026-05-30T12:00:00+00:00")
    _market(conn, "m1", settlement_date="2026-05-31")
    _pred(conn, "r1", "m1", "70-71°F", p_win=0.70, mu=70.0, sigma=2.0)
    _pred(conn, "r1", "m1", "72-73°F", p_win=0.20, mu=70.0, sigma=2.0)
    _outcome(conn, "m1", 70.0)
    conn.commit()

    write_snapshot(conn, "2026-06-17", "2026-06-17T00:00:00Z")

    row = conn.execute("SELECT * FROM forecast_accuracy WHERE kind = 'calibration'").fetchone()
    conn.close()

    assert row is not None
    assert row["variable"] == "TMAX"
    assert row["lead_time"] == 1
    assert row["crps"] is not None
    assert row["coverage_50"] is not None


def test_compute_live_calibration_skips_bad_dist_params():
    """Rows with unparsable dist_params are skipped without raising."""
    from rainmaker.tracking import compute_live_calibration

    conn = connect(":memory:")
    init_schema(conn)
    _run(conn, "r1", "2026-05-30T12:00:00+00:00")
    _market(conn, "m1", settlement_date="2026-05-31")
    conn.execute(
        "INSERT INTO predictions "
        "(run_id, market_id, bucket, side, p_win, dist_params, edge, recommended, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("r1", "m1", "70-71°F", "YES", 0.70, "not json", 0.1, 1, "t"),
    )
    _outcome(conn, "m1", 70.0)
    conn.commit()

    rows = compute_live_calibration(conn)
    conn.close()
    assert rows == []
