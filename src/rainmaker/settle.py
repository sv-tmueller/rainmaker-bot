"""Settle recorded markets against NOAA actuals (a proxy for Weather Underground).

For each recorded market whose settlement date has passed and that has no outcome
yet, fetch the daily extreme for its station/date/variable and record it.
Idempotent: already-settled markets are skipped, and a market whose data is
not published yet is left for a later run.

Settlement source routing:
  Polymarket TMAX/TMIN -> ASOS (Iowa State Mesonet): matches Polymarket's resolution source.
  Polymarket PRCP      -> NCEI GSOM: no ASOS precip path.
  Kalshi (all)         -> NCEI GHCND: Kalshi settles on the NOAA daily climate report.
  NULL venue (legacy)  -> NCEI GHCND: safe fallback, does not send legacy rows to ASOS.

Batching (ASOS path):
  Polymarket TMAX/TMIN markets are grouped by (asos_station, variable). All markets
  in a group share one Mesonet request covering their full date range. This collapses
  O(markets) requests to O(distinct station x variable) <= ~22 per run.
"""

import json
import sys
from collections import defaultdict
from datetime import date
from typing import Any

import httpx

from rainmaker.backfill import fetch_actuals, fetch_monthly_precip
from rainmaker.config import PRECIP_STATIONS, STATIONS
from rainmaker.forecasts.asos import ICAO_TO_ASOS_STATION, fetch_asos_daily_extreme
from rainmaker.probability.outcomes import settles
from rainmaker.probability.precip_outcomes import precip_settles
from rainmaker.store.db import Conn
from rainmaker.store.query import settled_polymarket_temp_markets, unsettled_markets
from rainmaker.store.record import record_outcome


def _legacy_ghcnd(market: dict[str, str]) -> str | None:
    """The settlement GHCND from the city registry, for rows recorded before the
    markets.settlement_ghcnd column existed (it is NULL on those rows)."""
    if market["variable"] == "PRCP":
        precip = PRECIP_STATIONS.get(market["city"])
        return precip.ghcnd_id if precip is not None else None
    station = STATIONS.get(market["city"])
    return station.ghcnd_id if station is not None else None


def _asos_code_for(city: str) -> str | None:
    """Return the Mesonet 3-letter ASOS code for a city, or None if unmapped."""
    station = STATIONS.get(city)
    if station is None:
        return None
    return ICAO_TO_ASOS_STATION.get(station.icao)


def _grade_won(variable: str, bucket: dict[str, Any], actual_value: float, side: str) -> int:
    """Return 1 if the bet won, 0 if it lost."""
    kind = bucket["kind"]
    lo, hi, thr = bucket["lo"], bucket["hi"], bucket["threshold"]
    if variable == "PRCP":
        in_bucket = precip_settles(kind, lo, hi, thr, actual_value)
    else:
        in_bucket = settles(kind, lo, hi, thr, actual_value)
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


def _grade_predictions_for_market(conn: Conn, market_id: str, actual_value: float) -> None:
    """Re-grade all recommended predictions for a specific market against a new actual value.

    Unconditionally overwrites predictions.won (ignores the IS NULL guard).
    Used by regrade_polymarket_settlements after the outcome is overwritten.
    """
    rows = conn.execute(
        "SELECT p.id, p.bucket, p.side, m.variable, m.outcome_spec "
        "FROM predictions p "
        "JOIN markets m ON m.id = p.market_id "
        "WHERE p.market_id = ? AND p.recommended = 1 AND p.bucket IS NOT NULL",
        (market_id,),
    ).fetchall()
    for row in rows:
        try:
            spec: list[dict[str, Any]] = json.loads(row["outcome_spec"])
        except (TypeError, json.JSONDecodeError):
            continue
        bucket = next((b for b in spec if b["label"] == row["bucket"]), None)
        if bucket is None:
            continue
        won = _grade_won(row["variable"], bucket, actual_value, row["side"] or "YES")
        conn.execute("UPDATE predictions SET won = ? WHERE id = ?", (won, row["id"]))
    conn.commit()


def run_settlement(
    conn: Conn, client: httpx.Client, today: date, settled_at: str
) -> tuple[int, int]:
    """Settle every unsettled past market. Returns (settled, waiting).

    Routes by venue and variable:
    - Polymarket TMAX/TMIN -> ASOS (Iowa State Mesonet), batched by station+variable
    - Polymarket PRCP      -> NCEI GSOM
    - Kalshi (all)         -> NCEI GHCND
    - NULL venue (legacy)  -> NCEI GHCND (safe fallback)
    """
    all_markets = unsettled_markets(conn, today)
    settled = 0
    waiting = 0

    # Separate Polymarket TMAX/TMIN markets (ASOS path) from everything else.
    asos_markets: list[dict[str, Any]] = []
    other_markets: list[dict[str, Any]] = []

    for m in all_markets:
        variable = m["variable"]
        venue = m.get("venue") or ""

        if variable not in {"TMAX", "TMIN", "PRCP"}:
            print(
                f"skipping {m['market_id']}: unknown variable {variable!r}",
                file=sys.stderr,
            )
            continue

        if venue == "polymarket" and variable in {"TMAX", "TMIN"}:
            asos_markets.append(m)
        else:
            other_markets.append(m)

    # --- ASOS batch path ---
    # Group by (asos_code, variable), fetch once per group over [min_date, max_date].
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)

    for m in asos_markets:
        asos_code = _asos_code_for(m["city"])
        if asos_code is None:
            print(
                f"skipping {m['market_id']}: no station for {m['city']!r}",
                file=sys.stderr,
            )
            continue
        groups[(asos_code, m["variable"])].append(m)

    for (asos_code, variable), group_markets in groups.items():
        dates = [date.fromisoformat(m["settlement_date"]) for m in group_markets]
        start = min(dates)
        end = max(dates)
        try:
            lookup = fetch_asos_daily_extreme(asos_code, start, end, client, variable)
        except httpx.HTTPError as exc:
            for m in group_markets:
                print(
                    f"waiting on {m['market_id']}: ASOS fetch failed: {exc}",
                    file=sys.stderr,
                )
                waiting += 1
            continue
        for m in group_markets:
            day = date.fromisoformat(m["settlement_date"])
            value = lookup.get(day)
            if value is None:
                waiting += 1
                continue
            record_outcome(conn, m["market_id"], value, settled_at)
            settled += 1

    # --- NCEI path (Kalshi, PRCP, NULL-venue legacy) ---
    for m in other_markets:
        day = date.fromisoformat(m["settlement_date"])
        variable = m["variable"]
        ghcnd = m.get("settlement_ghcnd") or _legacy_ghcnd(m)
        if ghcnd is None:
            print(f"skipping {m['market_id']}: no station for {m['city']!r}", file=sys.stderr)
            continue
        try:
            if variable == "PRCP":
                value = fetch_monthly_precip(ghcnd, day.year, day.month, client)
            else:
                value = fetch_actuals(ghcnd, day, day, client, variable).get(day)
        except httpx.HTTPError as exc:
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


def regrade_polymarket_settlements(conn: Conn, client: httpx.Client, regraded_at: str) -> int:
    """Re-settle existing Polymarket TMAX/TMIN outcomes using ASOS and re-grade predictions.

    Fetches the ASOS daily extreme for every settled Polymarket TMAX/TMIN market,
    overwrites outcomes.actual_value, and re-grades predictions.won.

    Markets are batched by (asos_station, variable): one Mesonet request covers
    all markets at the same station for the same variable.

    Returns the number of markets successfully regraded. Markets where ASOS
    returns no data for the settlement date are skipped (not counted).

    Re-runnable: running twice converges to the same ASOS value.
    """
    all_markets = settled_polymarket_temp_markets(conn)

    # Group by (asos_code, variable), fetch once per group.
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for m in all_markets:
        asos_code = _asos_code_for(m["city"])
        if asos_code is None:
            # No ASOS mapping for this city: skip silently (should not occur for
            # the 11 known cities, but do not crash if a legacy row appears).
            continue
        groups[(asos_code, m["variable"])].append(m)

    regraded = 0
    for (asos_code, variable), group_markets in groups.items():
        dates = [date.fromisoformat(m["settlement_date"]) for m in group_markets]
        start = min(dates)
        end = max(dates)
        try:
            lookup = fetch_asos_daily_extreme(asos_code, start, end, client, variable)
        except httpx.HTTPError as exc:
            for m in group_markets:
                print(
                    f"regrade skipped {m['market_id']}: ASOS fetch failed: {exc}",
                    file=sys.stderr,
                )
            continue
        for m in group_markets:
            day = date.fromisoformat(m["settlement_date"])
            value = lookup.get(day)
            if value is None:
                continue
            record_outcome(conn, m["market_id"], value, regraded_at)
            _grade_predictions_for_market(conn, m["market_id"], value)
            regraded += 1

    return regraded
