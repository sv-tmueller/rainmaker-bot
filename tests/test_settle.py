import json
import re
from datetime import date

import httpx
import pytest

from rainmaker.backfill import NCEI_URL
from rainmaker.settle import run_settlement
from rainmaker.store.db import connect, init_schema
from rainmaker.store.query import unsettled_markets
from rainmaker.store.record import record_outcome


def _market(conn, market_id, city, variable, settlement_date, outcome_spec=None):
    spec = json.dumps(outcome_spec) if outcome_spec is not None else None
    conn.execute(
        "INSERT INTO markets (id, city, variable, settlement_date, outcome_spec) "
        "VALUES (?, ?, ?, ?, ?)",
        (market_id, city, variable, settlement_date, spec),
    )
    conn.commit()


def _run(conn, run_id):
    conn.execute(
        "INSERT OR IGNORE INTO runs (id, started_at, status) VALUES (?, ?, ?)",
        (run_id, "t", "ok"),
    )
    conn.commit()


def _prediction(conn, run_id, market_id, bucket, side, p_win, recommended=1):
    _run(conn, run_id)
    cols = "run_id, market_id, bucket, side, p_win, edge, recommended, created_at"
    conn.execute(
        f"INSERT INTO predictions ({cols}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, market_id, bucket, side, p_win, 0.1, recommended, "t"),
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


def test_run_settlement_records_tmin_outcome(httpx_mock):
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "m1", "NYC", "TMIN", "2026-05-30")
    httpx_mock.add_response(
        url=re.compile(re.escape(NCEI_URL)),
        json=[{"DATE": "2026-05-30", "STATION": "USW00014732", "TMIN": "45"}],
    )
    with httpx.Client() as client:
        settled, waiting = run_settlement(conn, client, date(2026, 6, 3), "2026-06-03T00:00:00Z")
    row = conn.execute("SELECT actual_value FROM outcomes WHERE market_id = ?", ("m1",)).fetchone()
    conn.close()
    assert (settled, waiting) == (1, 0)
    assert row["actual_value"] == 45.0


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


def test_run_settlement_uses_row_ghcnd_for_kalshi(httpx_mock):
    # a Kalshi NYC temp market settles on Central Park (USW00094728), not the
    # city default LaGuardia (USW00014732); settlement must use the row's GHCND.
    conn = connect(":memory:")
    init_schema(conn)
    conn.execute(
        "INSERT INTO markets (id, city, variable, settlement_date, settlement_ghcnd) "
        "VALUES (?, ?, ?, ?, ?)",
        ("KXHIGHNY-26MAY30", "NYC", "TMAX", "2026-05-30", "USW00094728"),
    )
    conn.commit()
    captured: dict[str, str] = {}

    def handler(request):
        captured["url"] = str(request.url)
        return httpx.Response(200, json=[{"DATE": "2026-05-30", "TMAX": "70"}])

    httpx_mock.add_callback(handler)
    with httpx.Client() as client:
        settled, waiting = run_settlement(conn, client, date(2026, 6, 3), "t")
    conn.close()
    assert (settled, waiting) == (1, 0)
    assert "USW00094728" in captured["url"]  # Central Park, not LaGuardia
    assert "USW00014732" not in captured["url"]


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


def test_run_settlement_continues_past_http_error(httpx_mock):
    # One station's NCEI HTTP error must not abort the loop: that market waits and
    # every other market is still attempted. (Scheduled-run robustness.)
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "m1", "NYC", "TMAX", "2026-05-30")
    _market(conn, "m2", "NYC", "TMAX", "2026-05-30")
    httpx_mock.add_response(url=re.compile(re.escape(NCEI_URL)), status_code=500)
    httpx_mock.add_response(
        url=re.compile(re.escape(NCEI_URL)),
        json=[{"DATE": "2026-05-30", "TMAX": "71"}],
    )
    with httpx.Client() as client:
        settled, waiting = run_settlement(conn, client, date(2026, 6, 3), "2026-06-03T00:00:00Z")
    n = conn.execute("SELECT count(*) AS n FROM outcomes").fetchone()["n"]
    conn.close()
    assert (settled, waiting) == (1, 1)
    assert n == 1


def test_run_settlement_precip_waits_on_http_error(httpx_mock):
    # The precip fetch path (fetch_monthly_precip) also raises on HTTP error; the
    # market must wait, not crash the run.
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "p1", "NYC", "PRCP", "2026-06-30")
    httpx_mock.add_response(url=re.compile(re.escape(NCEI_URL)), status_code=500)
    with httpx.Client() as client:
        settled, waiting = run_settlement(conn, client, date(2026, 7, 3), "2026-07-03T00:00:00Z")
    n = conn.execute("SELECT count(*) AS n FROM outcomes").fetchone()["n"]
    conn.close()
    assert (settled, waiting) == (0, 1)
    assert n == 0


def test_run_settlement_skips_unknown_variable(capsys):
    # A market with an unrecognised variable (neither TMAX, TMIN, nor PRCP) is
    # skipped with a stderr warning - not sent to NCEI (which would stall it).
    # No httpx mock: any HTTP call would raise here, proving no call was made.
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "w1", "NYC", "WIND", "2026-05-30")
    with httpx.Client() as client:
        settled, waiting = run_settlement(conn, client, date(2026, 6, 3), "2026-06-03T00:00:00Z")
    n = conn.execute("SELECT count(*) AS n FROM outcomes").fetchone()["n"]
    conn.close()
    assert (settled, waiting) == (0, 0)
    assert n == 0
    captured = capsys.readouterr()
    assert "WIND" in captured.err


def test_run_settlement_unknown_variable_does_not_block_rest(httpx_mock):
    # The unknown-variable market is skipped; markets later in the loop still settle.
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "w1", "NYC", "WIND", "2026-05-30")
    _market(conn, "m1", "NYC", "TMAX", "2026-05-30")
    httpx_mock.add_response(
        url=re.compile(re.escape(NCEI_URL)),
        json=[{"DATE": "2026-05-30", "TMAX": "72"}],
    )
    with httpx.Client() as client:
        settled, waiting = run_settlement(conn, client, date(2026, 6, 3), "2026-06-03T00:00:00Z")
    n = conn.execute("SELECT count(*) AS n FROM outcomes").fetchone()["n"]
    conn.close()
    assert (settled, waiting) == (1, 0)
    assert n == 1


# ----- won grading tests -----

_TMAX_SPEC = [
    {"label": "59°F or below", "kind": "below", "lo": None, "hi": None, "threshold": 59},
    {"label": "60-64°F", "kind": "range", "lo": 60, "hi": 64, "threshold": None},
    {"label": "65°F or higher", "kind": "above", "lo": None, "hi": None, "threshold": 65},
]

_PRCP_SPEC = [
    {"label": 'under 1.00"', "kind": "below", "lo": None, "hi": None, "threshold": 1.0},
    {"label": '1.00"-2.00"', "kind": "range", "lo": 1.0, "hi": 2.0, "threshold": None},
    {"label": '2.00" or over', "kind": "above", "lo": None, "hi": None, "threshold": 2.0},
]


def test_run_settlement_grades_predictions_on_settle(httpx_mock):
    # When run_settlement settles a market, recommended predictions get won filled in.
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "m1", "NYC", "TMAX", "2026-05-30", _TMAX_SPEC)
    # actual is 62: lands in "60-64°F" (range, lo=60, hi=64); round(62.0)=62, 60<=62<=64 -> YES wins
    _prediction(conn, "run-1", "m1", "60-64°F", "YES", 0.5, recommended=1)
    _prediction(conn, "run-1", "m1", "65°F or higher", "YES", 0.3, recommended=1)
    httpx_mock.add_response(
        url=re.compile(re.escape(NCEI_URL)),
        json=[{"DATE": "2026-05-30", "TMAX": "62"}],
    )
    with httpx.Client() as client:
        run_settlement(conn, client, date(2026, 6, 3), "2026-06-03T00:00:00Z")
    q = conn.execute("SELECT bucket, won FROM predictions WHERE market_id = ?", ("m1",))
    rows = {r["bucket"]: r["won"] for r in q.fetchall()}
    conn.close()
    assert rows["60-64°F"] == 1  # in-bucket YES bet wins
    assert rows["65°F or higher"] == 0  # out-of-bucket YES bet loses


def test_run_settlement_grades_no_side_predictions(httpx_mock):
    # A NO bet wins when the bucket does NOT settle.
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "m1", "NYC", "TMAX", "2026-05-30", _TMAX_SPEC)
    # actual 62 -> "60-64°F" settles; NO on "60-64°F" loses; NO on "65°F or higher" wins
    _prediction(conn, "run-1", "m1", "60-64°F", "NO", 0.4, recommended=1)
    _prediction(conn, "run-1", "m1", "65°F or higher", "NO", 0.6, recommended=1)
    httpx_mock.add_response(
        url=re.compile(re.escape(NCEI_URL)),
        json=[{"DATE": "2026-05-30", "TMAX": "62"}],
    )
    with httpx.Client() as client:
        run_settlement(conn, client, date(2026, 6, 3), "2026-06-03T00:00:00Z")
    q = conn.execute("SELECT bucket, won FROM predictions WHERE market_id = ?", ("m1",))
    rows = {r["bucket"]: r["won"] for r in q.fetchall()}
    conn.close()
    assert rows["60-64°F"] == 0  # bucket settled, NO loses
    assert rows["65°F or higher"] == 1  # bucket did not settle, NO wins


def test_run_settlement_grades_precip_predictions(httpx_mock):
    # Precip uses precip_settles (half-open intervals); a boundary value (1.00 inches)
    # resolves to the higher bracket -- the 1.00-2.00 range wins, not the under-1.00 below.
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "p1", "NYC", "PRCP", "2026-06-30", _PRCP_SPEC)
    _prediction(conn, "run-1", "p1", 'under 1.00"', "YES", 0.3, recommended=1)
    _prediction(conn, "run-1", "p1", '1.00"-2.00"', "YES", 0.4, recommended=1)
    httpx_mock.add_response(
        url=re.compile(re.escape(NCEI_URL)),
        json=[{"DATE": "2026-06", "STATION": "USW00094728", "PRCP": "1.00"}],
    )
    with httpx.Client() as client:
        run_settlement(conn, client, date(2026, 7, 3), "2026-07-03T00:00:00Z")
    q = conn.execute("SELECT bucket, won FROM predictions WHERE market_id = ?", ("p1",))
    rows = {r["bucket"]: r["won"] for r in q.fetchall()}
    conn.close()
    # 1.00 is a boundary: precip_settles uses half-open [lo, hi), so 1.00 resolves UP.
    # "under 1.00"" is [0, 1.00) -> 1.00 does NOT land here -> YES loses
    # "1.00"-2.00"" is [1.00, 2.00) -> 1.00 DOES land here -> YES wins
    assert rows['under 1.00"'] == 0
    assert rows['1.00"-2.00"'] == 1


def test_run_settlement_backfills_won_for_already_settled_markets(httpx_mock):
    # Markets already in outcomes (settled before this change) must also get won
    # populated when predictions.won IS NULL. No NCEI call needed for them.
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "m1", "NYC", "TMAX", "2026-05-28", _TMAX_SPEC)
    _prediction(conn, "run-1", "m1", "60-64°F", "YES", 0.5, recommended=1)
    # Pre-seed the outcome (simulates "already settled before the won column existed")
    record_outcome(conn, "m1", 62.0, "2026-05-30T00:00:00Z")
    # Add an unsettled market so run_settlement has something to do (to show the
    # backfill pass runs regardless).
    _market(conn, "m2", "NYC", "TMAX", "2026-05-30", _TMAX_SPEC)
    httpx_mock.add_response(
        url=re.compile(re.escape(NCEI_URL)),
        json=[{"DATE": "2026-05-30", "TMAX": "70"}],
    )
    with httpx.Client() as client:
        run_settlement(conn, client, date(2026, 6, 3), "2026-06-03T00:00:00Z")
    row = conn.execute(
        "SELECT won FROM predictions WHERE market_id = ? AND bucket = ?", ("m1", "60-64°F")
    ).fetchone()
    conn.close()
    # The backfill pass must have graded m1's prediction even though m1 was
    # already settled before this run_settlement call.
    assert row["won"] == 1


def test_run_settlement_does_not_grade_non_recommended_predictions(httpx_mock):
    # won is only meaningful for recommended predictions; non-recommended rows
    # stay NULL to keep the data model clean.
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "m1", "NYC", "TMAX", "2026-05-30", _TMAX_SPEC)
    _prediction(conn, "run-1", "m1", "60-64°F", "YES", 0.5, recommended=0)
    httpx_mock.add_response(
        url=re.compile(re.escape(NCEI_URL)),
        json=[{"DATE": "2026-05-30", "TMAX": "62"}],
    )
    with httpx.Client() as client:
        run_settlement(conn, client, date(2026, 6, 3), "2026-06-03T00:00:00Z")
    row = conn.execute("SELECT won FROM predictions WHERE market_id = ?", ("m1",)).fetchone()
    conn.close()
    assert row["won"] is None  # not graded: not recommended
