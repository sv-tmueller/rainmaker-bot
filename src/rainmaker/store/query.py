"""Read helpers over the persisted store.

The write/read round-trip these support is the integrity check for the schema and
recorder; Phase 4 backfill/calibration builds on the same read surface.
"""

from datetime import date
from typing import Any

from rainmaker.probability.calibration import Calibration
from rainmaker.store.db import Conn


def get_run(conn: Conn, run_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return dict(row) if row is not None else None


def get_predictions(conn: Conn, run_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT market_id, p_win, edge, recommended, won FROM predictions "
        "WHERE run_id = ? ORDER BY edge DESC",
        (run_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def count_rows(conn: Conn, table: str) -> int:
    # table is a fixed internal identifier, never user input.
    row = conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()
    return int(row["n"])


def load_calibration(conn: Conn, station: str, variable: str, lead_time: int) -> Calibration | None:
    row = conn.execute(
        "SELECT station, variable, lead_time, bias, spread_scale, n_samples "
        "FROM calibration WHERE station = ? AND variable = ? AND lead_time = ?",
        (station, variable, lead_time),
    ).fetchone()
    return Calibration(**dict(row)) if row is not None else None


def unsettled_markets(conn: Conn, before: date) -> list[dict[str, Any]]:
    """Recorded markets with a past settlement date and no outcome yet."""
    rows = conn.execute(
        "SELECT m.id AS market_id, m.city AS city, m.variable AS variable, "
        "m.settlement_date AS settlement_date, m.settlement_ghcnd AS settlement_ghcnd, "
        "m.venue AS venue "
        "FROM markets m LEFT JOIN outcomes o ON o.market_id = m.id "
        "WHERE o.market_id IS NULL AND m.settlement_date < ? "
        "ORDER BY m.settlement_date",
        (before.isoformat(),),
    ).fetchall()
    return [dict(r) for r in rows]


def settled_polymarket_temp_markets(conn: Conn) -> list[dict[str, Any]]:
    """Settled Polymarket TMAX/TMIN markets eligible for ASOS re-grade.

    Returns all markets that have an outcome and are Polymarket venue temperature
    markets. Used by regrade_polymarket_settlements in settle.py.
    """
    rows = conn.execute(
        "SELECT m.id AS market_id, m.city AS city, m.variable AS variable, "
        "m.settlement_date AS settlement_date, m.settlement_ghcnd AS settlement_ghcnd, "
        "m.venue AS venue "
        "FROM markets m JOIN outcomes o ON o.market_id = m.id "
        "WHERE m.venue = 'polymarket' "
        "AND m.variable IN ('TMAX', 'TMIN') "
        "ORDER BY m.settlement_date",
    ).fetchall()
    return [dict(r) for r in rows]
