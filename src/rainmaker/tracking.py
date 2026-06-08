"""Score the bot against settled outcomes: hypothetical P&L and calibration.

Computed on read from predictions + prices + outcomes. One one-unit bet per
(market, UTC day): the best-edge recommended side/bucket from that day's latest
run. Buckets on one market describe the same temperature, so correlated
same-market bets collapse to one; the intraday runs (#77) that re-price a market
many times a day are correlated too, so they collapse to the latest run per UTC
day (#63, #78). Tracking only covers rows with a bucket recorded.
"""

import json
from collections import defaultdict
from datetime import date
from typing import Any

from rainmaker.config import STATIONS
from rainmaker.polymarket.markets import parse_bucket_label
from rainmaker.polymarket.precip_markets import parse_precip_bracket_label
from rainmaker.probability.calibration import CalibrationPair, compute_accuracy
from rainmaker.probability.outcomes import settles
from rainmaker.probability.precip_outcomes import precip_settles
from rainmaker.store.db import Conn
from rainmaker.store.record import save_accuracy


def _won(variable: str, bucket_label: str, actual_value: float) -> bool:
    if variable == "PRCP":
        return precip_settles(*parse_precip_bracket_label(bucket_label), actual_value)
    return settles(*parse_bucket_label(bucket_label), actual_value)


def _bet_won(row: dict[str, Any]) -> bool:
    """A YES bet wins when the bucket settles; a NO bet wins when it does not."""
    settled = _won(row["variable"], row["bucket"], row["actual_value"])
    return (not settled) if (row.get("side") or "YES") == "NO" else settled


def _latest_run_per_market_day(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only the latest run's rows per (market, UTC day).

    The intraday runs (#77) re-price a market many times a day; their bets are
    correlated, so counting each run separately inflates P&L and calibration
    (#63). started_at[:10] is the UTC day (same grain as compute_live_accuracy).
    Among rows sharing a (market_id, UTC day), keep only those whose run started
    latest; (started_at, run_id) breaks an exact-timestamp tie deterministically.
    """
    latest: dict[tuple[str, str], tuple[str, str]] = {}
    for r in rows:
        key = (r["market_id"], r["started_at"][:10])
        marker = (r["started_at"], r["run_id"])
        if key not in latest or marker > latest[key]:
            latest[key] = marker
    keep = {(market_id, run_id) for (market_id, _), (_, run_id) in latest.items()}
    return [r for r in rows if (r["market_id"], r["run_id"]) in keep]


def _settled_rows(conn: Conn) -> list[dict[str, Any]]:
    # Match the price to the prediction's side; legacy rows with a null side are YES.
    rows = conn.execute(
        "SELECT p.market_id AS market_id, p.run_id AS run_id, p.bucket AS bucket, "
        "p.side AS side, p.p_win AS p_win, p.edge AS edge, "
        "p.recommended AS recommended, m.variable AS variable, m.venue AS venue, "
        "r.started_at AS started_at, "
        "pr.price AS ask, o.actual_value AS actual_value "
        "FROM predictions p "
        "JOIN markets m ON m.id = p.market_id "
        "JOIN outcomes o ON o.market_id = p.market_id "
        "JOIN runs r ON r.id = p.run_id "
        "JOIN prices pr ON pr.run_id = p.run_id AND pr.market_id = p.market_id "
        "AND pr.outcome = p.bucket "
        "AND COALESCE(pr.side, 'YES') = COALESCE(p.side, 'YES') "
        "WHERE p.bucket IS NOT NULL AND pr.price IS NOT NULL"
    ).fetchall()
    return _latest_run_per_market_day([dict(r) for r in rows])


def _edge_key(r: dict[str, Any]) -> tuple[float, float, str, str]:
    edge = r["edge"] if r["edge"] is not None else float("-inf")
    return (edge, r["p_win"], r["bucket"], r.get("side") or "YES")


def _best_per_market_run(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse recommended bets to one per (market, run): the highest-edge bet.

    Buckets on one market all describe the same temperature, so NO bets across
    buckets win or lose together. Counting each separately would inflate P&L and
    hit rate, so keep only the best-edge bet per (market, run). Tie-break on
    (edge, p_win, bucket, side) for a deterministic pick.
    """
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        if not r["recommended"]:
            continue
        key = (r["market_id"], r["run_id"])
        if key not in best or _edge_key(r) > _edge_key(best[key]):
            best[key] = r
    return list(best.values())


def _filter_venue(rows: list[dict[str, Any]], venue: str | None) -> list[dict[str, Any]]:
    """Keep rows for one venue; legacy rows with a null venue count as polymarket."""
    if venue is None:
        return rows
    return [r for r in rows if (r.get("venue") or "polymarket") == venue]


def compute_pnl(conn: Conn, venue: str | None = None) -> dict[str, Any]:
    """Hypothetical P&L over recommended bets at a flat one-unit stake.

    With venue set ("polymarket" / "kalshi"), restrict to that venue's markets."""
    total_pnl = 0.0
    total_staked = 0.0
    wins = 0
    n = 0
    for r in _best_per_market_run(_filter_venue(_settled_rows(conn), venue)):
        n += 1
        ask = r["ask"]
        total_staked += ask
        if _bet_won(r):
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


def compute_calibration(conn: Conn, venue: str | None = None) -> dict[str, Any]:
    """Brier over the settled YES bucket-predictions, plus recommended hit rate.

    With venue set, restrict to that venue's markets."""
    rows = _filter_venue(_settled_rows(conn), venue)
    if not rows:
        return {"n": 0, "brier": None, "hit_rate": None}
    # Brier measures forecast calibration over the YES bucket-predictions; each NO
    # row's contribution is identical to its YES twin, so including it would only
    # double n. Hit rate is over the one best-edge bet per (market, run), either side.
    yes_rows = [r for r in rows if (r.get("side") or "YES") == "YES"]
    brier = (
        sum(
            (r["p_win"] - (1.0 if _won(r["variable"], r["bucket"], r["actual_value"]) else 0.0))
            ** 2
            for r in yes_rows
        )
        / len(yes_rows)
        if yes_rows
        else None
    )
    bets = _best_per_market_run(rows)
    hit_rate = sum(1 for r in bets if _bet_won(r)) / len(bets) if bets else None
    return {"n": len(yes_rows), "brier": brier, "hit_rate": hit_rate}


def compute_live_accuracy(conn: Conn) -> list[dict[str, Any]]:
    """Degrees-space accuracy of the bot's own forecasts over settled markets.

    One sample per (market, UTC day): the latest run's predicted mu against the
    settled actual, grouped per (station, variable, lead). DISTINCT collapses the
    per-bucket prediction rows, which share one dist_params, to one row per (run,
    market); _latest_run_per_market_day then keeps the latest run per (market, UTC
    day) so correlated intraday runs (#77) count once (#63, #78). This relies on
    _record_predictions writing an identical dist_params string for every bucket
    row of one (run, market); if that changes, replace DISTINCT with a subquery.
    Rows with an unknown city, unparsable dist_params, a null actual, or no usable
    mu/sigma are skipped.
    """
    rows = conn.execute(
        "SELECT DISTINCT p.run_id AS run_id, p.market_id AS market_id, "
        "p.dist_params AS dist_params, m.city AS city, m.variable AS variable, "
        "m.settlement_date AS settlement_date, r.started_at AS started_at, "
        "o.actual_value AS actual_value "
        "FROM predictions p "
        "JOIN outcomes o ON o.market_id = p.market_id "
        "JOIN markets m ON m.id = p.market_id "
        "JOIN runs r ON r.id = p.run_id "
        "WHERE p.dist_params IS NOT NULL AND o.actual_value IS NOT NULL"
    ).fetchall()
    groups: dict[tuple[str, str, str, int], list[CalibrationPair]] = defaultdict(list)
    for r in _latest_run_per_market_day([dict(row) for row in rows]):
        station = STATIONS.get(r["city"])
        if station is None:
            continue
        try:
            params = json.loads(r["dist_params"])
        except json.JSONDecodeError:
            continue  # unparsable dist_params: skip, never fail the snapshot
        mu, sigma = params.get("mu"), params.get("sigma")
        if mu is None or sigma is None or sigma <= 0:
            continue
        lead = (
            date.fromisoformat(r["settlement_date"]) - date.fromisoformat(r["started_at"][:10])
        ).days
        if lead < 0:
            continue  # a run after settlement is a catch-up, not a forecast: not accuracy
        key = (station.icao, r["city"], r["variable"], lead)
        groups[key].append(CalibrationPair(mu=mu, sigma=sigma, actual=r["actual_value"]))
    return [
        {
            "station": station,
            "city": city,
            "variable": variable,
            "lead_time": lead,
            "accuracy": compute_accuracy(pairs),
        }
        for (station, city, variable, lead), pairs in sorted(groups.items())
    ]


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
    # save_accuracy commits internally after each row; when there are no accuracy
    # rows, the snapshot upsert above is committed by conn.commit() below.
    for row in compute_live_accuracy(conn):
        save_accuracy(
            conn,
            station=row["station"],
            city=row["city"],
            variable=row["variable"],
            lead_time=row["lead_time"],
            kind="live",
            accuracy=row["accuracy"],
            updated_at=created_at,
        )
    conn.commit()
    return {"pnl": pnl, "calibration": cal}
