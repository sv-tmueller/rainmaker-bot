"""Tests for compute_tail_calibration (rainmaker tail-check).

Fixtures are deterministic: actual values are placed at fixed quantiles of the
stored (mu, sigma) so the PIT tail-occurrence ratios come out exact, not
approximate. No RNG.
"""

import json
import math

import pytest
from scipy.stats import norm

from rainmaker.cli import _tail_check
from rainmaker.store.db import connect, init_schema
from rainmaker.tracking import (
    MIN_TAIL_N,
    _pit_tail_ratios,
    compute_calibration_by_cell,
    compute_tail_calibration,
    settled_rows,
)


def _insert_row(
    conn,
    market_id: str,
    run_id: str,
    *,
    started_at: str,
    settlement_date: str,
    p_win: float,
    mu: float,
    sigma: float,
    actual: float,
    bucket_kind: str,
    lo: float | None = None,
    hi: float | None = None,
    threshold: float | None = None,
    variable: str = "TMAX",
    bucket_label: str = "B",
):
    """Insert one settled (market, run) row: a bucket claim plus its dist_params.

    outcome_spec carries the grading rule directly (kind/lo/hi/threshold), so the
    fixture never depends on the label parser.
    """
    conn.execute(
        "INSERT OR IGNORE INTO runs (id, started_at, status) VALUES (?, ?, ?)",
        (run_id, started_at, "ok"),
    )
    spec = json.dumps(
        [{"label": bucket_label, "kind": bucket_kind, "lo": lo, "hi": hi, "threshold": threshold}]
    )
    conn.execute(
        "INSERT INTO markets (id, city, variable, settlement_date, outcome_spec) "
        "VALUES (?, ?, ?, ?, ?)",
        (market_id, "NYC", variable, settlement_date, spec),
    )
    dist = json.dumps({"mu": mu, "sigma": sigma, "n_sources": 2})
    conn.execute(
        "INSERT INTO predictions "
        "(run_id, market_id, bucket, p_win, dist_params, edge, recommended, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, market_id, bucket_label, p_win, dist, 0.1, 1, "t"),
    )
    conn.execute(
        "INSERT INTO outcomes (market_id, actual_value, settled_at) VALUES (?, ?, ?)",
        (market_id, actual, "t"),
    )


def _primary_row(result, variable, lead, side, bin_label):
    for row in result["primary"]:
        if (
            row["variable"] == variable
            and row["lead_time"] == lead
            and row["side"] == side
            and row["bin"] == bin_label
        ):
            return row
    return None


def _pit_row(result, variable, lead, hour=None):
    for row in result["pit"]:
        if row["variable"] == variable and row["lead_time"] == lead and row["hour"] == hour:
            return row
    return None


def test_pit_tail_ratios_reports_observed_and_expected_counts():
    """obs/exp counts sit alongside each ratio, additive to the returned dict.

    20 PITs, hand-counted: below 0.10 are 0.01 and 0.07 (2); above 0.90 are
    0.91, 0.93, 0.99 (3); below 0.05 is 0.01 alone (1); above 0.95 is 0.99
    alone (1). Expected count is n * q, unaffected by how many actually
    landed in the tail, so it differs from obs in every cell here.
    """
    pits = [0.01, 0.07] + [0.5] * 15 + [0.91, 0.93, 0.99]
    ratios = _pit_tail_ratios(pits)

    assert ratios["n"] == 20
    assert ratios["lower_10_obs"] == 2
    assert ratios["lower_10_exp"] == pytest.approx(2.0, abs=1e-12)
    assert ratios["upper_10_obs"] == 3
    assert ratios["upper_10_exp"] == pytest.approx(2.0, abs=1e-12)
    assert ratios["lower_05_obs"] == 1
    assert ratios["lower_05_exp"] == pytest.approx(1.0, abs=1e-12)
    assert ratios["upper_05_obs"] == 1
    assert ratios["upper_05_exp"] == pytest.approx(1.0, abs=1e-12)

    # ratios themselves are unchanged by the new keys
    assert ratios["lower_10"] == pytest.approx(1.0, abs=1e-12)
    assert ratios["upper_10"] == pytest.approx(1.5, abs=1e-12)
    assert ratios["lower_05"] == pytest.approx(1.0, abs=1e-12)
    assert ratios["upper_05"] == pytest.approx(1.0, abs=1e-12)


def test_overconfident_book_flags_over_and_pit_upper_ratio_above_one():
    """Claims ~0.95 realizing ~0.5: n >= MIN_TAIL_N flags OVER; PIT upper ratio > 1.

    20 markets claim p_win=0.95 on a "72-73" range bucket. Half settle (actual
    72.5, inside the range) and half miss into the far upper tail (actual 76,
    outside the range): realized frequency 0.5, but the actual=76 half also
    pushes PIT (mu=70, sigma=2) into the upper tail past both q=0.10 and q=0.05.
    """
    conn = connect(":memory:")
    init_schema(conn)
    for j in range(MIN_TAIL_N):
        actual = 72.5 if j < 10 else 76.0
        _insert_row(
            conn,
            market_id=f"m1-{j}",
            run_id=f"r1-{j}",
            started_at="2026-05-31T12:00:00+00:00",
            settlement_date="2026-06-01",
            p_win=0.95,
            mu=70.0,
            sigma=2.0,
            actual=actual,
            bucket_kind="range",
            lo=72,
            hi=73,
        )
    conn.commit()
    result = compute_tail_calibration(conn)
    conn.close()

    row = _primary_row(result, "TMAX", 1, "YES", "[0.95,1.0]")
    assert row is not None
    assert row["n"] == 20
    assert row["claimed_mean"] == 0.95
    assert row["realized_freq"] == 0.5
    assert row["thin"] is False
    assert row["verdict"] == "OVER"

    pit = _pit_row(result, "TMAX", 1)
    assert pit is not None
    assert pit["n"] == 20
    assert pit["upper_10"] > 1
    assert pit["upper_05"] > 1


def test_calibrated_book_no_verdict_and_pit_ratios_near_one():
    """Claims ~0.90 realizing ~0.90: no verdict; PIT ratios exactly 1.

    20 markets claim p_win=0.90 on a "below 81" bucket (mu=70, sigma=10). Actual
    values sit at the j/21 quantiles for j=1..20, an exact PIT ladder: fractions
    above/below the q=0.10 and q=0.05 cutoffs come out to exactly 2/20 and 1/20,
    giving ratios of exactly 1.0. Threshold 81 splits the ladder 18 wins / 2
    losses (18/20 = 0.90, matching the claim).
    """
    conn = connect(":memory:")
    init_schema(conn)
    mu, sigma = 70.0, 10.0
    for j in range(1, 21):
        actual = mu + sigma * norm.ppf(j / 21)
        _insert_row(
            conn,
            market_id=f"m2-{j}",
            run_id=f"r2-{j}",
            started_at="2026-05-31T12:00:00+00:00",
            settlement_date="2026-06-01",
            p_win=0.90,
            mu=mu,
            sigma=sigma,
            actual=actual,
            bucket_kind="below",
            threshold=81,
        )
    conn.commit()
    result = compute_tail_calibration(conn)
    conn.close()

    row = _primary_row(result, "TMAX", 1, "YES", "[0.90,0.95)")
    assert row is not None
    assert row["n"] == 20
    assert row["claimed_mean"] == 0.90
    assert row["realized_freq"] == 0.90
    assert row["thin"] is False
    assert row["verdict"] is None

    pit = _pit_row(result, "TMAX", 1)
    assert pit is not None
    assert pit["upper_10"] == 1.0
    assert pit["lower_10"] == 1.0
    assert pit["upper_05"] == 1.0
    assert pit["lower_05"] == 1.0


def test_underconfident_book_flags_under_and_pit_ratios_below_one():
    """Claims ~0.80, realizes 20/20: n >= MIN_TAIL_N flags UNDER; PIT ratios < 1.

    20 markets claim p_win=0.80 on a "below 75" bucket (mu=70, sigma=2) and all
    settle (actual=70, below the threshold): realized frequency 1.0. The Wilson
    lower bound for wins=20/n=20 is ~0.839, above the claimed 0.80, so the claim
    understates the true win rate and must flag UNDER. actual=mu places every
    PIT value at exactly 0.5 (well inside both tails), so all four tail ratios
    come out to exactly 0, which is < 1.
    """
    conn = connect(":memory:")
    init_schema(conn)
    for j in range(MIN_TAIL_N):
        _insert_row(
            conn,
            market_id=f"m8-{j}",
            run_id=f"r8-{j}",
            started_at="2026-05-31T12:00:00+00:00",
            settlement_date="2026-06-01",
            p_win=0.80,
            mu=70.0,
            sigma=2.0,
            actual=70.0,
            bucket_kind="below",
            threshold=75,
        )
    conn.commit()
    result = compute_tail_calibration(conn)
    conn.close()

    row = _primary_row(result, "TMAX", 1, "YES", "[0.75,0.85)")
    assert row is not None
    assert row["n"] == 20
    assert row["claimed_mean"] == 0.80
    assert row["realized_freq"] == 1.0
    assert row["thin"] is False
    assert row["verdict"] == "UNDER"

    pit = _pit_row(result, "TMAX", 1)
    assert pit is not None
    assert pit["n"] == 20
    assert pit["upper_10"] < 1
    assert pit["lower_10"] < 1
    assert pit["upper_05"] < 1
    assert pit["lower_05"] < 1


def test_thin_cell_never_flags():
    """n < MIN_TAIL_N prints (via 'thin') but never gets a verdict, however skewed."""
    conn = connect(":memory:")
    init_schema(conn)
    for j in range(5):
        actual = 72.5 if j < 2 else 76.0
        _insert_row(
            conn,
            market_id=f"m3-{j}",
            run_id=f"r3-{j}",
            started_at="2026-05-31T12:00:00+00:00",
            settlement_date="2026-06-02",  # lead 2, distinct from other tests
            p_win=0.95,
            mu=70.0,
            sigma=2.0,
            actual=actual,
            bucket_kind="range",
            lo=72,
            hi=73,
        )
    conn.commit()
    result = compute_tail_calibration(conn)
    conn.close()

    row = _primary_row(result, "TMAX", 2, "YES", "[0.95,1.0]")
    assert row is not None
    assert row["n"] == 5
    assert row["thin"] is True
    assert row["verdict"] is None


def test_no_tail_mirroring():
    """A YES row at p_win 0.05 that misses the bucket mirrors to a NO [0.95,1.0] win."""
    conn = connect(":memory:")
    init_schema(conn)
    _insert_row(
        conn,
        market_id="m4",
        run_id="r4",
        started_at="2026-05-31T12:00:00+00:00",
        settlement_date="2026-06-03",  # lead 3, distinct from other tests
        p_win=0.05,
        mu=70.0,
        sigma=2.0,
        actual=76.0,  # outside [72,73]: bucket does not settle
        bucket_kind="range",
        lo=72,
        hi=73,
    )
    conn.commit()
    result = compute_tail_calibration(conn)
    conn.close()

    row = _primary_row(result, "TMAX", 3, "NO", "[0.95,1.0]")
    assert row is not None
    assert row["n"] == 1
    assert row["wins"] == 1
    assert row["claimed_mean"] == 0.95


def test_dedup_default_per_market_day_by_hour_splits_it():
    """Same-day intraday runs collapse to one by default; --by-hour keeps them separate."""
    conn = connect(":memory:")
    init_schema(conn)
    spec = json.dumps([{"label": "B", "kind": "range", "lo": 70, "hi": 71, "threshold": None}])
    conn.execute(
        "INSERT INTO markets (id, city, variable, settlement_date, outcome_spec) "
        "VALUES (?, ?, ?, ?, ?)",
        ("m5", "NYC", "TMAX", "2026-06-01", spec),
    )
    conn.execute(
        "INSERT INTO outcomes (market_id, actual_value, settled_at) VALUES (?, ?, ?)",
        ("m5", 70.0, "t"),
    )
    # Two runs on the market, same UTC day (2026-05-31), different hours.
    dist = json.dumps({"mu": 70.0, "sigma": 2.0, "n_sources": 2})
    for run_id, started_at in (
        ("r1", "2026-05-31T09:00:00+00:00"),
        ("r2", "2026-05-31T12:00:00+00:00"),
    ):
        conn.execute(
            "INSERT INTO runs (id, started_at, status) VALUES (?, ?, ?)", (run_id, started_at, "ok")
        )
        conn.execute(
            "INSERT INTO predictions "
            "(run_id, market_id, bucket, p_win, dist_params, edge, recommended, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, "m5", "B", 0.80, dist, 0.1, 1, "t"),
        )
    conn.commit()

    default_result = compute_tail_calibration(conn, by_hour=False)
    hourly_result = compute_tail_calibration(conn, by_hour=True)
    conn.close()

    pit_default = _pit_row(default_result, "TMAX", 1)
    assert pit_default is not None
    assert pit_default["n"] == 1  # only the latest run (12:00) survives

    pit_9 = _pit_row(hourly_result, "TMAX", 1, hour=9)
    pit_12 = _pit_row(hourly_result, "TMAX", 1, hour=12)
    assert pit_9 is not None and pit_9["n"] == 1
    assert pit_12 is not None and pit_12["n"] == 1


def test_prcp_excluded_and_variable_lead_keys_stay_separate():
    conn = connect(":memory:")
    init_schema(conn)
    _insert_row(
        conn,
        market_id="m6-prcp",
        run_id="r6-prcp",
        started_at="2026-05-31T12:00:00+00:00",
        settlement_date="2026-06-01",
        p_win=0.90,
        mu=2.0,
        sigma=0.5,
        actual=2.2,
        bucket_kind="range",
        lo=2,
        hi=3,
        variable="PRCP",
    )
    _insert_row(
        conn,
        market_id="m6-tmax",
        run_id="r6-tmax",
        started_at="2026-05-31T12:00:00+00:00",
        settlement_date="2026-06-01",  # lead 1
        p_win=0.80,
        mu=70.0,
        sigma=2.0,
        actual=70.0,
        bucket_kind="range",
        lo=70,
        hi=71,
        variable="TMAX",
    )
    _insert_row(
        conn,
        market_id="m6-tmin",
        run_id="r6-tmin",
        started_at="2026-05-30T12:00:00+00:00",
        settlement_date="2026-06-01",  # lead 2
        p_win=0.80,
        mu=50.0,
        sigma=2.0,
        actual=50.0,
        bucket_kind="range",
        lo=50,
        hi=51,
        variable="TMIN",
    )
    conn.commit()
    result = compute_tail_calibration(conn)
    conn.close()

    assert all(row["variable"] != "PRCP" for row in result["primary"])
    assert all(row["variable"] != "PRCP" for row in result["pit"])
    assert _pit_row(result, "TMAX", 1) is not None
    assert _pit_row(result, "TMIN", 2) is not None


def test_since_filter_restricts_to_runs_on_or_after_the_cutoff():
    """--since filters both the primary and PIT populations on runs.started_at.

    Two runs on distinct markets straddle the cutoff date. Without `since`, both
    count; with `since` set to the cutoff, only the later run's row survives.
    """
    conn = connect(":memory:")
    init_schema(conn)
    _insert_row(
        conn,
        market_id="m9-before",
        run_id="r9-before",
        started_at="2026-07-05T12:00:00+00:00",
        settlement_date="2026-07-06",
        p_win=0.90,
        mu=70.0,
        sigma=10.0,
        actual=70.0,
        bucket_kind="below",
        threshold=75,
    )
    _insert_row(
        conn,
        market_id="m9-after",
        run_id="r9-after",
        started_at="2026-07-06T12:00:00+00:00",
        settlement_date="2026-07-07",
        p_win=0.90,
        mu=70.0,
        sigma=10.0,
        actual=70.0,
        bucket_kind="below",
        threshold=75,
    )
    conn.commit()

    unfiltered = compute_tail_calibration(conn)
    filtered = compute_tail_calibration(conn, since="2026-07-06")
    conn.close()

    row_unfiltered = _primary_row(unfiltered, "TMAX", 1, "YES", "[0.90,0.95)")
    assert row_unfiltered is not None
    assert row_unfiltered["n"] == 2

    row_filtered = _primary_row(filtered, "TMAX", 1, "YES", "[0.90,0.95)")
    assert row_filtered is not None
    assert row_filtered["n"] == 1

    pit_unfiltered = _pit_row(unfiltered, "TMAX", 1)
    assert pit_unfiltered is not None
    assert pit_unfiltered["n"] == 2

    pit_filtered = _pit_row(filtered, "TMAX", 1)
    assert pit_filtered is not None
    assert pit_filtered["n"] == 1


def test_compute_calibration_by_cell_agrees_with_tail_calibration_on_n_at_since_boundary():
    """The two commands' populations differ (settled_rows() requires a price join,
    compute_tail_calibration's own queries do not, #323's sub-plan), but when a row
    is visible to both, both must count it the same way. Same fixture shape as
    test_since_filter_restricts_to_runs_on_or_after_the_cutoff, with a price row
    added so settled_rows() also sees it, and the second run's started_at set to
    exactly the since cutoff (the boundary-exact case: on-or-after in both).
    """
    conn = connect(":memory:")
    init_schema(conn)
    for market_id, run_id, started_at, settlement_date in (
        ("m10-before", "r10-before", "2026-07-05T12:00:00+00:00", "2026-07-06"),
        ("m10-after", "r10-after", "2026-07-06T00:00:00+00:00", "2026-07-07"),
    ):
        _insert_row(
            conn,
            market_id=market_id,
            run_id=run_id,
            started_at=started_at,
            settlement_date=settlement_date,
            p_win=0.90,
            mu=70.0,
            sigma=10.0,
            actual=70.0,
            bucket_kind="below",
            threshold=75,
        )
        conn.execute(
            "INSERT INTO prices (run_id, market_id, outcome, price, implied_prob, captured_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, market_id, "B", 0.5, 0.5, "t"),
        )
    conn.commit()

    tail_unfiltered = compute_tail_calibration(conn)
    tail_filtered = compute_tail_calibration(conn, since="2026-07-06")
    rows = settled_rows(conn)
    conn.close()

    cell_unfiltered = compute_calibration_by_cell(rows)
    cell_filtered = compute_calibration_by_cell(rows, since="2026-07-06")

    tail_row_unfiltered = _primary_row(tail_unfiltered, "TMAX", 1, "YES", "[0.90,0.95)")
    tail_row_filtered = _primary_row(tail_filtered, "TMAX", 1, "YES", "[0.90,0.95)")
    assert tail_row_unfiltered is not None and tail_row_unfiltered["n"] == 2
    # boundary-exact: m10-after started at 00:00:00 on the cutoff date, still on-or-after
    assert tail_row_filtered is not None and tail_row_filtered["n"] == 1

    cell_row_unfiltered = next(
        r for r in cell_unfiltered if r["variable"] == "TMAX" and r["lead_time"] == 1
    )
    cell_row_filtered = next(
        r for r in cell_filtered if r["variable"] == "TMAX" and r["lead_time"] == 1
    )
    assert cell_row_unfiltered["n"] == tail_row_unfiltered["n"] == 2
    assert cell_row_filtered["n"] == tail_row_filtered["n"] == 1


# ---------------------------------------------------------------------------
# Regression pin (#292): captured from the pre-family-aware implementation on
# a Gaussian-only, no-df-key (historical shape) seeded store. Family-aware
# tracking must reproduce these exact numbers for rows with no "df" key.
# ---------------------------------------------------------------------------


def test_compute_tail_calibration_gaussian_only_regression_pin():
    conn = connect(":memory:")
    init_schema(conn)
    _insert_row(
        conn,
        market_id="p1",
        run_id="rp1",
        started_at="2026-07-01T12:00:00+00:00",
        settlement_date="2026-07-02",
        p_win=0.72,
        mu=70.0,
        sigma=2.0,
        actual=70.5,
        bucket_kind="range",
        lo=70,
        hi=71,
        bucket_label="70-71°F",
        variable="TMAX",
    )
    _insert_row(
        conn,
        market_id="p2",
        run_id="rp2",
        started_at="2026-07-01T12:00:00+00:00",
        settlement_date="2026-07-02",
        p_win=0.55,
        mu=71.0,
        sigma=3.0,
        actual=74.0,
        bucket_kind="range",
        lo=72,
        hi=73,
        bucket_label="72-73°F",
        variable="TMAX",
    )
    _insert_row(
        conn,
        market_id="p3",
        run_id="rp3",
        started_at="2026-07-01T12:00:00+00:00",
        settlement_date="2026-07-03",
        p_win=0.30,
        mu=65.0,
        sigma=2.5,
        actual=68.5,
        bucket_kind="range",
        lo=68,
        hi=69,
        bucket_label="68-69°F",
        variable="TMAX",
    )
    _insert_row(
        conn,
        market_id="p4",
        run_id="rp4",
        started_at="2026-07-01T09:00:00+00:00",
        settlement_date="2026-07-03",
        p_win=0.61,
        mu=49.0,
        sigma=1.8,
        actual=50.2,
        bucket_kind="range",
        lo=50,
        hi=51,
        bucket_label="50-51°F",
        variable="TMIN",
    )
    _insert_row(
        conn,
        market_id="p5",
        run_id="rp5",
        started_at="2026-07-01T09:00:00+00:00",
        settlement_date="2026-07-04",
        p_win=0.44,
        mu=47.0,
        sigma=2.2,
        actual=44.0,
        bucket_kind="range",
        lo=45,
        hi=46,
        bucket_label="45-46°F",
        variable="TMIN",
    )
    conn.commit()
    result = compute_tail_calibration(conn)
    conn.close()

    row = _primary_row(result, "TMAX", 1, "YES", "<0.75")
    assert row is not None
    assert row["n"] == 2
    assert row["wins"] == 1
    assert row["claimed_mean"] == pytest.approx(0.635, abs=1e-12)
    assert row["realized_freq"] == pytest.approx(0.5, abs=1e-12)
    assert row["wilson_lo"] == pytest.approx(0.09452865480086614, abs=1e-12)
    assert row["wilson_hi"] == pytest.approx(0.9054713451991339, abs=1e-12)
    assert row["thin"] is True
    assert row["verdict"] is None

    row = _primary_row(result, "TMAX", 2, "NO", "<0.75")
    assert row is not None
    assert row["n"] == 1
    assert row["wins"] == 0
    assert row["claimed_mean"] == pytest.approx(0.7, abs=1e-12)
    assert row["realized_freq"] == pytest.approx(0.0, abs=1e-12)
    assert row["wilson_hi"] == pytest.approx(0.7934567085261071, abs=1e-12)

    row = _primary_row(result, "TMIN", 2, "YES", "<0.75")
    assert row is not None
    assert row["n"] == 1
    assert row["wins"] == 1
    assert row["claimed_mean"] == pytest.approx(0.61, abs=1e-12)
    assert row["wilson_lo"] == pytest.approx(0.20654329147389294, abs=1e-12)

    row = _primary_row(result, "TMIN", 3, "NO", "<0.75")
    assert row is not None
    assert row["n"] == 1
    assert row["wins"] == 1
    assert row["claimed_mean"] == pytest.approx(0.56, abs=1e-12)
    assert row["wilson_lo"] == pytest.approx(0.20654329147389294, abs=1e-12)

    pit = _pit_row(result, "TMAX", 1)
    assert pit is not None
    assert pit["n"] == 2
    assert pit["upper_10"] == pytest.approx(0.0, abs=1e-12)
    assert pit["lower_10"] == pytest.approx(0.0, abs=1e-12)
    assert pit["upper_05"] == pytest.approx(0.0, abs=1e-12)
    assert pit["lower_05"] == pytest.approx(0.0, abs=1e-12)

    pit = _pit_row(result, "TMAX", 2)
    assert pit is not None
    assert pit["n"] == 1
    assert pit["upper_10"] == pytest.approx(10.0, abs=1e-12)
    assert pit["lower_10"] == pytest.approx(0.0, abs=1e-12)
    assert pit["upper_05"] == pytest.approx(0.0, abs=1e-12)
    assert pit["lower_05"] == pytest.approx(0.0, abs=1e-12)

    pit = _pit_row(result, "TMIN", 2)
    assert pit is not None
    assert pit["n"] == 1
    assert pit["upper_10"] == pytest.approx(0.0, abs=1e-12)
    assert pit["lower_10"] == pytest.approx(0.0, abs=1e-12)

    pit = _pit_row(result, "TMIN", 3)
    assert pit is not None
    assert pit["n"] == 1
    assert pit["upper_10"] == pytest.approx(0.0, abs=1e-12)
    assert pit["lower_10"] == pytest.approx(10.0, abs=1e-12)


# ---------------------------------------------------------------------------
# Family-aware PIT (#292): the secondary ("pit") table computes each PIT with
# the row's own family instead of always norm.cdf.
# ---------------------------------------------------------------------------


def _insert_row_with_df(
    conn,
    market_id: str,
    run_id: str,
    *,
    started_at: str,
    settlement_date: str,
    p_win: float,
    mu: float,
    sigma: float,
    df,
    actual: float,
    bucket_kind: str,
    lo: float | None = None,
    hi: float | None = None,
    threshold: float | None = None,
    variable: str = "TMIN",
    bucket_label: str = "B",
):
    conn.execute(
        "INSERT OR IGNORE INTO runs (id, started_at, status) VALUES (?, ?, ?)",
        (run_id, started_at, "ok"),
    )
    spec = json.dumps(
        [{"label": bucket_label, "kind": bucket_kind, "lo": lo, "hi": hi, "threshold": threshold}]
    )
    conn.execute(
        "INSERT INTO markets (id, city, variable, settlement_date, outcome_spec) "
        "VALUES (?, ?, ?, ?, ?)",
        (market_id, "NYC", variable, settlement_date, spec),
    )
    dist = json.dumps({"mu": mu, "sigma": sigma, "n_sources": 2, "df": df})
    conn.execute(
        "INSERT INTO predictions "
        "(run_id, market_id, bucket, p_win, dist_params, edge, recommended, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, market_id, bucket_label, p_win, dist, 0.1, 1, "t"),
    )
    conn.execute(
        "INSERT INTO outcomes (market_id, actual_value, settled_at) VALUES (?, ?, ?)",
        (market_id, actual, "t"),
    )


def test_compute_tail_calibration_pit_uses_family_aware_cdf_for_student_t_row():
    """A Student-t row's PIT is t.cdf-based, not norm.cdf-based: the ratio at
    this construction differs materially between the two families."""
    from scipy.stats import t as student_t

    conn = connect(":memory:")
    init_schema(conn)
    # z=1.3: norm.cdf(1.3)=0.9032 (crosses 0.90) but student_t.cdf(1.3, df=5)=0.8748
    # (does not): the two families land on opposite sides of the upper_10 cutoff, so
    # this only passes if compute_tail_calibration is genuinely family-aware.
    mu, sigma, df = 50.0, 2.0, 5.0
    actual = mu + 1.3 * sigma
    _insert_row_with_df(
        conn,
        market_id="p1",
        run_id="rp1",
        started_at="2026-07-01T12:00:00+00:00",
        settlement_date="2026-07-02",
        p_win=0.70,
        mu=mu,
        sigma=sigma,
        df=df,
        actual=actual,
        bucket_kind="range",
        lo=50,
        hi=51,
    )
    conn.commit()
    result = compute_tail_calibration(conn)
    conn.close()

    pit = _pit_row(result, "TMIN", 1)
    assert pit is not None
    assert pit["n"] == 1
    z = (actual - mu) / sigma
    t_pit = float(student_t.cdf(z, df))
    normal_pit = float(norm.cdf(z))
    assert t_pit <= 0.90 < normal_pit  # families land on opposite sides of the cutoff
    # upper_10 is 1/1 divided by q=0.10 (=10.0) exactly when the PIT clears 1-0.10=0.90.
    assert pit["upper_10"] == 0.0  # the t-family PIT does not clear 0.90


def test_compute_tail_calibration_pit_skips_invalid_df():
    """A present-but-invalid df (non-numeric or <= 0) skips the row from the
    pit population, like the existing sigma<=0 guard."""
    conn = connect(":memory:")
    init_schema(conn)
    _insert_row_with_df(
        conn,
        market_id="p1",
        run_id="rp1",
        started_at="2026-07-01T12:00:00+00:00",
        settlement_date="2026-07-02",
        p_win=0.70,
        mu=50.0,
        sigma=2.0,
        df="bogus",
        actual=55.0,
        bucket_kind="range",
        lo=50,
        hi=51,
    )
    conn.commit()
    result = compute_tail_calibration(conn)
    conn.close()

    assert _pit_row(result, "TMIN", 1) is None  # excluded, never scored as Gaussian


# ---------------------------------------------------------------------------
# Non-finite dist_params guard hardening (#304)
# ---------------------------------------------------------------------------


def test_compute_tail_calibration_excludes_nan_df_row():
    """A NaN df must not poison the PIT population: pre-fix, NaN slips past the
    df<=0-only guard (NaN comparisons are always False), and json round-trips it
    unchanged, so std_cdf_for(nan) corrupts the group with a NaN PIT.
    """
    conn = connect(":memory:")
    init_schema(conn)
    _insert_row_with_df(
        conn,
        market_id="m1",
        run_id="r1",
        started_at="2026-07-01T12:00:00+00:00",
        settlement_date="2026-07-02",
        p_win=0.70,
        mu=70.0,
        sigma=2.0,
        df=5.0,
        actual=70.5,
        bucket_kind="range",
        lo=70,
        hi=71,
        bucket_label="70-71°F",
        variable="TMIN",
    )
    _insert_row_with_df(
        conn,
        market_id="m2",
        run_id="r2",
        started_at="2026-07-01T12:00:00+00:00",
        settlement_date="2026-07-02",
        p_win=0.55,
        mu=71.0,
        sigma=3.0,
        df=float("nan"),
        actual=74.0,
        bucket_kind="range",
        lo=72,
        hi=73,
        bucket_label="72-73°F",
        variable="TMIN",
    )
    conn.commit()

    result = compute_tail_calibration(conn)
    conn.close()

    pit = _pit_row(result, "TMIN", 1)
    assert pit is not None
    assert pit["n"] == 1  # only the df=5.0 row; the NaN-df row is excluded
    assert math.isfinite(pit["upper_10"])
    assert math.isfinite(pit["lower_10"])
    assert math.isfinite(pit["upper_05"])
    assert math.isfinite(pit["lower_05"])


def test_cli_tail_check_smoke(tmp_path, capsys):
    db = str(tmp_path / "tail.db")
    conn = connect(db)
    init_schema(conn)
    for j in range(MIN_TAIL_N):
        actual = 72.5 if j < 10 else 76.0
        _insert_row(
            conn,
            market_id=f"m7-{j}",
            run_id=f"r7-{j}",
            started_at="2026-05-31T12:00:00+00:00",
            settlement_date="2026-06-01",
            p_win=0.95,
            mu=70.0,
            sigma=2.0,
            actual=actual,
            bucket_kind="range",
            lo=72,
            hi=73,
        )
    conn.commit()
    conn.close()

    _tail_check(db)

    out = capsys.readouterr().out
    assert "Claimed vs realized" in out
    assert "PIT tail-occurrence" in out
    assert "TMAX" in out
    assert "OVER" in out
    assert "since:" not in out  # omitting --since prints no filter line
    # n=20, q=0.10 -> exp 2.00; q=0.05 -> exp 1.00; 10 of 20 PITs sit above
    # both cutoffs (upper tail), none below (lower tail).
    assert "( 10/  2.00)" in out  # upper_10 obs/exp
    assert "(  0/  2.00)" in out  # lower_10 obs/exp
    assert "( 10/  1.00)" in out  # upper_05 obs/exp
    assert "(  0/  1.00)" in out  # lower_05 obs/exp

    _tail_check(db, since="2026-05-01")  # before the fixture rows: prints the filter line

    out_since = capsys.readouterr().out
    assert "since: 2026-05-01" in out_since
    # test_cli_tail_check_since_filters_rows below covers the case where
    # --since actually excludes rows; this call only proves the printer
    # doesn't crash on that branch and still shows the filter line.
    assert "( 10/  2.00)" in out_since

    _tail_check(db, by_hour=True)

    out_hourly = capsys.readouterr().out
    assert "( 10/  2.00)" in out_hourly  # counts survive --by-hour too


def test_cli_tail_check_since_filters_rows(tmp_path, capsys):
    """--since excludes real rows, and the printed counts reflect only survivors.

    Two PIT groups for the same (variable, lead) straddle a cutoff: an older
    group of 10 miscalibrated rows (all hitting the upper tail) and a newer
    group of 10 calibrated rows (none hitting it). Unfiltered, the ratios and
    counts are the pooled 20; filtered to since=cutoff, the older group's 10
    hits must disappear from the printed output, not just from a `pit["n"]`
    check on the underlying function.
    """
    db = str(tmp_path / "tail.db")
    conn = connect(db)
    init_schema(conn)
    for j in range(10):  # before the cutoff: every actual sits in the upper tail
        _insert_row(
            conn,
            market_id=f"m10-before-{j}",
            run_id=f"r10-before-{j}",
            started_at="2026-05-31T12:00:00+00:00",
            settlement_date="2026-06-01",
            p_win=0.95,
            mu=70.0,
            sigma=2.0,
            actual=76.0,
            bucket_kind="range",
            lo=72,
            hi=73,
        )
    for j in range(10):  # after the cutoff: every actual sits well inside the bucket
        _insert_row(
            conn,
            market_id=f"m10-after-{j}",
            run_id=f"r10-after-{j}",
            started_at="2026-07-10T12:00:00+00:00",
            settlement_date="2026-07-11",
            p_win=0.95,
            mu=70.0,
            sigma=2.0,
            actual=72.5,
            bucket_kind="range",
            lo=72,
            hi=73,
        )
    conn.commit()
    conn.close()

    _tail_check(db)
    out_unfiltered = capsys.readouterr().out
    # n=20 pooled, q=0.10 -> exp 2.00; the 10 pre-cutoff hits are visible.
    assert "( 10/  2.00)" in out_unfiltered  # upper_10 obs/exp

    _tail_check(db, since="2026-07-01")  # excludes the pre-cutoff group
    out_filtered = capsys.readouterr().out
    assert "since: 2026-07-01" in out_filtered
    assert "( 10/" not in out_filtered  # the pre-cutoff hits must not survive
    # n=10 survives (post-cutoff group only), q=0.10 -> exp 1.00, zero hits.
    assert "(  0/  1.00)" in out_filtered  # upper_10 obs/exp


def test_tail_check_pit_columns_stay_aligned_at_four_digit_counts(tmp_path, capsys, monkeypatch):
    """A 4-digit obs count or a 4-digit exp value must not shift later columns.

    A fixed-width obs/exp field overflows once a PIT group's n reaches four
    figures (obs >= 1000, or exp = n*q >= 1000.00), which is no longer a
    hypothetical: production history already sits at n=1005 for one cell. The
    printer's PIT section must render every row and the header at the same
    line length regardless of how wide any one row's counts get.
    """
    db = str(tmp_path / "tail.db")
    conn = connect(db)
    init_schema(conn)
    conn.commit()
    conn.close()

    small_row = {
        "variable": "TMAX",
        "lead_time": 1,
        "hour": None,
        "n": 20,
        "upper_10": 5.00,
        "upper_10_obs": 10,
        "upper_10_exp": 2.00,
        "lower_10": 0.00,
        "lower_10_obs": 0,
        "lower_10_exp": 2.00,
        "upper_05": 10.00,
        "upper_05_obs": 10,
        "upper_05_exp": 1.00,
        "lower_05": 0.00,
        "lower_05_obs": 0,
        "lower_05_exp": 1.00,
    }
    # n is deliberately left small: n itself would need to reach 10_000 to
    # produce a real exp of 1000.00 at q=0.10, which would also overflow the
    # pre-existing 'n' column (out of scope for this fix, per the tester's
    # finding). Faking obs/exp directly isolates the obs/exp fix from that
    # separate, untouched fragility.
    large_row = {
        "variable": "TMAX",
        "lead_time": 2,
        "hour": None,
        "n": 999,
        "upper_10": 10.00,
        "upper_10_obs": 10_000,
        "upper_10_exp": 1000.00,
        "lower_10": 0.00,
        "lower_10_obs": 0,
        "lower_10_exp": 1000.00,
        "upper_05": 20.00,
        "upper_05_obs": 10_000,
        "upper_05_exp": 500.00,
        "lower_05": 0.00,
        "lower_05_obs": 0,
        "lower_05_exp": 500.00,
    }
    fake_result = {"primary": [], "pit": [small_row, large_row]}
    monkeypatch.setattr(
        "rainmaker.cli.compute_tail_calibration",
        lambda conn, by_hour=False, since=None: fake_result,
    )

    _tail_check(db)

    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    pit_header_and_rows = lines[-3:]  # PIT header, small_row, large_row
    widths = {len(line) for line in pit_header_and_rows}
    assert len(widths) == 1, f"PIT lines are not equal width: {pit_header_and_rows}"
