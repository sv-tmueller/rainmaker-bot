"""Prune redundant intraday rows for settled markets to bound storage (#78).

The every-3h cron (#77) writes prices/predictions/forecasts per run with no
upsert, so a settled market keeps many same-day runs even though tracking now
scores only the latest run per (market, UTC day). For SETTLED markets (an
outcomes row exists), delete the all-but-latest runs per (market_id, UTC day)
from prices, predictions, and forecasts. The durable history in
runs/markets/outcomes/tracking_snapshot/forecast_accuracy is never touched.

Day grouping is done in Python (started_at[:10]) so the SQL stays portable across
SQLite and Postgres. Idempotent: a second run deletes nothing.
"""

from collections import defaultdict

from rainmaker.store.db import Conn

_PRUNE_TABLES = ("prices", "predictions", "forecasts")


def _runs_to_prune(conn: Conn) -> list[tuple[str, str]]:
    """(run_id, market_id) pairs that are not the latest run for a settled (market, day).

    Anchors on both prices and predictions so markets that write only prices
    (precip path: no per-sample forecast rows) are not missed.
    """
    rows = conn.execute(
        "SELECT DISTINCT sub.market_id AS market_id, sub.run_id AS run_id, "
        "r.started_at AS started_at "
        "FROM ("
        "  SELECT market_id, run_id FROM prices"
        "  UNION"
        "  SELECT market_id, run_id FROM predictions"
        ") sub "
        "JOIN runs r ON r.id = sub.run_id "
        "JOIN outcomes o ON o.market_id = sub.market_id"
    ).fetchall()
    members: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for row in (dict(r) for r in rows):
        day = row["started_at"][:10]
        members[(row["market_id"], day)].append((row["started_at"], row["run_id"]))
    to_prune: list[tuple[str, str]] = []
    for (market_id, _day), runs in members.items():
        keep_run = max(runs)[1]  # latest started_at; run_id breaks an exact tie
        for _started_at, run_id in runs:
            if run_id != keep_run:
                to_prune.append((run_id, market_id))
    return to_prune


def prune_settled(conn: Conn) -> int:
    """Delete the all-but-latest intraday runs per settled (market, UTC day). Returns rows deleted.

    Does not commit; the caller commits. Tables are fixed internal identifiers,
    never user input; run_id and market_id are always bound as parameters.
    """
    deleted = 0
    for run_id, market_id in _runs_to_prune(conn):
        for table in _PRUNE_TABLES:
            cur = conn.execute(
                f"DELETE FROM {table} WHERE run_id = ? AND market_id = ?",
                (run_id, market_id),
            )
            deleted += cur.rowcount
    return deleted
