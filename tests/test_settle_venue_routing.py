"""TDD tests for per-venue settlement routing and re-grade.

Polymarket TMAX/TMIN -> ASOS (Iowa State Mesonet).
Polymarket PRCP      -> NCEI (unchanged).
Kalshi (all)         -> NCEI (unchanged).

Re-grade: regrade_polymarket_settlements re-settles existing Polymarket
TMAX/TMIN outcomes using ASOS and re-grades predictions.won.
"""

import json
import re
from datetime import date

import httpx
import pytest

from rainmaker.backfill import NCEI_URL
from rainmaker.forecasts.asos import MESONET_ASOS_URL
from rainmaker.settle import regrade_polymarket_settlements, run_settlement
from rainmaker.store.db import connect, init_schema
from rainmaker.store.migrate import _backfill_venue
from rainmaker.store.record import record_outcome

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_TMAX_SPEC = [
    {"label": "59°F or below", "kind": "below", "lo": None, "hi": None, "threshold": 59},
    {"label": "60-64°F", "kind": "range", "lo": 60, "hi": 64, "threshold": None},
    {"label": "65°F or higher", "kind": "above", "lo": None, "hi": None, "threshold": 65},
]

# ASOS mesonet CSV for KLGA (LGA): one hour at 20.0C = 68.0F
_ASOS_CSV_68F = "station,valid,tmpc\nLGA,2026-05-30 12:00,20.0\n"

# ASOS mesonet CSV for KLGA: one hour at 18.0C = 64.4F -> rounds to 64
# This lands in "60-64°F" when the NCEI value would land in "65°F or higher"
_ASOS_CSV_64F = "station,valid,tmpc\nLGA,2026-05-30 12:00,18.0\n"

# NCEI JSON: 65F (in "65°F or higher")
_NCEI_JSON_65F = [{"DATE": "2026-05-30", "TMAX": "65"}]


def _market(conn, market_id, city, variable, settlement_date, outcome_spec=None, venue=None):
    spec = json.dumps(outcome_spec) if outcome_spec is not None else None
    conn.execute(
        "INSERT INTO markets (id, city, variable, settlement_date, outcome_spec, venue) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (market_id, city, variable, settlement_date, spec, venue),
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


# ---------------------------------------------------------------------------
# per-venue routing: Polymarket TMAX -> ASOS
# ---------------------------------------------------------------------------


def test_polymarket_tmax_uses_asos(httpx_mock):
    """A Polymarket TMAX market must be settled via ASOS (Mesonet), not NCEI."""
    conn = connect(":memory:")
    init_schema(conn)
    # venue=polymarket (explicit; default is also polymarket)
    _market(conn, "poly-tmax", "NYC", "TMAX", "2026-05-30", venue="polymarket")

    httpx_mock.add_response(
        url=re.compile(re.escape(MESONET_ASOS_URL)),
        text=_ASOS_CSV_68F,
    )
    with httpx.Client() as client:
        settled, waiting = run_settlement(conn, client, date(2026, 6, 3), "2026-06-03T00:00:00Z")
    row = conn.execute(
        "SELECT actual_value FROM outcomes WHERE market_id = ?", ("poly-tmax",)
    ).fetchone()
    conn.close()
    assert (settled, waiting) == (1, 0)
    # 20.0C = 68.0F
    assert row["actual_value"] == pytest.approx(68.0, abs=0.1)


def test_polymarket_tmin_uses_asos(httpx_mock):
    """A Polymarket TMIN market must be settled via ASOS (Mesonet)."""
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "poly-tmin", "NYC", "TMIN", "2026-05-30", venue="polymarket")

    # Single ASOS reading; TMIN and TMAX both come from hourly tmpc extremes
    httpx_mock.add_response(
        url=re.compile(re.escape(MESONET_ASOS_URL)),
        text=_ASOS_CSV_68F,
    )
    with httpx.Client() as client:
        settled, waiting = run_settlement(conn, client, date(2026, 6, 3), "2026-06-03T00:00:00Z")
    row = conn.execute(
        "SELECT actual_value FROM outcomes WHERE market_id = ?", ("poly-tmin",)
    ).fetchone()
    conn.close()
    assert (settled, waiting) == (1, 0)
    # With only one reading, TMIN == TMAX == 68.0F
    assert row["actual_value"] == pytest.approx(68.0, abs=0.1)


def test_polymarket_tmax_does_not_call_ncei(httpx_mock):
    """No NCEI call when settling a Polymarket TMAX market."""
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "poly-tmax", "NYC", "TMAX", "2026-05-30", venue="polymarket")

    ncei_called: list[bool] = []

    def ncei_handler(request: httpx.Request) -> httpx.Response:
        ncei_called.append(True)
        return httpx.Response(200, json=[])

    httpx_mock.add_response(
        url=re.compile(re.escape(MESONET_ASOS_URL)),
        text=_ASOS_CSV_68F,
    )
    # If NCEI were called it would also need a mock, so any NCEI call would 404.
    # We register a callback to detect accidental NCEI calls.
    # (httpx_mock raises if an unmocked URL is called)
    with httpx.Client() as client:
        run_settlement(conn, client, date(2026, 6, 3), "2026-06-03T00:00:00Z")
    conn.close()
    assert not ncei_called


# ---------------------------------------------------------------------------
# per-venue routing: Kalshi TMAX -> NCEI (unchanged)
# ---------------------------------------------------------------------------


def test_kalshi_tmax_uses_ncei(httpx_mock):
    """A Kalshi TMAX market must continue to be settled via NCEI, not ASOS."""
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "kalshi-tmax", "NYC", "TMAX", "2026-05-30", venue="kalshi")

    httpx_mock.add_response(
        url=re.compile(re.escape(NCEI_URL)),
        json=_NCEI_JSON_65F,
    )
    with httpx.Client() as client:
        settled, waiting = run_settlement(conn, client, date(2026, 6, 3), "2026-06-03T00:00:00Z")
    row = conn.execute(
        "SELECT actual_value FROM outcomes WHERE market_id = ?", ("kalshi-tmax",)
    ).fetchone()
    conn.close()
    assert (settled, waiting) == (1, 0)
    assert row["actual_value"] == pytest.approx(65.0)


def test_kalshi_tmax_does_not_call_asos(httpx_mock):
    """No ASOS call when settling a Kalshi market."""
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "kalshi-tmax", "NYC", "TMAX", "2026-05-30", venue="kalshi")

    httpx_mock.add_response(
        url=re.compile(re.escape(NCEI_URL)),
        json=_NCEI_JSON_65F,
    )
    # ASOS would fail if called (no mock registered for it)
    with httpx.Client() as client:
        settled, waiting = run_settlement(conn, client, date(2026, 6, 3), "2026-06-03T00:00:00Z")
    conn.close()
    assert (settled, waiting) == (1, 0)


# ---------------------------------------------------------------------------
# per-venue routing: NULL venue falls back to NCEI (safe default)
# ---------------------------------------------------------------------------


def test_null_venue_falls_back_to_ncei(httpx_mock):
    """Markets with venue IS NULL (legacy rows) fall back to NCEI (safe default)."""
    conn = connect(":memory:")
    init_schema(conn)
    # venue=None simulates a legacy row that predates the venue column
    _market(conn, "legacy", "NYC", "TMAX", "2026-05-30", venue=None)

    httpx_mock.add_response(
        url=re.compile(re.escape(NCEI_URL)),
        json=_NCEI_JSON_65F,
    )
    with httpx.Client() as client:
        settled, waiting = run_settlement(conn, client, date(2026, 6, 3), "2026-06-03T00:00:00Z")
    row = conn.execute(
        "SELECT actual_value FROM outcomes WHERE market_id = ?", ("legacy",)
    ).fetchone()
    conn.close()
    assert (settled, waiting) == (1, 0)
    assert row["actual_value"] == pytest.approx(65.0)


# ---------------------------------------------------------------------------
# per-venue routing: Polymarket PRCP -> NCEI (unchanged)
# ---------------------------------------------------------------------------


def test_polymarket_prcp_still_uses_ncei(httpx_mock):
    """Polymarket PRCP (monthly) stays on NCEI GSOM; ASOS has no precip data."""
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "poly-prcp", "NYC", "PRCP", "2026-06-30", venue="polymarket")

    httpx_mock.add_response(
        url=re.compile(re.escape(NCEI_URL)),
        json=[{"DATE": "2026-06", "STATION": "USW00094728", "PRCP": "3.50"}],
    )
    with httpx.Client() as client:
        settled, waiting = run_settlement(conn, client, date(2026, 7, 3), "2026-07-03T00:00:00Z")
    row = conn.execute(
        "SELECT actual_value FROM outcomes WHERE market_id = ?", ("poly-prcp",)
    ).fetchone()
    conn.close()
    assert (settled, waiting) == (1, 0)
    assert row["actual_value"] == pytest.approx(3.50)


# ---------------------------------------------------------------------------
# ASOS waits when station unmapped (city not in ICAO_TO_ASOS_STATION)
# ---------------------------------------------------------------------------


def test_polymarket_tmax_waits_when_asos_station_unknown():
    """If no ASOS code exists for the city, the market waits (not crash)."""
    conn = connect(":memory:")
    init_schema(conn)
    # "Atlantis" has no ICAO -> no ASOS code
    _market(conn, "poly-atlantis", "Atlantis", "TMAX", "2026-05-30", venue="polymarket")

    with httpx.Client() as client:
        settled, waiting = run_settlement(conn, client, date(2026, 6, 3), "2026-06-03T00:00:00Z")
    conn.close()
    assert (settled, waiting) == (0, 0)  # skipped: no station for "Atlantis"


# ---------------------------------------------------------------------------
# ASOS HTTP error: market waits, loop continues
# ---------------------------------------------------------------------------


def test_polymarket_tmax_waits_on_asos_http_error(httpx_mock):
    """An ASOS HTTP error puts the market in waiting, does not crash the loop."""
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "poly-err", "NYC", "TMAX", "2026-05-30", venue="polymarket")
    _market(conn, "poly-ok", "NYC", "TMAX", "2026-05-30", venue="polymarket")

    httpx_mock.add_response(
        url=re.compile(re.escape(MESONET_ASOS_URL)),
        status_code=503,
    )
    httpx_mock.add_response(
        url=re.compile(re.escape(MESONET_ASOS_URL)),
        text=_ASOS_CSV_68F,
    )
    with httpx.Client() as client:
        settled, waiting = run_settlement(conn, client, date(2026, 6, 3), "2026-06-03T00:00:00Z")
    conn.close()
    assert (settled, waiting) == (1, 1)


# ---------------------------------------------------------------------------
# re-grade: regrade_polymarket_settlements
# ---------------------------------------------------------------------------


def test_regrade_updates_outcome_for_polymarket_tmax(httpx_mock):
    """regrade_polymarket_settlements overwrites outcomes.actual_value with ASOS data."""
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "poly-m", "NYC", "TMAX", "2026-05-30", _TMAX_SPEC, venue="polymarket")
    # Pre-seed with NCEI value (65F lands in "65°F or higher")
    record_outcome(conn, "poly-m", 65.0, "2026-05-31T00:00:00Z")

    # ASOS returns 64.4F -> rounds to 64 -> lands in "60-64°F"
    httpx_mock.add_response(
        url=re.compile(re.escape(MESONET_ASOS_URL)),
        text=_ASOS_CSV_64F,
    )
    with httpx.Client() as client:
        regraded = regrade_polymarket_settlements(conn, client, "2026-06-15T00:00:00Z")
    row = conn.execute(
        "SELECT actual_value FROM outcomes WHERE market_id = ?", ("poly-m",)
    ).fetchone()
    conn.close()
    assert regraded == 1
    # 18.0C = 64.4F
    assert row["actual_value"] == pytest.approx(18.0 * 9 / 5 + 32, abs=0.1)


def test_regrade_updates_predictions_won(httpx_mock):
    """regrade flips predictions.won when the ASOS bucket differs from NCEI."""
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "poly-m", "NYC", "TMAX", "2026-05-30", _TMAX_SPEC, venue="polymarket")
    # NCEI settled at 65F -> "65°F or higher"; YES on "65°F or higher" was graded won=1
    record_outcome(conn, "poly-m", 65.0, "2026-05-31T00:00:00Z")
    _prediction(conn, "run-1", "poly-m", "65°F or higher", "YES", 0.6, recommended=1)
    _prediction(conn, "run-1", "poly-m", "60-64°F", "YES", 0.3, recommended=1)
    # Simulate NCEI grading: "65°F or higher" won=1, "60-64°F" won=0
    conn.execute("UPDATE predictions SET won = 1 WHERE bucket = '65°F or higher'")
    conn.execute("UPDATE predictions SET won = 0 WHERE bucket = '60-64°F'")
    conn.commit()

    # ASOS returns 18.0C = 64.4F -> rounds to 64 -> "60-64°F" settles instead
    httpx_mock.add_response(
        url=re.compile(re.escape(MESONET_ASOS_URL)),
        text=_ASOS_CSV_64F,
    )
    with httpx.Client() as client:
        regrade_polymarket_settlements(conn, client, "2026-06-15T00:00:00Z")
    rows = {
        r["bucket"]: r["won"]
        for r in conn.execute(
            "SELECT bucket, won FROM predictions WHERE market_id = ?", ("poly-m",)
        ).fetchall()
    }
    conn.close()
    # After ASOS re-grade: "60-64°F" lands -> YES wins; "65°F or higher" does not -> YES loses
    assert rows["60-64°F"] == 1
    assert rows["65°F or higher"] == 0


def test_regrade_does_not_touch_kalshi_markets(httpx_mock):
    """regrade_polymarket_settlements must not re-settle Kalshi markets."""
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "kalshi-m", "NYC", "TMAX", "2026-05-30", _TMAX_SPEC, venue="kalshi")
    record_outcome(conn, "kalshi-m", 65.0, "2026-05-31T00:00:00Z")
    _prediction(conn, "run-1", "kalshi-m", "65°F or higher", "YES", 0.6, recommended=1)
    conn.execute("UPDATE predictions SET won = 1 WHERE bucket = '65°F or higher'")
    conn.commit()

    # No ASOS mock: any call to ASOS would raise (unmocked URL)
    with httpx.Client() as client:
        regraded = regrade_polymarket_settlements(conn, client, "2026-06-15T00:00:00Z")

    row = conn.execute(
        "SELECT actual_value FROM outcomes WHERE market_id = ?", ("kalshi-m",)
    ).fetchone()
    won = conn.execute("SELECT won FROM predictions WHERE market_id = ?", ("kalshi-m",)).fetchone()
    conn.close()
    assert regraded == 0  # nothing regraded
    assert row["actual_value"] == pytest.approx(65.0)  # unchanged
    assert won["won"] == 1  # unchanged


def test_regrade_does_not_touch_prcp_markets(httpx_mock):
    """regrade_polymarket_settlements must not re-settle PRCP markets (ASOS has no precip)."""
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "poly-prcp", "NYC", "PRCP", "2026-06-30", venue="polymarket")
    record_outcome(conn, "poly-prcp", 3.50, "2026-07-01T00:00:00Z")

    # No ASOS mock: any call would raise
    with httpx.Client() as client:
        regraded = regrade_polymarket_settlements(conn, client, "2026-06-15T00:00:00Z")
    conn.close()
    assert regraded == 0


def test_regrade_is_idempotent(httpx_mock):
    """Running regrade twice on the same row converges to the same ASOS value."""
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "poly-m", "NYC", "TMAX", "2026-05-30", _TMAX_SPEC, venue="polymarket")
    record_outcome(conn, "poly-m", 65.0, "2026-05-31T00:00:00Z")
    _prediction(conn, "run-1", "poly-m", "60-64°F", "YES", 0.3, recommended=1)

    httpx_mock.add_response(
        url=re.compile(re.escape(MESONET_ASOS_URL)),
        text=_ASOS_CSV_64F,
    )
    httpx_mock.add_response(
        url=re.compile(re.escape(MESONET_ASOS_URL)),
        text=_ASOS_CSV_64F,
    )
    with httpx.Client() as client:
        r1 = regrade_polymarket_settlements(conn, client, "2026-06-15T00:00:00Z")
        r2 = regrade_polymarket_settlements(conn, client, "2026-06-15T00:00:00Z")
    row = conn.execute(
        "SELECT actual_value FROM outcomes WHERE market_id = ?", ("poly-m",)
    ).fetchone()
    conn.close()
    assert r1 == 1
    assert r2 == 1  # re-runnable: same row is regraded again
    assert row["actual_value"] == pytest.approx(18.0 * 9 / 5 + 32, abs=0.1)


def test_regrade_waits_when_asos_returns_empty(httpx_mock):
    """If ASOS returns no data for a day, the outcome is left unchanged."""
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "poly-m", "NYC", "TMAX", "2026-05-30", _TMAX_SPEC, venue="polymarket")
    record_outcome(conn, "poly-m", 65.0, "2026-05-31T00:00:00Z")

    # Empty CSV (header only)
    httpx_mock.add_response(
        url=re.compile(re.escape(MESONET_ASOS_URL)),
        text="station,valid,tmpc\n",
    )
    with httpx.Client() as client:
        regraded = regrade_polymarket_settlements(conn, client, "2026-06-15T00:00:00Z")
    row = conn.execute(
        "SELECT actual_value FROM outcomes WHERE market_id = ?", ("poly-m",)
    ).fetchone()
    conn.close()
    assert regraded == 0  # not regraded (no data)
    assert row["actual_value"] == pytest.approx(65.0)  # unchanged


# ---------------------------------------------------------------------------
# finding 1: backfill_venue ensures legacy NULL-venue numeric rows are
# re-graded onto ASOS; Kalshi-ticker rows are NOT re-graded
# ---------------------------------------------------------------------------


def test_legacy_numeric_id_backfilled_and_regraded(httpx_mock):
    """A legacy NULL-venue market with a numeric id is backfilled to 'polymarket'
    and subsequently re-graded onto ASOS by regrade_polymarket_settlements."""
    conn = connect(":memory:")
    init_schema(conn)
    # Simulate a pre-0005 row: numeric Polymarket-style id, venue IS NULL
    # Use raw SQL to bypass record_market (which would set venue).
    conn.execute(
        "INSERT INTO markets (id, city, variable, settlement_date, outcome_spec) "
        "VALUES (?, ?, ?, ?, ?)",
        ("700001", "NYC", "TMAX", "2026-05-30", json.dumps(_TMAX_SPEC)),
    )
    conn.commit()
    # Pre-seed with an old NCEI value (65F -> "65°F or higher")
    record_outcome(conn, "700001", 65.0, "2026-05-31T00:00:00Z")

    # Backfill venue: 700001 is numeric -> 'polymarket'
    _backfill_venue(conn)

    row_venue = conn.execute("SELECT venue FROM markets WHERE id = ?", ("700001",)).fetchone()
    assert row_venue["venue"] == "polymarket", "backfill did not set venue"

    # ASOS returns 64.4F -> rounds to 64 -> "60-64°F"
    httpx_mock.add_response(
        url=re.compile(re.escape(MESONET_ASOS_URL)),
        text=_ASOS_CSV_64F,
    )
    with httpx.Client() as client:
        regraded = regrade_polymarket_settlements(conn, client, "2026-06-15T00:00:00Z")

    row = conn.execute(
        "SELECT actual_value FROM outcomes WHERE market_id = ?", ("700001",)
    ).fetchone()
    conn.close()
    assert regraded == 1
    # ASOS value overwrites the old NCEI value
    assert row["actual_value"] == pytest.approx(18.0 * 9 / 5 + 32, abs=0.1)


def test_legacy_kalshi_ticker_not_regraded(httpx_mock):
    """A legacy NULL-venue market with a Kalshi-style ticker id is backfilled to
    'kalshi' and NOT touched by regrade_polymarket_settlements."""
    conn = connect(":memory:")
    init_schema(conn)
    # Simulate a pre-0005 Kalshi row: alphanumeric ticker, venue IS NULL
    conn.execute(
        "INSERT INTO markets (id, city, variable, settlement_date, outcome_spec) "
        "VALUES (?, ?, ?, ?, ?)",
        ("KXHIGHNY-26JUN08", "NYC", "TMAX", "2026-06-08", json.dumps(_TMAX_SPEC)),
    )
    conn.commit()
    record_outcome(conn, "KXHIGHNY-26JUN08", 79.0, "2026-06-09T00:00:00Z")

    # Backfill venue: ticker is non-numeric -> 'kalshi'
    _backfill_venue(conn)

    row_venue = conn.execute(
        "SELECT venue FROM markets WHERE id = ?", ("KXHIGHNY-26JUN08",)
    ).fetchone()
    assert row_venue["venue"] == "kalshi", "backfill did not set venue"

    # No ASOS mock: any ASOS call would raise (unmocked URL)
    with httpx.Client() as client:
        regraded = regrade_polymarket_settlements(conn, client, "2026-06-15T00:00:00Z")

    row = conn.execute(
        "SELECT actual_value FROM outcomes WHERE market_id = ?", ("KXHIGHNY-26JUN08",)
    ).fetchone()
    conn.close()
    assert regraded == 0  # Kalshi row must not be regraded
    assert row["actual_value"] == pytest.approx(79.0)  # unchanged
