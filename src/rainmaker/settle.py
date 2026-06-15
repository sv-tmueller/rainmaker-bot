"""Settle recorded markets against NOAA actuals (a proxy for Weather Underground).

For each recorded market whose settlement date has passed and that has no outcome
yet, fetch the NOAA daily extreme for its station/date/variable and record it.
Idempotent: already-settled markets are skipped, and a market whose NOAA data is
not published yet is left for a later run.
"""

import json
import sys
from datetime import date
from typing import Any

import httpx

from rainmaker.backfill import fetch_actuals, fetch_monthly_precip
from rainmaker.config import PRECIP_STATIONS, STATIONS
from rainmaker.probability.outcomes import settles
from rainmaker.probability.precip_outcomes import precip_settles
from rainmaker.store.db import Conn
from rainmaker.store.query import unsettled_markets
from rainmaker.store.record import record_outcome


def _legacy_ghcnd(market: dict[str, str]) -> str | None:
    """The settlement GHCND from the city registry, for rows recorded before the
    markets.settlement_ghcnd column existed (it is NULL on those rows)."""
    if market["variable"] == "PRCP":
        precip = PRECIP_STATIONS.get(market["city"])
        return precip.ghcnd_id if precip is not None else None
    station = STATIONS.get(market["city"])
    return station.ghcnd_id if station is not None else None


def _grade_won(variable: str, bucket: dict[str, Any], actual_value: float, side: str) -> int:
    """Return 1 if the bet won, 0 if it lost."""
    kind = bucket["kind"]
    if variable == "PRCP":
        in_bucket = precip_settles(kind, bucket["lo"], bucket["hi"], bucket["threshold"], actual_value)
    else:
        in_bucket = settles(kind, bucket["lo"], bucket["hi"], bucket["threshold"], actual_value)
    # A NO bet wins when the bucket does not settle.
    return int((not in_bucket) if side == "NO" else in_bucket)


def _grade_predictions(conn: Conn) -> None:
    """Fill predictions.won for all recommended predictions where won IS NULL.

    Joins predictions -> outcomes (actual_value) -> markets (variable, outcome_spec).
    Idempotent: rows with won already set are skipped by the IS NULL filter.
    Covers both freshly-settled markets and pre-existing ones (backfill).
    """
    rows = conn.execute(
        "SELECT p.id, p.bucket, p.side, o.actual_value, m.variable, m.outcome_spec "
        "FROM predictions p "
        "JOIN outcomes o ON o.market_id = p.market_id "
        "JOIN markets m ON m.id = p.market_id "
        "WHERE p.recommended = 1 AND p.won IS NULL AND p.bucket IS NOT NULL"
    ).fetchall()
    for row in rows:
        try:
            spec: list[dict[str, Any]] = json.loads(row["outcome_spec"])
        except (TypeError, json.JSONDecodeError):
            continue
        bucket = next((b for b in spec if b["label"] == row["bucket"]), None)
        if bucket is None:
            continue
        won = _grade_won(row["variable"], bucket, row["actual_value"], row["side"] or "YES")
        conn.execute("UPDATE predictions SET won = ? WHERE id = ?", (won, row["id"]))
    conn.commit()


def run_settlement(
    conn: Conn, client: httpx.Client, today: date, settled_at: str
) -> tuple[int, int]:
    """Settle every unsettled past market that has NOAA data. Returns (settled, waiting)."""
    settled = 0
    waiting = 0
    for m in unsettled_markets(conn, today):
        day = date.fromisoformat(m["settlement_date"])
        # Use the market's exact settlement station (Kalshi NYC = Central Park, not
        # LaGuardia); fall back to the city registry for legacy rows.
        ghcnd = m.get("settlement_ghcnd") or _legacy_ghcnd(m)
        if ghcnd is None:
            print(f"skipping {m['market_id']}: no station for {m['city']!r}", file=sys.stderr)
            continue
        if m["variable"] not in {"TMAX", "TMIN", "PRCP"}:
            print(
                f"skipping {m['market_id']}: unknown variable {m['variable']!r}",
                file=sys.stderr,
            )
            continue
        try:
            if m["variable"] == "PRCP":
                value = fetch_monthly_precip(ghcnd, day.year, day.month, client)
            else:
                value = fetch_actuals(ghcnd, day, day, client, m["variable"]).get(day)
        except httpx.HTTPError as exc:
            # One station's transient NCEI error must not abort the rest of the loop.
            print(f"waiting on {m['market_id']}: NCEI fetch failed: {exc}", file=sys.stderr)
            waiting += 1
            continue
        if value is None:
            waiting += 1
            continue
        record_outcome(conn, m["market_id"], value, settled_at)
        settled += 1
    _grade_predictions(conn)
    return settled, waiting
