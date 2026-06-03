"""Read helpers over the persisted store.

The write/read round-trip these support is the integrity check for the schema and
recorder; Phase 4 backfill/calibration builds on the same read surface.
"""

from typing import Any

from rainmaker.probability.calibration import Calibration
from rainmaker.store.db import Conn


def get_run(conn: Conn, run_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return dict(row) if row is not None else None


def get_predictions(conn: Conn, run_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT market_id, p_win, edge, recommended FROM predictions "
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
