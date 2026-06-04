"""Score the bot against settled outcomes: hypothetical P&L and calibration.

Computed on read from predictions + prices + outcomes. Each recommended
prediction row is one one-unit bet (a market re-recommended across daily runs
counts as separate bets). Tracking only covers rows with a bucket recorded.
"""

from typing import Any

from rainmaker.polymarket.markets import parse_bucket_label
from rainmaker.store.db import Conn


def _won(bucket_label: str, actual_value: float) -> bool:
    kind, lo, hi, threshold = parse_bucket_label(bucket_label)
    v = round(actual_value)
    if kind == "below":
        assert threshold is not None
        return v <= threshold
    if kind == "above":
        assert threshold is not None
        return v >= threshold
    assert lo is not None and hi is not None
    return lo <= v <= hi


def _settled_rows(conn: Conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT p.bucket AS bucket, p.p_win AS p_win, p.recommended AS recommended, "
        "pr.price AS ask, o.actual_value AS actual_value "
        "FROM predictions p "
        "JOIN outcomes o ON o.market_id = p.market_id "
        "JOIN prices pr ON pr.run_id = p.run_id AND pr.market_id = p.market_id "
        "AND pr.outcome = p.bucket "
        "WHERE p.bucket IS NOT NULL AND pr.price IS NOT NULL"
    ).fetchall()
    return [dict(r) for r in rows]


def compute_pnl(conn: Conn) -> dict[str, Any]:
    """Hypothetical P&L over recommended bets at a flat one-unit stake."""
    total_pnl = 0.0
    total_staked = 0.0
    wins = 0
    n = 0
    for r in _settled_rows(conn):
        if not r["recommended"]:
            continue
        n += 1
        ask = r["ask"]
        total_staked += ask
        if _won(r["bucket"], r["actual_value"]):
            wins += 1
            total_pnl += 1 - ask
        else:
            total_pnl -= ask
    roi = total_pnl / total_staked if total_staked else 0.0
    return {
        "n_bets": n,
        "wins": wins,
        "losses": n - wins,
        "total_pnl": total_pnl,
        "roi": roi,
    }


def compute_calibration(conn: Conn) -> dict[str, Any]:
    """Brier score over all settled bucket-predictions, plus recommended hit rate."""
    rows = _settled_rows(conn)
    if not rows:
        return {"n": 0, "brier": None, "hit_rate": None}
    brier = sum(
        (r["p_win"] - (1.0 if _won(r["bucket"], r["actual_value"]) else 0.0)) ** 2 for r in rows
    ) / len(rows)
    recommended = [r for r in rows if r["recommended"]]
    hit_rate = (
        sum(1 for r in recommended if _won(r["bucket"], r["actual_value"])) / len(recommended)
        if recommended
        else None
    )
    return {"n": len(rows), "brier": brier, "hit_rate": hit_rate}


def write_snapshot(conn: Conn, on_date: str, created_at: str) -> dict[str, Any]:
    """Compute the current P&L/calibration and upsert a snapshot row for on_date."""
    pnl = compute_pnl(conn)
    cal = compute_calibration(conn)
    conn.execute(
        "INSERT INTO tracking_snapshot "
        "(snapshot_date, n_bets, wins, losses, total_pnl, roi, brier, hit_rate, "
        "n_scored, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(snapshot_date) DO UPDATE SET "
        "n_bets = excluded.n_bets, wins = excluded.wins, losses = excluded.losses, "
        "total_pnl = excluded.total_pnl, roi = excluded.roi, brier = excluded.brier, "
        "hit_rate = excluded.hit_rate, n_scored = excluded.n_scored, "
        "created_at = excluded.created_at",
        (
            on_date,
            pnl["n_bets"],
            pnl["wins"],
            pnl["losses"],
            pnl["total_pnl"],
            pnl["roi"],
            cal["brier"],
            cal["hit_rate"],
            cal["n"],
            created_at,
        ),
    )
    conn.commit()
    return {"pnl": pnl, "calibration": cal}
