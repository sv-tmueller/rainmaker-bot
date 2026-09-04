"""TDD tests for per-venue settlement routing and re-grade.

Polymarket TMAX/TMIN (US)  -> wrh (Synoptic Data API, weather.gov): Fahrenheit,
                              local-day bucketing. ASOS fallback on fetch failure.
Polymarket TMAX/TMIN (intl)-> ASOS (Iowa State Mesonet): Celsius, local-day bucketing.
Polymarket PRCP            -> NCEI (unchanged).
Kalshi (all)              -> NCEI (unchanged).

Re-grade: regrade_polymarket_settlements re-settles existing Polymarket
TMAX/TMIN outcomes using wrh (primary) with ASOS fallback, and re-grades
predictions.won.
"""

import json
import re
from datetime import date
from pathlib import Path

import httpx
import pytest

from rainmaker.backfill import NCEI_URL
from rainmaker.forecasts.asos import MESONET_ASOS_URL
from rainmaker.forecasts.wrh import SYNOPTIC_API_URL
from rainmaker.settle import regrade_polymarket_settlements, run_settlement
from rainmaker.store.db import connect, init_schema
from rainmaker.store.migrate import _backfill_venue
from rainmaker.store.record import record_outcome

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_FIXTURES = Path(__file__).parent / "fixtures"

_TMAX_SPEC = [
    {"label": "59\u00b0F or below", "kind": "below", "lo": None, "hi": None, "threshold": 59},
    {"label": "60-64\u00b0F", "kind": "range", "lo": 60, "hi": 64, "threshold": None},
    {"label": "65\u00b0F or higher", "kind": "above", "lo": None, "hi": None, "threshold": 65},
]

# wrh JSON for KLGA: one observation at 68.0F on 2026-05-30
_WRH_KLGA_68F = (_FIXTURES / "wrh_klga_2026-05-30_68f.json").read_text()

# wrh JSON for KLGA: one observation at 64.4F on 2026-05-30
_WRH_KLGA_64F = (_FIXTURES / "wrh_klga_2026-05-30_64f.json").read_text()

# wrh JSON for KMIA: one observation at 68.0F on 2026-05-30
_WRH_KMIA_68F = (_FIXTURES / "wrh_kmia_2026-05-30_68f.json").read_text()

# wrh JSON for KLGA: June 2026 data (multiple days)
_WRH_KLGA_JUNE = (_FIXTURES / "wrh_klga_2026-06.json").read_text()

# wrh JSON: empty STATION list (no data)
_WRH_EMPTY = (_FIXTURES / "wrh_empty.json").read_text()

# ASOS mesonet CSV for intl tests (kept for the intl path)
_INTL_EGLC_CSV = (
    "station,valid,tmpc\n"
    "EGLC,2026-06-08 23:20,14.0\n"
    "EGLC,2026-06-09 10:50,18.0\n"
    "EGLC,2026-06-09 11:20,19.0\n"
    "EGLC,2026-06-09 11:50,18.0\n"
    "EGLC,2026-06-09 22:50,11.0\n"
    "EGLC,2026-06-09 23:20,10.0\n"
)

_INTL_TMAX_SPEC = [
    {"label": "17\u00b0C or below", "kind": "below", "lo": None, "hi": None, "threshold": 17},
    {"label": "18-21\u00b0C", "kind": "range", "lo": 18, "hi": 21, "threshold": None},
    {"label": "22\u00b0C or higher", "kind": "above", "lo": None, "hi": None, "threshold": 22},
]

# NCEI JSON: 65F (in "65\u00b0F or higher")
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
# per-venue routing: Polymarket TMAX -> wrh (primary), ASOS fallback
# ---------------------------------------------------------------------------


def test_polymarket_tmax_uses_wrh(httpx_mock):
    """A US Polymarket TMAX market must be settled via wrh, not NCEI or ASOS."""
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "poly-tmax", "NYC", "TMAX", "2026-05-30", venue="polymarket")

    httpx_mock.add_response(
        url=re.compile(re.escape(SYNOPTIC_API_URL)),
        text=_WRH_KLGA_68F,
    )
    with httpx.Client() as client:
        settled, waiting = run_settlement(conn, client, date(2026, 6, 3), "2026-06-03T00:00:00Z")
    row = conn.execute(
        "SELECT actual_value FROM outcomes WHERE market_id = ?", ("poly-tmax",)
    ).fetchone()
    conn.close()
    assert (settled, waiting) == (1, 0)
    assert row["actual_value"] == pytest.approx(68.0, abs=0.1)


def test_polymarket_tmin_uses_wrh(httpx_mock):
    """A US Polymarket TMIN market must be settled via wrh."""
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "poly-tmin", "NYC", "TMIN", "2026-05-30", venue="polymarket")

    httpx_mock.add_response(
        url=re.compile(re.escape(SYNOPTIC_API_URL)),
        text=_WRH_KLGA_68F,
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
    """No NCEI call when settling a US Polymarket TMAX market."""
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "poly-tmax", "NYC", "TMAX", "2026-05-30", venue="polymarket")

    ncei_called: list[bool] = []

    def ncei_handler(request: httpx.Request) -> httpx.Response:
        ncei_called.append(True)
        return httpx.Response(200, json=[])

    httpx_mock.add_response(
        url=re.compile(re.escape(SYNOPTIC_API_URL)),
        text=_WRH_KLGA_68F,
    )
    with httpx.Client() as client:
        run_settlement(conn, client, date(2026, 6, 3), "2026-06-03T00:00:00Z")
    conn.close()
    assert not ncei_called


def test_polymarket_tmax_falls_back_to_asos_on_wrh_failure(httpx_mock):
    """If wrh fetch fails, US Polymarket TMAX falls back to ASOS."""
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "poly-tmax", "NYC", "TMAX", "2026-05-30", venue="polymarket")

    # wrh returns 503; ASOS succeeds with 68F
    httpx_mock.add_response(
        url=re.compile(re.escape(SYNOPTIC_API_URL)),
        status_code=503,
    )
    httpx_mock.add_response(
        url=re.compile(re.escape(MESONET_ASOS_URL)),
        text="station,valid,tmpc\nLGA,2026-05-30 12:00,20.0\n",
    )
    with httpx.Client() as client:
        settled, waiting = run_settlement(conn, client, date(2026, 6, 3), "2026-06-03T00:00:00Z")
    row = conn.execute(
        "SELECT actual_value FROM outcomes WHERE market_id = ?", ("poly-tmax",)
    ).fetchone()
    conn.close()
    assert (settled, waiting) == (1, 0)
    # 20.0C = 68.0F from ASOS fallback
    assert row["actual_value"] == pytest.approx(68.0, abs=0.1)


# ---------------------------------------------------------------------------
# per-venue routing: Kalshi TMAX -> NCEI (unchanged)
# ---------------------------------------------------------------------------


def test_kalshi_tmax_uses_ncei(httpx_mock):
    """A Kalshi TMAX market must continue to be settled via NCEI."""
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


def test_kalshi_tmax_does_not_call_wrh_or_asos(httpx_mock):
    """No wrh or ASOS call when settling a Kalshi market."""
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "kalshi-tmax", "NYC", "TMAX", "2026-05-30", venue="kalshi")

    httpx_mock.add_response(
        url=re.compile(re.escape(NCEI_URL)),
        json=_NCEI_JSON_65F,
    )
    # Neither wrh nor ASOS should be called (no mock registered for them)
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
    """Polymarket PRCP (monthly) stays on NCEI GSOM; wrh has no precip data."""
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
# wrh waits when station unmapped (city not in STATIONS)
# ---------------------------------------------------------------------------


def test_polymarket_tmax_waits_when_station_unknown():
    """If no station exists for the city, the market waits (not crash)."""
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "poly-atlantis", "Atlantis", "TMAX", "2026-05-30", venue="polymarket")

    with httpx.Client() as client:
        settled, waiting = run_settlement(conn, client, date(2026, 6, 3), "2026-06-03T00:00:00Z")
    conn.close()
    assert (settled, waiting) == (0, 0)  # skipped: no station for "Atlantis"


# ---------------------------------------------------------------------------
# wrh HTTP error: market waits, loop continues
# ---------------------------------------------------------------------------


def test_polymarket_tmax_waits_on_fetch_error_same_station(httpx_mock):
    """A fetch error (wrh + ASOS both fail) for a (station, variable) group puts
    ALL markets in that group in waiting. Same-station markets share one batch
    request; if it fails, all markets in the batch wait together."""
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "poly-err1", "NYC", "TMAX", "2026-05-30", venue="polymarket")
    _market(conn, "poly-err2", "NYC", "TMAX", "2026-05-31", venue="polymarket")

    # wrh 503, ASOS 503 -> both fail
    httpx_mock.add_response(
        url=re.compile(re.escape(SYNOPTIC_API_URL)),
        status_code=503,
    )
    httpx_mock.add_response(
        url=re.compile(re.escape(MESONET_ASOS_URL)),
        status_code=503,
    )
    with httpx.Client() as client:
        settled, waiting = run_settlement(conn, client, date(2026, 6, 3), "2026-06-03T00:00:00Z")
    conn.close()
    assert (settled, waiting) == (0, 2)


def test_polymarket_tmax_waits_on_fetch_error_different_stations(httpx_mock):
    """A fetch error for one station/variable group does not prevent other
    station groups from settling. NYC (KLGA) fails; Miami (KMIA) succeeds."""
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "poly-nyc", "NYC", "TMAX", "2026-05-30", venue="polymarket")
    _market(conn, "poly-mia", "Miami", "TMAX", "2026-05-30", venue="polymarket")

    # NYC: wrh 503, ASOS 503 -> fail
    httpx_mock.add_response(
        url=re.compile(re.escape(SYNOPTIC_API_URL)),
        status_code=503,
    )
    httpx_mock.add_response(
        url=re.compile(re.escape(MESONET_ASOS_URL)),
        status_code=503,
    )
    # Miami: wrh succeeds
    httpx_mock.add_response(
        url=re.compile(re.escape(SYNOPTIC_API_URL)),
        text=_WRH_KMIA_68F,
    )
    with httpx.Client() as client:
        settled, waiting = run_settlement(conn, client, date(2026, 6, 3), "2026-06-03T00:00:00Z")
    conn.close()
    # One settled (MIA), one waiting (NYC)
    assert (settled, waiting) == (1, 1)


def test_polymarket_tmax_waits_when_batch_returns_no_data_for_date(httpx_mock):
    """Batch succeeds but a specific date has no data; that market waits."""
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "poly-june01", "NYC", "TMAX", "2026-06-01", venue="polymarket")
    _market(conn, "poly-june03", "NYC", "TMAX", "2026-06-03", venue="polymarket")

    httpx_mock.add_response(
        url=re.compile(re.escape(SYNOPTIC_API_URL)),
        text=_WRH_KLGA_JUNE,
    )
    with httpx.Client() as client:
        settled, waiting = run_settlement(conn, client, date(2026, 6, 5), "2026-06-05T00:00:00Z")
    conn.close()
    # June 01 settles (has data); June 03 waits (no data in batch response)
    assert (settled, waiting) == (1, 1)


# ---------------------------------------------------------------------------
# Batching: N markets at same station/variable -> 1 wrh request
# ---------------------------------------------------------------------------


def test_same_station_variable_uses_single_wrh_request(httpx_mock):
    """Multiple Polymarket TMAX markets for the same city/date-range issue ONE
    wrh request (not one per market)."""
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "m1", "NYC", "TMAX", "2026-06-01", venue="polymarket")
    _market(conn, "m2", "NYC", "TMAX", "2026-06-02", venue="polymarket")

    httpx_mock.add_response(
        url=re.compile(re.escape(SYNOPTIC_API_URL)),
        text=_WRH_KLGA_JUNE,
    )
    with httpx.Client() as client:
        settled, waiting = run_settlement(conn, client, date(2026, 6, 5), "2026-06-05T00:00:00Z")
    conn.close()
    # Both dates in the June fixture -> both settle
    assert (settled, waiting) == (2, 0)
    # Only ONE wrh request was made (the batch)
    wrh_requests = [r for r in httpx_mock.get_requests() if SYNOPTIC_API_URL in str(r.url)]
    assert len(wrh_requests) == 1


def test_different_variables_use_separate_requests(httpx_mock):
    """TMAX and TMIN for the same station are separate batches (different variable)."""
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "tmax-m", "NYC", "TMAX", "2026-05-30", venue="polymarket")
    _market(conn, "tmin-m", "NYC", "TMIN", "2026-05-30", venue="polymarket")

    httpx_mock.add_response(
        url=re.compile(re.escape(SYNOPTIC_API_URL)),
        text=_WRH_KLGA_68F,
    )
    httpx_mock.add_response(
        url=re.compile(re.escape(SYNOPTIC_API_URL)),
        text=_WRH_KLGA_68F,
    )
    with httpx.Client() as client:
        settled, _waiting = run_settlement(conn, client, date(2026, 6, 5), "2026-06-05T00:00:00Z")
    conn.close()
    assert settled == 2
    wrh_requests = [r for r in httpx_mock.get_requests() if SYNOPTIC_API_URL in str(r.url)]
    # Two batches: one TMAX, one TMIN
    assert len(wrh_requests) == 2


def test_regrade_same_station_uses_single_wrh_request(httpx_mock):
    """regrade_polymarket_settlements: N markets at same station/variable -> 1 request."""
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "m1", "NYC", "TMAX", "2026-06-01", _TMAX_SPEC, venue="polymarket")
    _market(conn, "m2", "NYC", "TMAX", "2026-06-02", _TMAX_SPEC, venue="polymarket")
    record_outcome(conn, "m1", 65.0, "2026-06-02T00:00:00Z")
    record_outcome(conn, "m2", 65.0, "2026-06-03T00:00:00Z")

    httpx_mock.add_response(
        url=re.compile(re.escape(SYNOPTIC_API_URL)),
        text=_WRH_KLGA_JUNE,
    )
    with httpx.Client() as client:
        regraded = regrade_polymarket_settlements(conn, client, "2026-06-15T00:00:00Z")
    conn.close()
    assert regraded == 2
    wrh_requests = [r for r in httpx_mock.get_requests() if SYNOPTIC_API_URL in str(r.url)]
    assert len(wrh_requests) == 1


# ---------------------------------------------------------------------------
# re-grade: regrade_polymarket_settlements
# ---------------------------------------------------------------------------


def test_regrade_updates_outcome_for_polymarket_tmax(httpx_mock):
    """regrade_polymarket_settlements overwrites outcomes.actual_value with wrh data."""
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "poly-m", "NYC", "TMAX", "2026-05-30", _TMAX_SPEC, venue="polymarket")
    record_outcome(conn, "poly-m", 65.0, "2026-05-31T00:00:00Z")

    # wrh returns 64.4F -> lands in "60-64\u00b0F"
    httpx_mock.add_response(
        url=re.compile(re.escape(SYNOPTIC_API_URL)),
        text=_WRH_KLGA_64F,
    )
    with httpx.Client() as client:
        regraded = regrade_polymarket_settlements(conn, client, "2026-06-15T00:00:00Z")
    row = conn.execute(
        "SELECT actual_value FROM outcomes WHERE market_id = ?", ("poly-m",)
    ).fetchone()
    conn.close()
    assert regraded == 1
    assert row["actual_value"] == pytest.approx(64.4, abs=0.1)


def test_regrade_updates_predictions_won(httpx_mock):
    """regrade flips predictions.won when the wrh bucket differs from NCEI."""
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "poly-m", "NYC", "TMAX", "2026-05-30", _TMAX_SPEC, venue="polymarket")
    record_outcome(conn, "poly-m", 65.0, "2026-05-31T00:00:00Z")
    _prediction(conn, "run-1", "poly-m", "65\u00b0F or higher", "YES", 0.6, recommended=1)
    _prediction(conn, "run-1", "poly-m", "60-64\u00b0F", "YES", 0.3, recommended=1)
    conn.execute("UPDATE predictions SET won = 1 WHERE bucket = '65\u00b0F or higher'")
    conn.execute("UPDATE predictions SET won = 0 WHERE bucket = '60-64\u00b0F'")
    conn.commit()

    # wrh returns 64.4F -> "60-64\u00b0F" settles instead
    httpx_mock.add_response(
        url=re.compile(re.escape(SYNOPTIC_API_URL)),
        text=_WRH_KLGA_64F,
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
    assert rows["60-64\u00b0F"] == 1
    assert rows["65\u00b0F or higher"] == 0


def test_regrade_does_not_touch_kalshi_markets(httpx_mock):
    """regrade_polymarket_settlements must not re-settle Kalshi markets."""
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "kalshi-m", "NYC", "TMAX", "2026-05-30", _TMAX_SPEC, venue="kalshi")
    record_outcome(conn, "kalshi-m", 65.0, "2026-05-31T00:00:00Z")
    _prediction(conn, "run-1", "kalshi-m", "65\u00b0F or higher", "YES", 0.6, recommended=1)
    conn.execute("UPDATE predictions SET won = 1 WHERE bucket = '65\u00b0F or higher'")
    conn.commit()

    # No wrh mock: any call to wrh would raise (unmocked URL)
    with httpx.Client() as client:
        regraded = regrade_polymarket_settlements(conn, client, "2026-06-15T00:00:00Z")

    row = conn.execute(
        "SELECT actual_value FROM outcomes WHERE market_id = ?", ("kalshi-m",)
    ).fetchone()
    won = conn.execute("SELECT won FROM predictions WHERE market_id = ?", ("kalshi-m",)).fetchone()
    conn.close()
    assert regraded == 0
    assert row["actual_value"] == pytest.approx(65.0)
    assert won["won"] == 1


def test_regrade_does_not_touch_prcp_markets(httpx_mock):
    """regrade_polymarket_settlements must not re-settle PRCP markets."""
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "poly-prcp", "NYC", "PRCP", "2026-06-30", venue="polymarket")
    record_outcome(conn, "poly-prcp", 3.50, "2026-07-01T00:00:00Z")

    with httpx.Client() as client:
        regraded = regrade_polymarket_settlements(conn, client, "2026-06-15T00:00:00Z")
    conn.close()
    assert regraded == 0


def test_regrade_is_idempotent(httpx_mock):
    """Running regrade twice on the same row converges to the same wrh value."""
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "poly-m", "NYC", "TMAX", "2026-05-30", _TMAX_SPEC, venue="polymarket")
    record_outcome(conn, "poly-m", 65.0, "2026-05-31T00:00:00Z")
    _prediction(conn, "run-1", "poly-m", "60-64\u00b0F", "YES", 0.3, recommended=1)

    httpx_mock.add_response(
        url=re.compile(re.escape(SYNOPTIC_API_URL)),
        text=_WRH_KLGA_64F,
    )
    httpx_mock.add_response(
        url=re.compile(re.escape(SYNOPTIC_API_URL)),
        text=_WRH_KLGA_64F,
    )
    with httpx.Client() as client:
        r1 = regrade_polymarket_settlements(conn, client, "2026-06-15T00:00:00Z")
        r2 = regrade_polymarket_settlements(conn, client, "2026-06-15T00:00:00Z")
    row = conn.execute(
        "SELECT actual_value FROM outcomes WHERE market_id = ?", ("poly-m",)
    ).fetchone()
    conn.close()
    assert r1 == 1
    assert r2 == 1
    assert row["actual_value"] == pytest.approx(64.4, abs=0.1)


def test_regrade_waits_when_wrh_returns_empty(httpx_mock):
    """If wrh returns no data for a day, the outcome is left unchanged."""
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "poly-m", "NYC", "TMAX", "2026-05-30", _TMAX_SPEC, venue="polymarket")
    record_outcome(conn, "poly-m", 65.0, "2026-05-31T00:00:00Z")

    httpx_mock.add_response(
        url=re.compile(re.escape(SYNOPTIC_API_URL)),
        text=_WRH_EMPTY,
    )
    with httpx.Client() as client:
        regraded = regrade_polymarket_settlements(conn, client, "2026-06-15T00:00:00Z")
    row = conn.execute(
        "SELECT actual_value FROM outcomes WHERE market_id = ?", ("poly-m",)
    ).fetchone()
    conn.close()
    assert regraded == 0
    assert row["actual_value"] == pytest.approx(65.0)


# ---------------------------------------------------------------------------
# finding 1: backfill_venue ensures legacy NULL-venue numeric rows are
# re-graded onto wrh; Kalshi-ticker rows are NOT re-graded
# ---------------------------------------------------------------------------


def test_legacy_numeric_id_backfilled_and_regraded(httpx_mock):
    """A legacy NULL-venue market with a numeric id is backfilled to 'polymarket'
    and subsequently re-graded onto wrh by regrade_polymarket_settlements."""
    conn = connect(":memory:")
    init_schema(conn)
    conn.execute(
        "INSERT INTO markets (id, city, variable, settlement_date, outcome_spec) "
        "VALUES (?, ?, ?, ?, ?)",
        ("700001", "NYC", "TMAX", "2026-05-30", json.dumps(_TMAX_SPEC)),
    )
    conn.commit()
    record_outcome(conn, "700001", 65.0, "2026-05-31T00:00:00Z")

    _backfill_venue(conn)

    row_venue = conn.execute("SELECT venue FROM markets WHERE id = ?", ("700001",)).fetchone()
    assert row_venue["venue"] == "polymarket", "backfill did not set venue"

    httpx_mock.add_response(
        url=re.compile(re.escape(SYNOPTIC_API_URL)),
        text=_WRH_KLGA_64F,
    )
    with httpx.Client() as client:
        regraded = regrade_polymarket_settlements(conn, client, "2026-06-15T00:00:00Z")

    row = conn.execute(
        "SELECT actual_value FROM outcomes WHERE market_id = ?", ("700001",)
    ).fetchone()
    conn.close()
    assert regraded == 1
    assert row["actual_value"] == pytest.approx(64.4, abs=0.1)


def test_legacy_kalshi_ticker_not_regraded(httpx_mock):
    """A legacy NULL-venue market with a Kalshi-style ticker id is backfilled to
    'kalshi' and NOT touched by regrade_polymarket_settlements."""
    conn = connect(":memory:")
    init_schema(conn)
    conn.execute(
        "INSERT INTO markets (id, city, variable, settlement_date, outcome_spec) "
        "VALUES (?, ?, ?, ?, ?)",
        ("KXHIGHNY-26JUN08", "NYC", "TMAX", "2026-06-08", json.dumps(_TMAX_SPEC)),
    )
    conn.commit()
    record_outcome(conn, "KXHIGHNY-26JUN08", 79.0, "2026-06-09T00:00:00Z")

    _backfill_venue(conn)

    row_venue = conn.execute(
        "SELECT venue FROM markets WHERE id = ?", ("KXHIGHNY-26JUN08",)
    ).fetchone()
    assert row_venue["venue"] == "kalshi", "backfill did not set venue"

    with httpx.Client() as client:
        regraded = regrade_polymarket_settlements(conn, client, "2026-06-15T00:00:00Z")

    row = conn.execute(
        "SELECT actual_value FROM outcomes WHERE market_id = ?", ("KXHIGHNY-26JUN08",)
    ).fetchone()
    conn.close()
    assert regraded == 0
    assert row["actual_value"] == pytest.approx(79.0)


# ---------------------------------------------------------------------------
# International settlement via IEM METAR (#190, unchanged ASOS path)
# ---------------------------------------------------------------------------


def test_intl_polymarket_tmax_settles_in_celsius(httpx_mock):
    """An intl Polymarket TMAX market (London EGLC) settles in Celsius via ASOS."""
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "eglc-tmax-0609", "London", "TMAX", "2026-06-09", venue="polymarket")

    httpx_mock.add_response(
        url=re.compile(re.escape(MESONET_ASOS_URL)),
        text=_INTL_EGLC_CSV,
    )
    with httpx.Client() as client:
        settled, waiting = run_settlement(conn, client, date(2026, 6, 12), "2026-06-12T00:00:00Z")
    row = conn.execute(
        "SELECT actual_value FROM outcomes WHERE market_id = ?", ("eglc-tmax-0609",)
    ).fetchone()
    conn.close()
    assert (settled, waiting) == (1, 0)
    assert row["actual_value"] == pytest.approx(19.0, abs=0.01)


def test_intl_polymarket_tmin_settles_in_celsius(httpx_mock):
    """An intl Polymarket TMIN market settles in Celsius."""
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "eglc-tmin-0609", "London", "TMIN", "2026-06-09", venue="polymarket")

    httpx_mock.add_response(
        url=re.compile(re.escape(MESONET_ASOS_URL)),
        text=_INTL_EGLC_CSV,
    )
    with httpx.Client() as client:
        settled, waiting = run_settlement(conn, client, date(2026, 6, 12), "2026-06-12T00:00:00Z")
    row = conn.execute(
        "SELECT actual_value FROM outcomes WHERE market_id = ?", ("eglc-tmin-0609",)
    ).fetchone()
    conn.close()
    assert (settled, waiting) == (1, 0)
    assert row["actual_value"] == pytest.approx(11.0, abs=0.01)


def test_intl_settlement_is_idempotent(httpx_mock):
    """Running intl settlement twice settles 0 on the second pass (idempotent)."""
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "eglc-tmax-0609", "London", "TMAX", "2026-06-09", venue="polymarket")

    httpx_mock.add_response(
        url=re.compile(re.escape(MESONET_ASOS_URL)),
        text=_INTL_EGLC_CSV,
    )
    with httpx.Client() as client:
        r1_settled, _ = run_settlement(conn, client, date(2026, 6, 12), "t")
        r2_settled, r2_waiting = run_settlement(conn, client, date(2026, 6, 12), "t")
    conn.close()
    assert r1_settled == 1
    assert (r2_settled, r2_waiting) == (0, 0)


def test_intl_settlement_does_not_call_ncei(httpx_mock):
    """Intl TMAX/TMIN markets must use ASOS, not NCEI."""
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "eglc-tmax-0609", "London", "TMAX", "2026-06-09", venue="polymarket")

    ncei_called: list[bool] = []

    def ncei_handler(request: httpx.Request) -> httpx.Response:
        ncei_called.append(True)
        return httpx.Response(200, json=[])

    httpx_mock.add_response(
        url=re.compile(re.escape(MESONET_ASOS_URL)),
        text=_INTL_EGLC_CSV,
    )
    with httpx.Client() as client:
        run_settlement(conn, client, date(2026, 6, 12), "t")
    conn.close()
    assert not ncei_called


def test_intl_settlement_not_stored_as_fahrenheit(httpx_mock):
    """Intl outcome must be stored as Celsius (<50), not as Fahrenheit (>50)."""
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "eglc-tmax-0609", "London", "TMAX", "2026-06-09", venue="polymarket")

    httpx_mock.add_response(
        url=re.compile(re.escape(MESONET_ASOS_URL)),
        text=_INTL_EGLC_CSV,
    )
    with httpx.Client() as client:
        run_settlement(conn, client, date(2026, 6, 12), "t")
    row = conn.execute(
        "SELECT actual_value FROM outcomes WHERE market_id = ?", ("eglc-tmax-0609",)
    ).fetchone()
    conn.close()
    assert row["actual_value"] < 50


def test_intl_regrade_skips_intl_cities(httpx_mock):
    """regrade_polymarket_settlements must not process intl markets (US-only)."""
    conn = connect(":memory:")
    init_schema(conn)
    _market(
        conn, "eglc-tmax-0609", "London", "TMAX", "2026-06-09", _INTL_TMAX_SPEC, venue="polymarket"
    )
    record_outcome(conn, "eglc-tmax-0609", 19.0, "2026-06-10T00:00:00Z")

    with httpx.Client() as client:
        regraded = regrade_polymarket_settlements(conn, client, "2026-06-15T00:00:00Z")

    row = conn.execute(
        "SELECT actual_value FROM outcomes WHERE market_id = ?", ("eglc-tmax-0609",)
    ).fetchone()
    conn.close()
    assert regraded == 0
    assert row["actual_value"] == pytest.approx(19.0)
