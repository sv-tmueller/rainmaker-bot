#!/usr/bin/env python3
"""Throwaway analysis script for Issue #373: Hot-city TMAX calibration audit.

Computes the cold bias in TMAX forecasts for Los Angeles, San Francisco,
Dallas, and Houston by comparing forecast means (mu from dist_params) against
NOAA actuals over the settled-history window.

Works against any DATABASE_URL (Supabase Postgres in production, local SQLite
for development). The local SQLite DB has minimal data (Aug 2026, lead 0 only);
the full audit requires a production run with DATABASE_URL set.

Usage:
    # Local SQLite (demo):
    python scripts/tmax_bias_audit.py

    # Production (requires DATABASE_URL):
    DATABASE_URL="postgresql://..." python scripts/tmax_bias_audit.py

Outputs:
    - Per (city, lead) bias table (forecast mean minus actual mean)
    - Consistency analysis (per-date errors to detect structural vs episodic)
    - Per-city recommendation: gate, tighten, or refit
"""

from __future__ import annotations

import json
import os
import statistics
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rainmaker.config import STATION_EDGE_DELTA, STATION_POLICIES, STATIONS
from rainmaker.store.db import connect, init_schema

AUDIT_CITIES = ["Los Angeles", "San Francisco", "Dallas", "Houston"]
TARGET_VARIABLE = "TMAX"
THRESHOLD_GATE_BIAS_F = 3.0  # deg F: bias above this suggests gating/exclusion
THRESHOLD_TIGHTEN_BIAS_F = 1.5  # deg F: bias above this suggests tightening


def get_db_conn():
    """Connect to the store."""
    dsn = os.environ.get("DATABASE_URL", "rainmaker.db")
    conn = connect(dsn)
    init_schema(conn)
    return conn


def fetch_settled_forecast_vs_actual(conn):
    """Fetch one row per (market, run) with dist_params and actual, deduplicated.

    Mirrors compute_live_accuracy's query: DISTINCT collapses per-bucket
    prediction rows (which share one dist_params) to one row per (run, market).
    """
    rows = conn.execute(
        "SELECT DISTINCT p.run_id AS run_id, p.market_id AS market_id, "
        "p.dist_params AS dist_params, m.city AS city, m.variable AS variable, "
        "m.venue AS venue, m.settlement_date AS settlement_date, "
        "r.started_at AS started_at, o.actual_value AS actual_value "
        "FROM predictions p "
        "JOIN outcomes o ON o.market_id = p.market_id "
        "JOIN markets m ON m.id = p.market_id "
        "JOIN runs r ON r.id = p.run_id "
        "WHERE p.dist_params IS NOT NULL "
        "AND o.actual_value IS NOT NULL "
        "AND m.variable = ?",
        (TARGET_VARIABLE,),
    ).fetchall()
    return [dict(r) for r in rows]


def _latest_run_per_market_day(rows):
    """Deduplicate to the latest run per (market, UTC day).

    Matches tracking.py's _latest_run_per_market_day semantics.
    """
    best: dict[tuple[str, str], dict] = {}
    for r in rows:
        key = (r["market_id"], r["started_at"][:10])
        if key not in best or r["started_at"] > best[key]["started_at"]:
            best[key] = r
    return list(best.values())


def compute_lead(run_started_at: str, settlement_date: str) -> int:
    """Compute lead time in days from run started_at to settlement_date."""
    try:
        run_date = date.fromisoformat(run_started_at[:10])
        settle_date = date.fromisoformat(settlement_date)
        lead = (settle_date - run_date).days
        return max(lead, 0)
    except (ValueError, TypeError):
        return -1


def analyze_bias(rows):
    """Group rows by (city, lead) and compute bias statistics.

    Returns a dict keyed by (city, lead) with:
      - n: sample count
      - mean_mu: average forecast mean (deg F)
      - mean_actual: average actual (deg F)
      - mean_bias: mean(mu - actual), positive = forecast too warm, negative = too cold
      - median_bias: median(mu - actual)
      - std_error: std dev of (mu - actual)
      - pct_signed_positive: fraction where mu > actual (too warm)
      - per_date_errors: list of (date, mu, actual, error) for consistency analysis
    """
    groups: dict[tuple[str, int], list[dict]] = defaultdict(list)

    for r in rows:
        city = r["city"]
        if city not in AUDIT_CITIES:
            continue
        lead = compute_lead(r["started_at"], r["settlement_date"])
        if lead < 0:
            continue
        try:
            params = json.loads(r["dist_params"])
        except (json.JSONDecodeError, TypeError):
            continue
        mu = params.get("mu")
        if mu is None or not isinstance(mu, (int, float)):
            continue
        actual = r["actual_value"]
        if actual is None:
            continue

        groups[(city, lead)].append(
            {
                "date": r["settlement_date"],
                "mu": float(mu),
                "actual": float(actual),
                "error": float(mu) - float(actual),
            }
        )

    results = {}
    for (city, lead), entries in sorted(groups.items()):
        errors = [e["error"] for e in entries]
        mus = [e["mu"] for e in entries]
        actuals = [e["actual"] for e in entries]

        results[(city, lead)] = {
            "city": city,
            "lead": lead,
            "n": len(entries),
            "mean_mu": statistics.mean(mus),
            "mean_actual": statistics.mean(actuals),
            "mean_bias": statistics.mean(errors),
            "median_bias": statistics.median(errors),
            "std_error": statistics.stdev(errors) if len(errors) > 1 else 0.0,
            "pct_signed_positive": sum(1 for e in errors if e > 0) / len(errors),
            "per_date_errors": sorted(entries, key=lambda x: x["date"]),
        }

    return results


def classify_consistency(per_date_errors):
    """Classify whether the bias is structural (consistent) or episodic (clustered).

    Heuristic:
    - Structural: signs are consistent (>70% same sign), std_error < abs(mean_bias)
    - Episodic: mixed signs or high variance relative to mean
    """
    if len(per_date_errors) < 2:
        return "insufficient_data"

    errors = [e["error"] for e in per_date_errors]
    mean_bias = statistics.mean(errors)
    std_err = statistics.stdev(errors) if len(errors) > 1 else 0.0

    positive_count = sum(1 for e in errors if e > 0)
    negative_count = sum(1 for e in errors if e < 0)
    dominant_sign_pct = max(positive_count, negative_count) / len(errors)

    # Structural: consistent sign, relatively low dispersion
    if dominant_sign_pct >= 0.70 and std_err < abs(mean_bias) * 1.5:
        return "structural"
    elif dominant_sign_pct >= 0.60:
        return "mostly_structural"
    else:
        return "episodic"


def recommend(result, consistency):
    """Generate per-city recommendation: gate, tighten, or refit.

    Logic:
    - If bias is strongly cold (< -3 deg F) and structural -> gate (exclude)
    - If bias is moderately cold (-1.5 to -3 deg F) and structural -> tighten
    - If bias is moderate and episodic -> refit (calibration adjustment)
    - If bias is small (< 1.5 deg F) -> monitor (no action needed)
    """
    mean_bias = result["mean_bias"]
    n = result["n"]

    if n < 5:
        return "monitor", "Insufficient data (<5 samples) for confident recommendation."

    abs_bias = abs(mean_bias)

    # Positive bias = too warm (engine predicts warmer than actual)
    # Negative bias = too cold (engine predicts cooler than actual) -- the reported problem
    is_cold_bias = mean_bias < 0

    if abs_bias >= THRESHOLD_GATE_BIAS_F and consistency in ("structural", "mostly_structural"):
        if is_cold_bias:
            return (
                "gate",
                f"Cold bias of {mean_bias:.1f}°F is ≥{THRESHOLD_GATE_BIAS_F}°F and {consistency}. "
                f"The forecast mean is systematically low; the engine bets the high stays "
                f"below a bucket, then the actual realizes above it.",
            )
        else:
            return (
                "gate",
                f"Warm bias of {mean_bias:+.1f}°F is ≥{THRESHOLD_GATE_BIAS_F}°F and {consistency}. "
                f"Symmetric concern: forecast mean is systematically high.",
            )

    if abs_bias >= THRESHOLD_TIGHTEN_BIAS_F and consistency in ("structural", "mostly_structural"):
        if is_cold_bias:
            msg = (
                f"Cold bias of {mean_bias:.1f}F is >= {THRESHOLD_TIGHTEN_BIAS_F}F "
                f"and {consistency}. Raise the edge-floor delta "
                f"(STATION_EDGE_DELTA) to reduce exposure until a calibration "
                f"refit can address the root cause."
            )
        else:
            msg = (
                f"Warm bias of {mean_bias:+.1f}F is >= {THRESHOLD_TIGHTEN_BIAS_F}F "
                f"and {consistency}. Raise the edge-floor delta to reduce exposure."
            )
        return ("tighten", msg)

    if abs_bias >= THRESHOLD_TIGHTEN_BIAS_F and consistency == "episodic":
        return (
            "refit",
            f"Bias of {mean_bias:+.1f}°F is ≥{THRESHOLD_TIGHTEN_BIAS_F}°F but episodic. "
            f"A calibration refit (adjusting the bias parameter in the calibration cell) "
            f"is the appropriate remedy. The calibration cell is keyed by "
            f"(station_ICAO, 'TMAX', lead_time) in the calibration table; the bias column "
            f"is subtracted from mu in apply_calibration, so increasing bias reduces the "
            f"predictive mean.",
        )

    return (
        "monitor",
        f"Bias of {mean_bias:+.1f}°F is below the {THRESHOLD_TIGHTEN_BIAS_F}°F threshold. "
        f"No action needed at this time; continue monitoring.",
    )


def identify_refit_cell(city, lead):
    """Identify the specific calibration parameter and cell for a refit recommendation.

    The calibration table is keyed by (station, variable, lead_time).
    The bias column is the EMOS bias term subtracted from mu.
    Increasing the stored bias shifts the predictive mean DOWN (cooler).
    Decreasing the stored bias shifts the predictive mean UP (warmer).

    For a cold bias (forecast mean < actual), the engine needs the predictive
    mean shifted UP, meaning the stored bias should be DECREASED (or made more
    negative if currently zero/positive).
    """
    station = STATIONS.get(city)
    if station is None:
        return "Unknown station"

    icao = station.icao
    return (
        f"Calibration cell: (station='{icao}', variable='TMAX', lead_time={lead}). "
        f"Parameter: bias (column 'bias' in the calibration table). "
        f"Fitted by fit_calibration in backfill.py via run_backfill. "
        f"To correct a cold bias, decrease the stored bias value so that "
        f"apply_calibration computes mu - bias closer to the actual. "
        f"Refit via: uv run rainmaker backfill --city '{city}'."
    )


def main():
    conn = get_db_conn()

    print("=" * 80)
    print("ISSUE #373: HOT-CITY TMAX CALIBRATION AUDIT")
    print("=" * 80)
    print()

    # Check data availability
    raw_rows = fetch_settled_forecast_vs_actual(conn)
    deduped = _latest_run_per_market_day(raw_rows)

    print(f"Total settled TMAX rows (deduped): {len(deduped)}")

    # Show data coverage per city
    city_counts = defaultdict(int)
    for r in deduped:
        if r["city"] in AUDIT_CITIES:
            city_counts[r["city"]] += 1
    print("\nData coverage:")
    for city in AUDIT_CITIES:
        print(f"  {city}: {city_counts[city]} settled rows")
    print()

    # Compute bias per (city, lead)
    results = analyze_bias(deduped)

    print("=" * 80)
    print("BIAS TABLE: FORECAST MEAN MINUS ACTUAL MEAN PER (CITY, LEAD)")
    print("=" * 80)
    print()

    header = (
        f"{'City':<16} {'Lead':>4} {'N':>4} {'Mean mu':>8} "
        f"{'Mean Actual':>12} {'Bias':>8} {'Med Bias':>9} "
        f"{'StdErr':>8} {'%Warm':>6}"
    )
    print(header)
    print("-" * len(header))

    for (city, lead), r in results.items():
        print(
            f"{city:<16} {lead:>4} {r['n']:>4} "
            f"{r['mean_mu']:>8.2f} {r['mean_actual']:>12.2f} "
            f"{r['mean_bias']:>+8.2f} {r['median_bias']:>+9.2f} "
            f"{r['std_error']:>8.2f} {r['pct_signed_positive'] * 100:>5.1f}%"
        )

    # Cities with no data
    cities_with_data = {key[0] for key in results}
    for city in AUDIT_CITIES:
        if city not in cities_with_data:
            print(f"{city:<16} ---- No settled TMAX data found")

    print()
    print("=" * 80)
    print("CONSISTENCY ANALYSIS: STRUCTURAL vs EPISODIC")
    print("=" * 80)
    print()

    consistency_results = {}
    for (city, lead), r in results.items():
        consistency = classify_consistency(r["per_date_errors"])
        consistency_results[(city, lead)] = consistency
        print(f"{city} (lead {lead}): {consistency}")
        print("  Per-date errors:")
        for e in r["per_date_errors"]:
            sign = "+" if e["error"] > 0 else ""
            print(
                f"    {e['date']}  mu={e['mu']:.2f}"
                f"  actual={e['actual']:.2f}"
                f"  err={sign}{e['error']:.2f}"
            )
        print()

    print("=" * 80)
    print("PER-CITY RECOMMENDATIONS")
    print("=" * 80)
    print()

    # Group by city for per-city recommendation (aggregate across leads)
    city_results = defaultdict(list)
    for (city, lead), r in results.items():
        city_results[city].append(((city, lead), r))

    recommendations = {}

    for city in AUDIT_CITIES:
        city_leads = city_results.get(city, [])
        if not city_leads:
            print(f"{city}: No data — MONITOR (production run needed)")
            recommendations[city] = ("monitor", "No data available in local DB.")
            print()
            continue

        # Aggregate: use the largest-n lead as representative
        best_key, best_result = max(city_leads, key=lambda x: x[1]["n"])
        best_consistency = consistency_results[best_key]

        action, rationale = recommend(best_result, best_consistency)
        recommendations[city] = (action, rationale)

        print(f"{city}: {action.upper()}")
        print(f"  Lead analyzed: {best_result['lead']} (n={best_result['n']})")
        print(f"  Mean bias: {best_result['mean_bias']:+.2f}°F")
        print(f"  Median bias: {best_result['median_bias']:+.2f}°F")
        print(f"  Std error: {best_result['std_error']:.2f}°F")
        print(f"  % too warm: {best_result['pct_signed_positive'] * 100:.1f}%")
        print(f"  Consistency: {best_consistency}")
        print(f"  Rationale: {rationale}")

        if action == "refit":
            print(f"  Refit target: {identify_refit_cell(city, best_result['lead'])}")

        # Current policy status
        station = STATIONS.get(city)
        if station:
            icao = station.icao
            if icao in STATION_POLICIES:
                print(f"  Current policy: EXCLUDED (STATION_POLICIES[{icao}])")
            elif (icao, TARGET_VARIABLE) in STATION_EDGE_DELTA:
                delta = STATION_EDGE_DELTA[(icao, TARGET_VARIABLE)]
                print(
                    f"  Current policy: EDGE DELTA +{delta:.2f} (STATION_EDGE_DELTA[{icao}, TMAX])"
                )
            else:
                print("  Current policy: NONE (no exclusion or edge delta)")

        print()

    # Summary table
    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print()
    print(f"{'City':<16} {'Action':>10} {'Bias °F':>8} {'Consistency':>16} {'N':>4}")
    print("-" * 60)
    for city in AUDIT_CITIES:
        action, _ = recommendations[city]
        city_leads = city_results.get(city, [])
        if city_leads:
            _, best_r = max(city_leads, key=lambda x: x[1]["n"])
            cons = consistency_results.get((city, best_r["lead"]), "?")
            print(
                f"{city:<16} {action:>10} {best_r['mean_bias']:>+8.2f} {cons:>16} {best_r['n']:>4}"
            )
        else:
            print(f"{city:<16} {action:>10} {'N/A':>8} {'N/A':>16} {0:>4}")

    print()
    print("NOTE: Local SQLite DB has limited data (Aug 2026, lead 0 only).")
    print("      Full audit requires a production run with DATABASE_URL set.")

    conn.close()


if __name__ == "__main__":
    main()
