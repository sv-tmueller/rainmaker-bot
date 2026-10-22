"""Reconstruction pair builder: rebuild live-shape calibration pairs from the
forecasts table (#462).

The #459 measurement proved the calibration fit consumed a narrower sigma
statistic than the live apply path (the deterministic-archive inter-model
std) and that the divergence is MATERIAL on 18 of 30 eligible cells. This
module rebuilds each persisted (run, market) pool the way the live pipeline
saw it - one row per (run, market, source, model) with the member values as a
JSON list, so the per-group composition fit_gaussian consumes is recovered
exactly (the #400/#417 method, precedent scripts/sigma_statistic_gap.py and
refit_v2_replay.py) - and reduces it to CalibrationPairs carrying the ONE
canonical statistic (mixture_sigma, floored at MIN_SIGMA_F on both sides).

Query patterns are derived from scripts/sigma_statistic_gap.py (proven;
the forecasts query is narrowed to (run_id, market_id) pairs here, #466):
predictions with dist_params joined to settled outcomes, collapsed
to the latest run per (market, UTC day) (prune.py already enforces this
physically for settled markets; the collapse is defensive), forecasts-table
pools decoded from values_json, Kalshi venue and station exclusion,
temperature only.

Lead fidelity: the forecasts table's own lead_time column (written from the
live pipeline's ForecastSample.lead_time_days) is the primary lead, NOT the
UTC-day arithmetic the #459 script used (late-evening runs read one lead
high). Rows with NULL lead_time fall back to the UTC-day derivation so
pre-#460 history is not silently dropped; the fallback is counted so a dry-run
surfaces its prevalence instead of hiding it.

Regime eligibility (#462 routing rule step 1): a reconstructed pair is
usable only if its run consumed the CURRENT sampling regime. TMAX sampling
never changed, so all TMAX pools are eligible. TMIN was retargeted to the
calendar-day low at the #426 ship date (2026-10-19); every stored TMIN pool
predates it and carries the night-period low as mu (measured ~1F cold, #404
wedge, non-stationary on cold-front evenings), so pre-retarget TMIN pools are
EXCLUDED BY A DATED CONSTANT, not blended or bias-corrected.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date
from typing import Any

import numpy as np

from rainmaker.config import KALSHI_STATIONS, MIN_SIGMA_F, STATIONS
from rainmaker.probability.calibration import CalibrationPair
from rainmaker.probability.distribution import mixture_sigma

# The #426 TMIN retarget (calendar-day low via the NWS hourly product) shipped
# 2026-10-19. Stored TMIN pools from before this date carry the night-period
# low as mu; they are excluded from reconstruction wholesale.
TMIN_RETROFIT_DATE = date(2026, 10, 19)

Pool = dict[tuple[str, str], list[float]]
# {(run_id, market_id): lead}
PoolLeads = dict[tuple[str, str], int | None]
# {(station, variable, lead): [pair, ...]}
CellPairs = dict[tuple[str, str, int], list[CalibrationPair]]

_SKIP_KEYS = (
    "rows_read",
    "rows_collapsed",
    "skipped_no_pool",
    "null_lead_utc_fallback_used",
    "skipped_bad_lead",
    "skipped_tmin_pre_retrofit",
    "skipped_unknown_or_kalshi_city",
)


def utc_day(started_at: str) -> str:
    return started_at[:10]


def lead_from_columns(lead_time: int | None, row: dict[str, Any]) -> int | None:
    """Resolve a pool's lead: the forecasts table's own lead_time column first
    (the live pipeline's ForecastSample.lead_time_days), the UTC-day
    arithmetic (settlement date minus run start date) as fallback for rows
    predating the column. Returns None when neither yields a usable lead.

    Late-evening runs read one lead HIGH under the UTC-day derivation, which
    is why the column wins whenever it is populated (#459 disclosed the
    misclassification; #460 made the column canonical).
    """
    if lead_time is not None and lead_time >= 0:
        return int(lead_time)
    sd, rd = row.get("settlement_date"), row.get("started_at")
    if sd is None or rd is None:
        return None
    try:
        lead = (date.fromisoformat(sd) - date.fromisoformat(rd[:10])).days
    except ValueError:
        return None
    return lead if lead >= 0 else None


def fetch_settled_rows(
    conn: Any, since: str | None = None
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Latest-run-per-(market, UTC day) settled temperature rows, Polymarket only.

    Lifted verbatim from scripts/sigma_statistic_gap.py's
    fetch_cells_and_history: predictions with dist_params joined to settled
    outcomes, Kalshi venue and Kalshi-only stations excluded (NCEI-blocked,
    out of scope per #462).
    """
    kalshi_icaos = {s.icao for s in KALSHI_STATIONS.values()}
    since_clause = " AND r.started_at >= ?" if since is not None else ""
    since_params = (since,) if since is not None else ()
    rows = conn.execute(
        "SELECT p.run_id AS run_id, p.market_id AS market_id, "
        "m.variable AS variable, m.city AS city, "
        "m.settlement_date AS settlement_date, "
        "r.started_at AS started_at, o.actual_value AS actual_value "
        "FROM predictions p "
        "JOIN outcomes o ON o.market_id = p.market_id "
        "JOIN markets m ON m.id = p.market_id "
        "JOIN runs r ON r.id = p.run_id "
        "WHERE p.dist_params IS NOT NULL AND o.actual_value IS NOT NULL "
        "AND m.variable != 'PRCP'"
        "AND (m.venue IS NULL OR m.venue != 'kalshi')" + since_clause,
        since_params,
    ).fetchall()
    rows = [dict(r) for r in rows]

    # Defensive collapse to the latest run per (market, UTC day): prune.py
    # already enforces this physically for settled markets, but correctness
    # must not depend on the prune having run.
    latest: dict[tuple[str, str], tuple[str, str]] = {}
    for r in rows:
        key = (r["market_id"], utc_day(r["started_at"]))
        marker = (r["started_at"], r["run_id"])
        if key not in latest or marker > latest[key]:
            latest[key] = marker
    keep = {(mid, rid) for (mid, _), (_, rid) in latest.items()}
    collapsed = [r for r in rows if (r["market_id"], r["run_id"]) in keep]

    seen: set[tuple[str, str]] = set()
    uniq: list[dict[str, Any]] = []
    for r in collapsed:
        key = (r["run_id"], r["market_id"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)

    # Drop pools whose city maps to a Kalshi-only station (NCEI-blocked) or an
    # unknown city.
    out: list[dict[str, Any]] = []
    skipped_unknown = 0
    for r in uniq:
        station = STATIONS.get(r["city"])
        if station is None or station.icao in kalshi_icaos:
            skipped_unknown += 1
            continue
        out.append(r)
    stats = {
        "rows_read": len(rows),
        "rows_collapsed": len(out),
        "skipped_unknown_or_kalshi_city": skipped_unknown,
    }
    return out, stats


def fetch_pools(
    conn: Any, run_market_ids: list[tuple[str, str]]
) -> tuple[dict[tuple[str, str], Pool], PoolLeads]:
    """Reconstruct pools and their leads from per-sample forecast rows.

    Returns ({(run_id, market_id): {(source, model): [values]}},
    {(run_id, market_id): lead_or_None}), derived from
    scripts/sigma_statistic_gap.py.fetch_pools. The query is narrowed to
    (run_id, market_id) pairs here (#466; the script uses run_id IN (...) plus
    a Python filter): chunked row-value IN queries; values_json decoded
    defensively (bad or empty JSON rows are skipped, not fatal); plus the
    lead_time column the script lacked.
    """
    pools: dict[tuple[str, str], Pool] = defaultdict(dict)
    leads: dict[tuple[str, str], int | None] = {}
    unique = list(dict.fromkeys(run_market_ids))
    # Row-value IN on (run_id, market_id): only the asked-for pools leave the
    # database, not every market of every run. 2 params per pair, so 250 pairs
    # stay at 500 params per query.
    CHUNK = 250
    for start in range(0, len(unique), CHUNK):
        chunk = unique[start : start + CHUNK]
        marks = ",".join(["(?, ?)"] * len(chunk))
        rows = conn.execute(
            f"SELECT run_id, market_id, source, model, values_json, lead_time FROM forecasts "
            f"WHERE (run_id, market_id) IN ({marks})",
            [v for pair in chunk for v in pair],
        ).fetchall()
        for r in rows:
            key = (r["run_id"], r["market_id"])
            try:
                values = json.loads(r["values_json"])
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(values, list) or not values:
                continue
            pools[key][(r["source"], r["model"])] = [float(v) for v in values]
            lt = r["lead_time"]
            prev = leads.get(key)
            # One pool spans one target, so rows agree; keep the smallest
            # non-negative value if a rogue row disagrees.
            cand = int(lt) if lt is not None and lt >= 0 else None
            if prev is None or (cand is not None and (prev is None or cand < prev)):
                leads[key] = cand
    return dict(pools), leads


def pair_from_pool(pool: Pool, actual: float) -> CalibrationPair | None:
    """Reduce one reconstructed pool to a pair under the unified statistic.

    sigma = max(mixture_sigma(groups), MIN_SIGMA_F) and ensemble_var =
    sigma**2, identically to the live apply side (#462 invariant). mu is the
    equal-weight mean of group means (the same mu both #459 arms shared).
    """
    if not pool:
        return None
    mu = float(np.mean([np.mean(v) for v in pool.values()]))
    sigma = max(mixture_sigma(pool), MIN_SIGMA_F)
    return CalibrationPair(mu=mu, sigma=sigma, ensemble_var=sigma**2, actual=actual)


def build_recon_pairs(
    conn: Any,
    *,
    since: str | None = None,
    tmin_retrofit_date: date = TMIN_RETROFIT_DATE,
) -> tuple[CellPairs, dict[str, int]]:
    """Build reconstructed CalibrationPairs per (station, variable, lead).

    Returns ({(station, variable, lead): [pairs]}, skip_counts). Skip counts
    are the containment instrument: a dry-run prints them so surprises
    (widespread NULL lead_time, pool decode gaps) surface instead of silently
    shrinking the population.
    """
    rows, stats = fetch_settled_rows(conn, since)
    pools, leads = fetch_pools(conn, [(r["run_id"], r["market_id"]) for r in rows])

    counts: dict[str, int] = {k: 0 for k in _SKIP_KEYS}
    counts["rows_read"] = stats["rows_read"]
    counts["rows_collapsed"] = stats["rows_collapsed"]
    counts["skipped_unknown_or_kalshi_city"] = stats["skipped_unknown_or_kalshi_city"]

    by_cell: CellPairs = defaultdict(list)
    for r in rows:
        key = (r["run_id"], r["market_id"])
        pool = pools.get(key)
        if not pool:
            counts["skipped_no_pool"] += 1
            continue
        lead = leads.get(key)
        if lead is None:
            lead = lead_from_columns(None, r)
            if lead is not None:
                counts["null_lead_utc_fallback_used"] += 1
        if lead is None:
            counts["skipped_bad_lead"] += 1
            continue
        if (
            r["variable"] == "TMIN"
            and date.fromisoformat(utc_day(r["started_at"])) < tmin_retrofit_date
        ):
            counts["skipped_tmin_pre_retrofit"] += 1
            continue
        pair = pair_from_pool(pool, float(r["actual_value"]))
        if pair is None:  # pragma: no cover - guarded by the pool check above
            counts["skipped_no_pool"] += 1
            continue
        by_cell[(STATIONS[r["city"]].icao, r["variable"], lead)].append(pair)
    return dict(by_cell), counts


def routing_table(pairs: CellPairs, *, min_cal_samples: int) -> list[dict[str, Any]]:
    """Per (station, variable, lead): recon-only vs archive-fallback routing.

    A cell routes to recon iff it exists in the reconstructed pairs AND has
    n >= min_cal_samples; everything else rides the archive fallback
    (fetch_historical_lead_forecasts re-expressed under the unified
    statistic). Sorted for stable rendering.
    """
    table = []
    for (station, variable, lead), plist in sorted(pairs.items()):
        table.append(
            {
                "station": station,
                "variable": variable,
                "lead": lead,
                "n_recon": len(plist),
                "route": "recon" if len(plist) >= min_cal_samples else "archive",
            }
        )
    return table
