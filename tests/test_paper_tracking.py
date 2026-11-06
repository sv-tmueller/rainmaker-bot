"""Paper-order grading, regrade, and the go-live report (#492)."""

import json
import re
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from rainmaker.forecasts.wrh import SYNOPTIC_API_URL
from rainmaker.paper_tracking import (
    GO_LIVE_MAX_GAP_USDC,
    GO_LIVE_MIN_DATES,
    GO_LIVE_MIN_NET_ROI,
    GO_LIVE_MIN_ORDERS,
    PAPER_ERAS,
    ROI_INTERVAL_LEVEL,
    ROI_INTERVAL_MIN_DATES,
    ROI_INTERVAL_RESAMPLES,
    PaperEra,
    compute_paper_report,
    grade_paper_orders,
    load_paper_rows,
    paper_pnl,
    regrade_paper_orders_for_market,
    render_paper_report,
)
from rainmaker.settle import regrade_polymarket_settlements, run_settlement
from rainmaker.store.db import READ_METER, connect, init_schema
from rainmaker.store.record import record_outcome, record_paper_order

ERA_START = datetime(2026, 11, 3, 7, 36, 10, tzinfo=UTC)
T_IN_ERA = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)

_TMAX_SPEC = [
    {"label": "59°F or below", "kind": "below", "lo": None, "hi": None, "threshold": 59},
    {"label": "60-64°F", "kind": "range", "lo": 60, "hi": 64, "threshold": None},
    {"label": "65°F or higher", "kind": "above", "lo": None, "hi": None, "threshold": 65},
]
_PRCP_SPEC = [
    {"label": '<1"', "kind": "below", "lo": None, "hi": None, "threshold": 1.0},
    {"label": '1-2"', "kind": "range", "lo": 1.0, "hi": 2.0, "threshold": None},
    {"label": '>2"', "kind": "above", "lo": None, "hi": None, "threshold": 2.0},
]


@pytest.fixture
def conn():
    c = connect(":memory:")
    init_schema(c)
    c.execute("INSERT INTO runs (id, started_at, status) VALUES ('r1', 't', 'ok')")
    c.commit()
    yield c
    c.close()


def _market(conn, market_id, variable="TMAX", spec=_TMAX_SPEC, settlement_date="2026-11-05"):
    conn.execute(
        "INSERT INTO markets (id, city, variable, settlement_date, outcome_spec, venue) "
        "VALUES (?, 'NYC', ?, ?, ?, 'polymarket')",
        (market_id, variable, settlement_date, json.dumps(spec) if spec is not None else None),
    )
    conn.commit()


def _order(conn, **overrides):
    kwargs = {
        "run_id": "r1",
        "market_id": "m1",
        "outcome": "60-64°F",
        "side": "YES",
        "token_id": "tok",
        "status": "filled",
        "stake_usdc": 5.0,
        "fill_price": 0.5,
        "shares": 10.0,
        "fee_usdc": 0.125,
        "p_win": 0.6,
        "edge": 0.1,
        "quoted_ask": 0.5,
        "limit_price": 0.55,
        "book_snapshot": "{}",
        "created_at": T_IN_ERA,
    }
    kwargs.update(overrides)
    record_paper_order(conn, **kwargs)


def _won_pnl(conn):
    return [
        (r["won"], r["pnl_usdc"])
        for r in conn.execute("SELECT won, pnl_usdc FROM paper_orders ORDER BY id").fetchall()
    ]


# --- paper_pnl ------------------------------------------------------------


def test_paper_pnl_win_pays_one_per_share_less_outlay():
    assert paper_pnl(10.0, 0.5, 0.125, True) == pytest.approx(4.875)


def test_paper_pnl_loss_is_minus_outlay():
    assert paper_pnl(10.0, 0.5, 0.125, False) == pytest.approx(-5.125)


def test_paper_pnl_partial_win():
    assert paper_pnl(4.0, 0.75, 0.0375, True) == pytest.approx(0.9625)


def test_paper_pnl_treats_a_missing_fee_as_zero():
    assert paper_pnl(10.0, 0.5, None, True) == pytest.approx(5.0)


# --- grade_paper_orders ---------------------------------------------------


def test_grades_a_winning_yes_fill(conn):
    _market(conn, "m1")
    _order(conn)
    record_outcome(conn, "m1", 62.0, "t")  # lands in 60-64
    assert grade_paper_orders(conn) == 1
    assert _won_pnl(conn) == [(1, pytest.approx(4.875))]


def test_grades_a_losing_yes_fill(conn):
    _market(conn, "m1")
    _order(conn)
    record_outcome(conn, "m1", 70.0, "t")
    grade_paper_orders(conn)
    assert _won_pnl(conn) == [(0, pytest.approx(-5.125))]


def test_grades_a_partial_fill(conn):
    _market(conn, "m1")
    _order(conn, status="partial", shares=4.0, fill_price=0.75, fee_usdc=0.0375)
    record_outcome(conn, "m1", 62.0, "t")
    grade_paper_orders(conn)
    assert _won_pnl(conn) == [(1, pytest.approx(0.9625))]


def test_no_fill_is_never_graded_even_with_walked_shares_and_a_price(conn):
    # A below-minimum no_fill row carries a non-null fill_price (see the lead's decision).
    _market(conn, "m1")
    _order(conn, status="no_fill", shares=2.0, fill_price=0.5, fee_usdc=0.01)
    record_outcome(conn, "m1", 62.0, "t")
    assert grade_paper_orders(conn) == 0
    assert _won_pnl(conn) == [(None, None)]


def test_open_market_is_not_graded(conn):
    _market(conn, "m1")
    _order(conn)
    assert grade_paper_orders(conn) == 0
    assert _won_pnl(conn) == [(None, None)]


def test_no_side_wins_when_the_bucket_does_not_settle(conn):
    _market(conn, "m1")
    _order(conn, side="NO", fill_price=0.75, shares=4.0, fee_usdc=0.0375)
    record_outcome(conn, "m1", 70.0, "t")  # 60-64 does not settle: NO wins
    grade_paper_orders(conn)
    assert _won_pnl(conn) == [(1, pytest.approx(0.9625))]


def test_no_side_loses_when_the_bucket_settles(conn):
    _market(conn, "m1")
    _order(conn, side="NO", fill_price=0.75, shares=4.0, fee_usdc=0.0375)
    record_outcome(conn, "m1", 62.0, "t")
    grade_paper_orders(conn)
    assert _won_pnl(conn) == [(0, pytest.approx(-3.0375))]


def test_precip_market_grades_through_precip_settles(conn):
    _market(conn, "m1", variable="PRCP", spec=_PRCP_SPEC)
    _order(conn, outcome='1-2"')
    record_outcome(conn, "m1", 1.4, "t")
    grade_paper_orders(conn)
    assert _won_pnl(conn)[0][0] == 1


def test_ungradable_label_stays_null_and_does_not_block_other_rows(conn):
    _market(conn, "m1", spec=None)
    _order(conn, outcome="inches")
    _order(conn, outcome="60-64°F")
    record_outcome(conn, "m1", 62.0, "t")
    assert grade_paper_orders(conn) == 1
    assert _won_pnl(conn)[0] == (None, None)
    assert _won_pnl(conn)[1][0] == 1


def test_grading_is_idempotent(conn):
    _market(conn, "m1")
    _order(conn)
    record_outcome(conn, "m1", 62.0, "t")
    assert grade_paper_orders(conn) == 1
    first = _won_pnl(conn)
    assert grade_paper_orders(conn) == 0
    assert _won_pnl(conn) == first


def test_paper_won_agrees_with_the_advisory_prediction_grade(conn):
    _market(conn, "m1")
    for side in ("YES", "NO"):
        conn.execute(
            "INSERT INTO predictions (run_id, market_id, bucket, side, p_win, edge, "
            "recommended, created_at) VALUES ('r1', 'm1', '60-64°F', ?, 0.6, 0.1, 1, 't')",
            (side,),
        )
        _order(conn, side=side)
    conn.commit()
    record_outcome(conn, "m1", 62.0, "t")
    from rainmaker.settle import _grade_predictions

    _grade_predictions(conn)
    grade_paper_orders(conn)
    pred = [r["won"] for r in conn.execute("SELECT won FROM predictions ORDER BY id")]
    paper = [r["won"] for r in conn.execute("SELECT won FROM paper_orders ORDER BY id")]
    assert paper == pred == [1, 0]


# --- settle wiring --------------------------------------------------------


def test_run_settlement_grades_paper_orders_without_any_fetch(conn):
    # The market is already settled, so run_settlement makes no HTTP call, but the
    # grading pass still runs and picks up the new paper order.
    _market(conn, "m1")
    _order(conn)
    record_outcome(conn, "m1", 62.0, "t")
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail("no http"))) as client:
        run_settlement(conn, client, date(2026, 11, 10), "2026-11-10T00:00:00Z")
    assert _won_pnl(conn) == [(1, pytest.approx(4.875))]


# --- regrade --------------------------------------------------------------


def test_regrade_flips_won_and_pnl(conn):
    _market(conn, "m1")
    _order(conn)
    record_outcome(conn, "m1", 62.0, "t")
    grade_paper_orders(conn)
    record_outcome(conn, "m1", 70.0, "t2")
    regrade_paper_orders_for_market(conn, "m1", 70.0)
    assert _won_pnl(conn) == [(0, pytest.approx(-5.125))]


def test_regrade_clears_a_now_ungradable_row_to_null(conn):
    # The label "inches" has no parseable form, so only the stored spec grades it.
    spec = [{"label": "inches", "kind": "range", "lo": 60, "hi": 64, "threshold": None}]
    _market(conn, "m1", spec=spec)
    _order(conn, outcome="inches")
    record_outcome(conn, "m1", 62.0, "t")
    grade_paper_orders(conn)
    assert _won_pnl(conn) == [(1, pytest.approx(4.875))]
    conn.execute("UPDATE markets SET outcome_spec = NULL WHERE id = 'm1'")
    conn.commit()
    regrade_paper_orders_for_market(conn, "m1", 62.0)
    assert _won_pnl(conn) == [(None, None)]


def test_regrade_leaves_no_fill_rows_and_other_markets_alone(conn):
    _market(conn, "m1")
    _market(conn, "m2")
    _order(conn, status="no_fill", shares=0.0, fill_price=None, fee_usdc=None)
    _order(conn, market_id="m2")
    record_outcome(conn, "m2", 62.0, "t")
    grade_paper_orders(conn)
    regrade_paper_orders_for_market(conn, "m1", 62.0)
    assert _won_pnl(conn) == [(None, None), (1, pytest.approx(4.875))]


def test_regrade_polymarket_settlements_regrades_paper_orders(conn, httpx_mock):
    wrh_64f = (Path(__file__).parent / "fixtures" / "wrh_klga_2026-05-30_64f.json").read_text()
    _market(conn, "m1", settlement_date="2026-05-30")
    _order(conn, outcome="65°F or higher")
    record_outcome(conn, "m1", 65.0, "2026-05-31T00:00:00Z")
    grade_paper_orders(conn)
    assert _won_pnl(conn)[0][0] == 1
    httpx_mock.add_response(url=re.compile(re.escape(SYNOPTIC_API_URL)), text=wrh_64f)
    with httpx.Client() as client:
        regrade_polymarket_settlements(conn, client, "2026-06-15T00:00:00Z")
    assert _won_pnl(conn) == [(0, pytest.approx(-5.125))]


# --- egress ---------------------------------------------------------------


def test_grading_and_loading_never_read_the_book_snapshot(conn):
    _market(conn, "m1")
    for _ in range(3):
        _order(conn, book_snapshot="x" * 1_000_000)
    record_outcome(conn, "m1", 62.0, "t")
    READ_METER.reset()
    grade_paper_orders(conn)
    load_paper_rows(conn)
    assert READ_METER.bytes < 100_000


def test_paper_tracking_never_selects_star():
    import rainmaker.paper_tracking as module

    assert "SELECT *" not in Path(module.__file__).read_text().upper()


def test_paper_report_command_never_reads_the_book_snapshot(tmp_path, capsys):
    from rainmaker import cli

    db = str(tmp_path / "t.db")
    c = connect(db)
    init_schema(c)
    c.execute("INSERT INTO runs (id, started_at, status) VALUES ('r1', 't', 'ok')")
    _market(c, "m1")
    for _ in range(3):
        _order(c, book_snapshot="x" * 1_000_000)
    c.close()
    cli.main(["paper-report", "--db", db])
    assert "store read:" in capsys.readouterr().err
    assert READ_METER.bytes < 100_000


# --- report: known-answer set ---------------------------------------------


def _known_answer_store(conn):
    conn.execute("INSERT INTO runs (id, started_at, status) VALUES ('r2', 't', 'ok')")
    _market(conn, "mA", settlement_date="2026-11-05")
    _market(conn, "mB", settlement_date="2026-11-06")
    _market(conn, "mC", settlement_date="2026-11-04")
    _market(conn, "mOpen", settlement_date="2026-12-31")
    # o1 wins and o2 loses on the same market and run; the best-per-market view
    # keeps the higher edge (o1).
    _order(conn, market_id="mA", outcome="60-64°F", quoted_ask=0.50, edge=0.20)
    _order(conn, market_id="mA", outcome="65°F or higher", quoted_ask=0.48, edge=0.15)
    _order(
        conn,
        market_id="mB",
        run_id="r2",
        outcome="60-64°F",
        side="NO",
        status="partial",
        shares=4.0,
        fill_price=0.75,
        fee_usdc=0.0375,
        quoted_ask=0.74,
        edge=None,
    )
    # o4: a no_fill that still carries a walked price and shares.
    _order(conn, market_id="mC", status="no_fill", shares=2.0, fill_price=0.5, fee_usdc=0.0)
    # o5: filled, market still open.
    _order(conn, market_id="mOpen")
    # o6: filled and settled, but created before the era start.
    _order(conn, market_id="mC", created_at=datetime(2026, 11, 1, 23, 0, tzinfo=UTC))
    record_outcome(conn, "mA", 62.0, "t")
    record_outcome(conn, "mB", 70.0, "t")
    record_outcome(conn, "mC", 62.0, "t")
    grade_paper_orders(conn)


def test_known_answer_report(conn):
    _known_answer_store(conn)
    report = compute_paper_report(load_paper_rows(conn))

    assert report.status_counts == {"filled": 3, "partial": 1, "no_fill": 1}
    assert report.fill_rate == pytest.approx(0.8)
    assert report.sample == 3
    assert report.open_fills == 1
    assert report.ungradable == 0
    assert report.dates == 2
    assert report.pnl == pytest.approx(0.7125)
    assert report.outlay == pytest.approx(13.2875)
    assert report.net_roi == pytest.approx(0.0536218, abs=1e-6)
    assert report.gap == pytest.approx(0.01)
    assert report.gap_n == 3
    assert report.pre_era_orders == 1
    assert report.best.n == 2
    assert report.best.pnl == pytest.approx(5.8375)
    assert report.best.outlay == pytest.approx(8.1625)
    assert report.best.roi == pytest.approx(0.715161, abs=1e-6)


def test_known_answer_verdict_is_pending_with_roi_pass_and_gap_fail(conn):
    _known_answer_store(conn)
    report = compute_paper_report(load_paper_rows(conn))
    assert report.verdict == "PENDING"
    passed = {c.name: c.passed for c in report.checks}
    assert passed == {"sample": False, "dates": False, "net ROI": True, "execution gap": False}


def test_report_counts_per_era_and_marks_pre_era(conn):
    _known_answer_store(conn)
    report = compute_paper_report(load_paper_rows(conn))
    assert [(e.label, e.orders, e.sample) for e in report.eras] == [("fee-aware gate (#491)", 5, 3)]


def test_report_ignores_no_fill_rows_for_roi_fill_rate_and_gap(conn):
    # The no_fill row has a price below the quoted ask: counting it would move the gap.
    _market(conn, "m1")
    _order(conn, quoted_ask=0.5)
    _order(conn, status="no_fill", shares=2.0, fill_price=0.1, quoted_ask=0.9)
    record_outcome(conn, "m1", 62.0, "t")
    grade_paper_orders(conn)
    report = compute_paper_report(load_paper_rows(conn))
    assert report.sample == 1
    assert report.gap == pytest.approx(0.0)
    assert report.fill_rate == pytest.approx(0.5)
    assert report.pnl == pytest.approx(4.875)


def test_empty_store_is_pending_with_no_data(conn):
    report = compute_paper_report(load_paper_rows(conn))
    assert report.verdict == "PENDING"
    assert report.sample == 0
    assert report.net_roi is None
    assert report.gap is None
    assert report.fill_rate is None
    assert not any(c.passed for c in report.checks)


def test_ungradable_settled_fill_is_counted_but_not_in_the_sample(conn):
    _market(conn, "m1", spec=None)
    _order(conn, outcome="inches")
    record_outcome(conn, "m1", 62.0, "t")
    grade_paper_orders(conn)
    report = compute_paper_report(load_paper_rows(conn))
    assert (report.sample, report.ungradable, report.open_fills) == (0, 1, 0)


# --- report: verdict boundaries -------------------------------------------


def _row(i, *, won=True, ask=0.5, price=0.5, created=T_IN_ERA, day=0, **kw):
    shares, fee = 10.0, 0.0
    outlay = shares * price + fee
    base = {
        "id": i,
        "run_id": f"r{i}",
        "market_id": f"m{i}",
        "outcome": "b",
        "side": "YES",
        "status": "filled",
        "shares": shares,
        "fill_price": price,
        "fee_usdc": fee,
        "p_win": 0.6,
        "edge": 0.1,
        "quoted_ask": ask,
        "won": int(won),
        "pnl_usdc": (shares - outlay) if won else -outlay,
        "created_at": created.isoformat(),
        "settlement_date": date(2027, 1, 1)
        .replace(day=1 + day % 28, month=1 + day // 28)
        .isoformat(),
        "settled": 1,
    }
    base.update(kw)
    return base


def _baseline(n=400, wins=202, dates=60, ask=0.5):
    return [_row(i, won=i < wins, ask=ask, day=i % dates) for i in range(n)]


def test_baseline_at_every_threshold_is_go():
    report = compute_paper_report(_baseline())
    assert report.sample == GO_LIVE_MIN_ORDERS
    assert report.dates == GO_LIVE_MIN_DATES
    assert report.net_roi == pytest.approx(GO_LIVE_MIN_NET_ROI)
    assert report.verdict == "GO"
    assert all(c.passed for c in report.checks)


def test_one_order_short_is_pending():
    assert compute_paper_report(_baseline(n=399)).verdict == "PENDING"


def test_one_date_short_is_pending():
    report = compute_paper_report(_baseline(dates=59))
    assert report.dates == 59
    assert report.verdict == "PENDING"


def test_roi_just_below_the_bar_is_no_go():
    report = compute_paper_report(_baseline(wins=201))
    assert report.verdict == "NO-GO"
    assert {c.name: c.passed for c in report.checks}["net ROI"] is False


def test_gap_of_one_cent_is_no_go():
    assert compute_paper_report(_baseline(ask=0.49)).verdict == "NO-GO"


def test_gap_of_exactly_half_a_cent_is_go_despite_float_error():
    # 0.5 - 0.495 is 0.0050000000000000044 in binary floating point.
    assert GO_LIVE_MAX_GAP_USDC == 0.005
    assert compute_paper_report(_baseline(ask=0.495)).verdict == "GO"


# --- report: eras ---------------------------------------------------------


def test_verdict_uses_only_the_latest_era_and_both_are_listed():
    old = datetime(2026, 11, 2, tzinfo=UTC)
    new = datetime(2026, 12, 1, tzinfo=UTC)
    eras = (PaperEra(old, "old"), PaperEra(new, "new"))
    rows = [_row(i, created=old, day=i % 60) for i in range(400)]
    rows += [_row(1000 + i, created=new, day=i % 60) for i in range(3)]
    report = compute_paper_report(rows, eras=eras)
    assert report.era is not None and report.era.label == "new"
    assert report.sample == 3
    assert report.verdict == "PENDING"
    assert [(e.label, e.orders, e.sample) for e in report.eras] == [
        ("old", 400, 400),
        ("new", 3, 3),
    ]


def test_order_before_the_first_era_never_counts():
    rows = [_row(1, created=datetime(2026, 10, 1, tzinfo=UTC))]
    report = compute_paper_report(rows)
    assert report.sample == 0
    assert report.pre_era_orders == 1


def test_first_era_is_the_fee_aware_gate():
    # #491 (PR #497) merged at 2026-11-03T07:36:10Z.
    assert PAPER_ERAS[0] == PaperEra(ERA_START, "fee-aware gate (#491)")


# --- render ---------------------------------------------------------------


def test_render_shows_verdict_checks_and_counts(conn):
    _known_answer_store(conn)
    text = render_paper_report(compute_paper_report(load_paper_rows(conn)))
    assert "PENDING" in text
    assert "fee-aware gate (#491)" in text
    assert "filled 3" in text and "no_fill 1" in text
    assert "net ROI" in text and "execution gap" in text
    assert "—" not in text


def test_render_of_an_empty_store_is_pending(conn):
    text = render_paper_report(compute_paper_report(load_paper_rows(conn)))
    assert "PENDING" in text


# --- report: bootstrap ROI interval (#513) ---------------------------------


def _spread(n=400, dates=60):
    # Every order on one date shares that date's weather, so a date wins or loses
    # as a block. That is the correlation the bootstrap has to resample.
    return [_row(i, won=(i % dates) < dates // 2, day=i % dates) for i in range(n)]


def test_roi_interval_tolerates_a_null_settlement_date():
    # settlement_date is nullable in the schema; the review flagged that
    # sorted(by_date) raised TypeError when None mixed with date strings.
    rows = _spread(n=10, dates=2)
    rows[0] = {**rows[0], "settlement_date": None}
    report = compute_paper_report(rows)
    # The None row clusters under "" instead of crashing the sort. The
    # report-level count (a set) always tolerated None; it sees 3 dates here.
    assert report.roi_interval is not None
    assert report.dates == 3
    assert report.roi_interval.dates == 3


def test_roi_interval_is_deterministic():
    rows = _spread(dates=15)
    a = compute_paper_report(rows).roi_interval
    b = compute_paper_report(rows).roi_interval
    assert a is not None and a == b
    assert a.resamples == ROI_INTERVAL_RESAMPLES
    assert a.level == ROI_INTERVAL_LEVEL
    assert a.dates == 15
    assert a.lo <= a.hi


def test_roi_interval_widens_as_dates_fall_with_orders_held_constant():
    wide = compute_paper_report(_spread(n=400, dates=5)).roi_interval
    mid = compute_paper_report(_spread(n=400, dates=15)).roi_interval
    narrow = compute_paper_report(_spread(n=400, dates=60)).roi_interval
    assert wide is not None and mid is not None and narrow is not None
    width = lambda iv: iv.hi - iv.lo  # noqa: E731
    assert width(wide) > width(mid) > width(narrow)


def test_too_few_dates_yields_no_interval_and_says_so():
    one = compute_paper_report(_spread(n=10, dates=1))
    assert one.dates == 1
    assert one.roi_interval is None
    assert "too few settlement dates" in render_paper_report(one)

    empty = compute_paper_report([])
    assert empty.dates == 0
    assert empty.roi_interval is None
    assert "too few settlement dates" in render_paper_report(empty)


def test_at_the_floor_the_interval_is_computed():
    assert ROI_INTERVAL_MIN_DATES == 2
    report = compute_paper_report(_spread(n=40, dates=2))
    assert report.dates == 2
    assert report.roi_interval is not None


def test_render_shows_level_resamples_and_date_count():
    report = compute_paper_report(_spread(n=400, dates=15))
    text = render_paper_report(report)
    assert "90%" in text
    assert "10000 resamples" in text
    assert "15 settlement dates" in text


def test_roi_interval_never_touches_the_verdict_path():
    report = compute_paper_report(_baseline())
    assert report.verdict == "GO"
    assert [c.name for c in report.checks] == [
        "sample",
        "dates",
        "net ROI",
        "execution gap",
    ]
    assert report.roi_interval is not None
