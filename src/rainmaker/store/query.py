"""Read helpers over the persisted store.

The write/read round-trip these support is the integrity check for the schema and
recorder; Phase 4 backfill/calibration builds on the same read surface.
"""

import sqlite3
from typing import Any


def get_run(conn: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return dict(row) if row is not None else None


def get_predictions(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT market_id, p_win, edge, recommended FROM predictions "
        "WHERE run_id = ? ORDER BY edge DESC",
        (run_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def count_rows(conn: sqlite3.Connection, table: str) -> int:
    # table is a fixed internal identifier, never user input.
    (count,) = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
    return int(count)
