"""Measure the fit-vs-apply sigma statistic gap per (station, variable, lead).

Issue #459 (implements #435). Method: reconstruct historical pools from the
forecasts table (per-sample rows; the #400/#417 method, precedent
scripts/refit_v2_replay.py), pair each reconstructed pool with its settled
actual, and refit each calibration cell TWICE:

  - ARM "fit"   : ensemble_var from the deterministic-archive-style statistic
                  (pooled std across ALL individual samples, the statistic
                  backfill.py's archive path supplies to fit_gaussian's
                  fallback arm, hence to the EMOS var_b fit).
  - ARM "apply" : ensemble_var from the LIVE extended-sigma mixture
                  (fit_gaussian's #426 arm: equal-weight mixture variance
                  across all (source, model) groups, det groups as
                  singletons), floored at MIN_SIGMA_F.

Both arms use the SAME mu (per-group-mean mean, identical in both
definitions) and the SAME actuals, so the only difference fed into the EMOS
objective is the sigma statistic. Per cell we report delta var_b =
var_b_apply - var_b_fit, and the predictive-sigma ordering check at the
cell's median ensemble_var.

Decision rule (pre-registered in #459): immaterial iff abs(delta var_b) <
0.05 on every cell with n >= 40 AND no directional flip in predictive sigma
ordering (sign of var_b flips, or the ordering of predictive sigma between
the two arms inverts at representative pool sizes).

Temperature only (Kalshi stations excluded: NCEI-blocked; their cells are
also excluded from the comparison). Read-only SELECTs; DSN never echoed.

Usage:
    uv run python scripts/sigma_statistic_gap.py \
        [--dsn-file PATH] [--since YYYY-MM-DD] [--output FILE]
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rainmaker.config import (  # noqa: E402
    CALIBRATION_FAMILY,
    KALSHI_STATIONS,
    MIN_SIGMA_F,
    STATIONS,
)
from rainmaker.probability.calibration import (  # noqa: E402
    CalibrationPair,
    fit_calibration,
    fit_student_t_free_df,
)
from rainmaker.store.db import connect  # noqa: E402

DECISION_ABS_DVAR_B = 0.05  # pre-registered immateriality threshold (#459)
DECISION_MIN_N = 40  # cells below this n are exempt from the threshold


def archive_style_sigma(
    groups: dict[tuple[str, str], list[float]], *, openmeteo_only: bool = False
) -> float | None:
    """The fit-arm statistic: std across the deterministic models' values.

    The backfill archive (Previous Runs API) carries one scalar per
    deterministic Open-Meteo model and no ensemble members, so the statistic
    the cells were fitted on is the inter-model std over those scalars. On a
    reconstructed live pool the deterministic models appear as singleton
    groups (len(values) == 1, the same ensemble/det classification
    refit_v2_replay uses), so this is the faithful proxy. Returns None when
    fewer than two deterministic groups are present (no spread to estimate).

    openmeteo_only restricts the det set to Open-Meteo models, excluding the
    NWS singleton: the archive itself has no NWS counterpart, so this is the
    stricter reading of "archive-like". Sensitivity arm only (--fit-arm
    openmeteo); the default includes NWS (the live pool's det family is
    whatever singleton groups it carries, and NWS is one more independent
    deterministic model in the same spirit as the archive's models).
    """
    if openmeteo_only:
        det_values = [
            vals[0] for (src, _mdl), vals in groups.items() if len(vals) == 1 and src == "open-meteo"
        ]
    else:
        det_values = [vals[0] for vals in groups.values() if len(vals) == 1]
    if len(det_values) < 2:
        return None
    return float(statistics.stdev(det_values))


def extended_mixture_sigma(groups: dict[tuple[str, str], list[float]]) -> float:
    """The apply-arm statistic: the #426 all-groups mixture variance.

    Equal weight per (source, model) group; a group's component variance is
    its within-group sample variance (0 for singletons); sigma^2 is the mean
    over groups of (component variance + squared distance of the group mean
    from the grand mean of group means). Verbatim from fit_gaussian's
    ensemble arm (probability/distribution.py).
    """
    means = np.array([float(np.mean(v)) for v in groups.values()], dtype=float)
    variances = np.array(
        [float(np.var(v, ddof=1)) if len(v) >= 2 else 0.0 for v in groups.values()],
        dtype=float,
    )
    m_bar = means.mean()
    return float(np.sqrt(np.mean(variances + (means - m_bar) ** 2)))


def grouped_mu(groups: dict[tuple[str, str], list[float]]) -> float:
    """Equal weight per group mean; identical in both sigma definitions."""
    return float(np.mean([np.mean(v) for v in groups.values()]))


def fetch_cells_and_history(conn, since: str | None) -> tuple[list[dict], list[dict]]:
    """Stored calibration cells + one row per (run, market) settled pool.

    Pool population mirrors refit_v2_replay.fetch_replay_rows: predictions
    with dist_params joined to settled outcomes, collapsed to the latest run
    per (market, UTC day), one row per (run, market). Temperature only,
    Polymarket venue only (Kalshi stations are NCEI-blocked and excluded by
    the issue's non-goals).
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
    latest: dict[tuple[str, str], tuple[str, str]] = {}
    for r in rows:
        key = (r["market_id"], r["started_at"][:10])
        marker = (r["started_at"], r["run_id"])
        if key not in latest or marker > latest[key]:
            latest[key] = marker
    keep = {(mid, rid) for (mid, _), (_, rid) in latest.items()}
    rows = [r for r in rows if (r["market_id"], r["run_id"]) in keep]
    seen: set[tuple[str, str]] = set()
    uniq: list[dict] = []
    for r in rows:
        key = (r["run_id"], r["market_id"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)
    # Drop pools whose city maps to a Kalshi-only station (NCEI-blocked).
    uniq = [
        r
        for r in uniq
        if STATIONS.get(r["city"]) is not None and STATIONS[r["city"]].icao not in kalshi_icaos
    ]
    cells = [
        dict(r)
        for r in conn.execute(
            "SELECT station, variable, lead_time, bias, var_a, var_b, df, n_samples"
            " FROM calibration"
        ).fetchall()
    ]
    cells = [c for c in cells if c["station"] in {s.icao for s in STATIONS.values()}]
    return cells, uniq


def fetch_pools(conn, run_market_ids: list[tuple[str, str]]) -> dict:
    """{(run_id, market_id): {(source, model): [values]}} from per-sample rows.

    The forecasts table stores one row per (run, market, source, model) with
    the member values as a JSON list (multi-value = ensemble member set,
    single-value = deterministic model), so the per-group composition the
    live fit_gaussian consumes is reconstructible exactly (#400/#417 method).
    """
    pools: dict[tuple[str, str], dict[tuple[str, str], list[float]]] = defaultdict(dict)
    unique = list(dict.fromkeys(run_market_ids))
    CHUNK = 500
    for start in range(0, len(unique), CHUNK):
        chunk = unique[start : start + CHUNK]
        run_ids = list(dict.fromkeys(rid for rid, _ in chunk))
        marks_r = ",".join("?" * len(run_ids))
        rows = conn.execute(
            f"SELECT run_id, market_id, source, model, values_json FROM forecasts "
            f"WHERE run_id IN ({marks_r})",
            run_ids,
        ).fetchall()
        wanted = set(chunk)
        for r in rows:
            key = (r["run_id"], r["market_id"])
            if key not in wanted:
                continue
            try:
                values = json.loads(r["values_json"])
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(values, list) or not values:
                continue
            pools[key][(r["source"], r["model"])] = [float(v) for v in values]
    return pools


def lead_of(row: dict) -> int | None:
    """UTC-days between the run's start date and the settlement date."""
    sd, rd = row["settlement_date"], row["started_at"][:10]
    if sd is None or rd is None:
        return None
    try:
        from datetime import date

        lead = (date.fromisoformat(sd) - date.fromisoformat(rd)).days
    except ValueError:
        return None
    return lead if lead >= 0 else None


def fit_arm(
    station: str,
    variable: str,
    lead: int,
    pairs: list[CalibrationPair],
) -> dict[str, Any]:
    fitter = (
        fit_student_t_free_df
        if CALIBRATION_FAMILY.get(variable) == "student_t"
        else fit_calibration
    )
    cal = fitter(station, variable, lead, pairs)
    return {
        "bias": cal.bias,
        "var_a": cal.var_a,
        "var_b": cal.var_b,
        "df": cal.df,
        "n": cal.n_samples,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dsn-file", default="/tmp/batch458/dsn.env")
    ap.add_argument("--since", default="2026-07-18")
    ap.add_argument("--output", default="scripts/sigma_statistic_gap_results.json")
    ap.add_argument(
        "--fit-arm",
        choices=("all", "openmeteo"),
        default="all",
        help="which singleton groups count as the archive-style det set"
        " (openmeteo excludes the NWS singleton; sensitivity arm)",
    )
    args = ap.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn and Path(args.dsn_file).exists():
        dsn = Path(args.dsn_file).read_text().strip()
    if not dsn:
        print("DATABASE_URL not set and no --dsn-file readable; aborting", file=sys.stderr)
        return 2
    conn = connect(dsn)  # never echo the DSN
    try:
        cells, rows = fetch_cells_and_history(conn, args.since)
        print(f"pool rows: {len(rows)}; stored cells (non-Kalshi): {len(cells)}")
        pools = fetch_pools(conn, [(r["run_id"], r["market_id"]) for r in rows])
        print(f"sample pools found: {len(pools)}")

        by_cell: dict[tuple[str, str, int], list[dict]] = defaultdict(list)
        skipped_no_pool = 0
        skipped_no_det = 0
        for r in rows:
            key = (r["run_id"], r["market_id"])
            pool = pools.get(key)
            lead = lead_of(r)
            if not pool or lead is None:
                skipped_no_pool += 1
                continue
            icao = STATIONS[r["city"]].icao
            groups = pool
            sigma_fit = archive_style_sigma(groups, openmeteo_only=args.fit_arm == "openmeteo")
            if sigma_fit is None:
                skipped_no_det += 1
                continue
            mu = grouped_mu(groups)
            sigma_apply = extended_mixture_sigma(groups)
            by_cell[(icao, r["variable"], lead)].append(
                {
                    "mu": mu,
                    "ev_fit": max(sigma_fit, MIN_SIGMA_F) ** 2,
                    "ev_apply": max(sigma_apply, MIN_SIGMA_F) ** 2,
                    "actual": float(r["actual_value"]),
                }
            )
        print(
            f"pools bucketed into {len(by_cell)} cells;"
            f" skipped(no pool/lead): {skipped_no_pool}, skipped(<2 det groups): {skipped_no_det}"
        )

        results: list[dict[str, Any]] = []
        for (icao, variable, lead), obs in sorted(by_cell.items()):
            pairs_fit = [
                CalibrationPair(
                    mu=o["mu"], sigma=o["ev_fit"] ** 0.5, ensemble_var=o["ev_fit"], actual=o["actual"]
                )
                for o in obs
            ]
            pairs_apply = [
                CalibrationPair(
                    mu=o["mu"],
                    sigma=o["ev_apply"] ** 0.5,
                    ensemble_var=o["ev_apply"],
                    actual=o["actual"],
                )
                for o in obs
            ]
            fit = fit_arm(icao, variable, lead, pairs_fit)
            apply_ = fit_arm(icao, variable, lead, pairs_apply)
            med_ev = float(np.median([o["ev_fit"] for o in obs]))
            med_ev_ap = float(np.median([o["ev_apply"] for o in obs]))

            def pred_sigma(arm: dict, ev: float) -> float:
                if arm["var_a"] is None or arm["var_b"] is None:
                    return float("nan")
                return float(np.sqrt(arm["var_a"] + arm["var_b"] * ev))

            d_var_b = apply_["var_b"] - fit["var_b"]
            # Directional flip: var_b crosses zero, or the predictive-sigma
            # ordering between the arms inverts anywhere on the observed
            # ensemble_var range (checked at each arm's median and both ends).
            ev_lo = float(min(min(o["ev_fit"], o["ev_apply"]) for o in obs))
            ev_hi = float(max(max(o["ev_fit"], o["ev_apply"]) for o in obs))
            order_points = [med_ev, med_ev_ap, ev_lo, ev_hi]
            comparisons = [pred_sigma(fit, ev) - pred_sigma(apply_, ev) for ev in order_points]
            comparisons = [c for c in comparisons if c == c]  # drop NaN
            flip_order = bool(comparisons) and (min(comparisons) * max(comparisons) < 0)
            results.append(
                {
                    "station": icao,
                    "variable": variable,
                    "lead": lead,
                    "n": fit["n"],
                    "median_ev_fit": med_ev,
                    "median_ev_apply": med_ev_ap,
                    "fit": fit,
                    "apply": apply_,
                    "delta_var_b": d_var_b,
                    "flip_var_b_sign": (fit["var_b"] > 0) != (apply_["var_b"] > 0),
                    "flip_predictive_order": flip_order,
                    "pred_sigma_fit_at_med": pred_sigma(fit, med_ev),
                    "pred_sigma_apply_at_med": pred_sigma(apply_, med_ev_ap),
                }
            )

        eligible = [r for r in results if r["n"] >= DECISION_MIN_N]
        breaches = [
            r
            for r in eligible
            if abs(r["delta_var_b"]) >= DECISION_ABS_DVAR_B
            or r["flip_var_b_sign"]
            or r["flip_predictive_order"]
        ]
        verdict = "IMMATERIAL" if not breaches else "MATERIAL"

        payload = {
            "since": args.since,
            "fit_arm": args.fit_arm,
            "decision_rule": {
                "abs_delta_var_b_lt": DECISION_ABS_DVAR_B,
                "min_n": DECISION_MIN_N,
                "no_directional_flip": True,
            },
            "cells_total": len(results),
            "cells_eligible": len(eligible),
            "breaches": [
                f"{r['station']}|{r['variable']}|{r['lead']}" for r in breaches
            ],
            "verdict": verdict,
            "cells": results,
        }
        Path(args.output).write_text(json.dumps(payload, indent=2))

        hdr = (
            f"{'station':<6} {'var':<5} {'lead':>4} {'n':>4} "
            f"{'vb_fit':>7} {'vb_apply':>8} {'d_vb':>7} {'va_fit':>7} {'va_apply':>8}"
            f" {'sig_fit':>7} {'sig_apply':>8} {'flips':>6}"
        )
        print(hdr)
        print("-" * len(hdr))
        for r in results:
            fl = "YES" if (r["flip_var_b_sign"] or r["flip_predictive_order"]) else ""
            star = "*" if r in breaches else ""
            print(
                f"{r['station']:<6} {r['variable']:<5} {r['lead']:>4} {r['n']:>4} "
                f"{r['fit']['var_b']:>7.3f} {r['apply']['var_b']:>8.3f} {r['delta_var_b']:>+7.3f} "
                f"{r['fit']['var_a']:>7.3f} {r['apply']['var_a']:>8.3f}"
                f" {r['pred_sigma_fit_at_med']:>7.2f} {r['pred_sigma_apply_at_med']:>8.2f}"
                f" {fl:>6}{star}"
            )
        print(f"\neligible cells (n >= {DECISION_MIN_N}): {len(eligible)}, breaches: {len(breaches)}")
        print(f"VERDICT: {verdict}")
        print(f"results written to {args.output}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
