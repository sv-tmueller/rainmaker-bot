"""Replay script for #426: extended sigma + TMIN calendar-day retarget, before/after.

Reads the persisted history (forecasts table sample groups + settled actuals)
from the datastore named by DATABASE_URL (read-only SELECTs), rebuilds each
(run, market) pool's raw Gaussian under the OLD sigma definition (ens-only
mixture) and the NEW one (all-groups mixture, the #400/#426 extension), then
scores both against the stored calibration cells: per-(variable, lead) Brier
over the market's own buckets and PIT tail ratios (P(PIT>1-q)/q, P(PIT<q)/q).

It does NOT refit cells: the point is the honest live-sigma before/after on the
same history. The refit itself is `uv run rainmaker backfill --city all`
(operator step; see the runbook). Precedent: compare_asos_vs_wrh.py /
tmax_bias_audit.py (throwaway analysis tools under scripts/).

TMIN retarget note: the forecasts table stores the NIGHT-PERIOD low for
historical NWS rows (the pre-#426 window), so the replay's TMIN "after" arm
shows the sigma-extension effect only; the calendar-day alignment is proven by
the backfill path (whose pairs always used the local calendar-day min) and by
the wedge measurement in the #404 appendix. The stale-bias purge is the refit's
own contribution.

DSN handling: DATABASE_URL must be set externally (never echoed). The script
reads it from the environment or from a file passed via --dsn-file, without
printing its value.

Usage:
    DATABASE_URL="postgresql://..." uv run python scripts/refit_v2_replay.py \
        [--dsn-file PATH] [--since YYYY-MM-DD] [--output FILE]

Queries are read-only SELECTs; nothing is written to the datastore.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
from scipy.stats import norm
from scipy.stats import t as student_t

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rainmaker.config import (  # noqa: E402
    MIN_CAL_BIAS_SAMPLES,
    MIN_CAL_SAMPLES,
    MIN_SIGMA_F,
    UNCALIBRATED_WIDEN,
)
from rainmaker.probability.calibration import std_cdf_for  # noqa: E402
from rainmaker.probability.outcomes import settles  # noqa: E402
from rainmaker.store.db import connect  # noqa: E402

# ----------------------------------------------------------------------------
# Old sigma definition (pre-#426): equal-weight mixture over ENSEMBLE groups
# only; deterministic groups contribute nothing. Kept here verbatim from the
# pre-extension fit_gaussian so the replay is self-contained and immune to
# later edits of the live fit.
# ----------------------------------------------------------------------------


def _groups(values: list[tuple[str, str, float]]) -> dict[tuple[str, str], list[float]]:
    out: dict[tuple[str, str], list[float]] = defaultdict(list)
    for source, model, v in values:
        out[(source, model)].append(v)
    return out


def _mixture_sigma(means: list[float], variances: list[float]) -> float:
    arr_m = np.array(means, dtype=float)
    arr_v = np.array(variances, dtype=float)
    m_bar = arr_m.mean()
    return float(np.sqrt(np.mean(arr_v + (arr_m - m_bar) ** 2)))


def old_sigma(groups: dict[tuple[str, str], list[float]], ens_keys: set) -> float:
    """Pre-#426 ensemble arm: mixture over ens groups only (0.0 when none)."""
    ens_means, ens_vars = [], []
    for key, vals in groups.items():
        if key not in ens_keys:
            continue
        ens_means.append(float(np.mean(vals)))
        ens_vars.append(float(np.var(vals, ddof=1)) if len(vals) >= 2 else 0.0)
    if not ens_means:
        return 0.0
    return _mixture_sigma(ens_means, ens_vars)


def new_sigma(groups: dict[tuple[str, str], list[float]]) -> float:
    """#426 extended arm: mixture over ALL groups (det groups as singletons)."""
    means = [float(np.mean(vals)) for vals in groups.values()]
    variances = [float(np.var(vals, ddof=1)) if len(vals) >= 2 else 0.0 for vals in groups.values()]
    return _mixture_sigma(means, variances)


def pooled_mu(groups: dict[tuple[str, str], list[float]]) -> float:
    return float(np.mean([np.mean(vals) for vals in groups.values()]))


# ----------------------------------------------------------------------------
# Store reads (read-only)
# ----------------------------------------------------------------------------


def fetch_replay_rows(conn, since: str | None) -> list[dict[str, Any]]:
    """One row per (run, market) with settled actual, buckets, and sample pool.

    Population mirrors compute_tail_calibration's PIT arm: predictions with
    dist_params joined to outcomes, deduped to the latest run per
    (market, UTC day), temperature only. The sample pool is rebuilt from the
    forecasts table (the live samples that fed the run), not from dist_params,
    because the sigma definitions under comparison consume the pool.
    """
    since_clause = " AND r.started_at >= ?" if since is not None else ""
    since_params = (since,) if since is not None else ()
    rows = conn.execute(
        "SELECT p.run_id AS run_id, p.market_id AS market_id, "
        "m.variable AS variable, m.city AS city, m.venue AS venue, "
        "m.settlement_date AS settlement_date, m.outcome_spec AS outcome_spec, "
        "r.started_at AS started_at, o.actual_value AS actual_value "
        "FROM predictions p "
        "JOIN outcomes o ON o.market_id = p.market_id "
        "JOIN markets m ON m.id = p.market_id "
        "JOIN runs r ON r.id = p.run_id "
        "WHERE p.dist_params IS NOT NULL AND o.actual_value IS NOT NULL "
        "AND m.variable != 'PRCP'" + since_clause,
        since_params,
    ).fetchall()
    rows = [dict(r) for r in rows]
    # Latest run per (market, UTC day), same collapse as tracking.py.
    latest: dict[tuple[str, str], tuple[str, str]] = {}
    for r in rows:
        key = (r["market_id"], r["started_at"][:10])
        marker = (r["started_at"], r["run_id"])
        if key not in latest or marker > latest[key]:
            latest[key] = marker
    keep = {(mid, rid) for (mid, _), (_, rid) in latest.items()}
    rows = [r for r in rows if (r["market_id"], r["run_id"]) in keep]
    # predictions carries one row per bucket/side; collapse to one row per
    # (run, market) so each market contributes exactly once to the score.
    seen: set[tuple[str, str]] = set()
    uniq_rows: list[dict[str, Any]] = []
    for r in rows:
        key = (r["run_id"], r["market_id"])
        if key in seen:
            continue
        seen.add(key)
        uniq_rows.append(r)
    return uniq_rows


def fetch_forecast_pools(conn, run_market_ids: list[tuple[str, str]]) -> dict:
    """{(run_id, market_id): [(source, model, values_json, member_flag)]}.

    The forecasts table has no member column: a group is an ensemble group iff
    its values_json decodes to >1 value (the recorder aggregates per
    (source, model, variable, lead), so a multi-value row is a member set).
    That reconstruction is exact for the live pool shape (det models are always
    single-valued rows; ensembles are the only multi-valued rows).
    """
    pools: dict[tuple[str, str], list[tuple[str, str, list[float]]]] = defaultdict(list)
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
            if (r["run_id"], r["market_id"]) not in wanted:
                continue
            try:
                values = json.loads(r["values_json"])
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(values, list) or not values:
                continue
            pools[(r["run_id"], r["market_id"])].append(
                (r["source"], r["model"], [float(v) for v in values])
            )
    return pools


def fetch_calibration_table(conn) -> dict[tuple[str, str, int], dict[str, Any]]:
    rows = conn.execute(
        "SELECT station, variable, lead_time, bias, var_a, var_b, df, n_samples FROM calibration"
    ).fetchall()
    return {(r["station"], r["variable"], r["lead_time"]): dict(r) for r in rows}


def buckets_from_spec(outcome_spec: str | None) -> list[dict[str, Any]] | None:
    if not outcome_spec:
        return None
    try:
        spec = json.loads(outcome_spec)
    except json.JSONDecodeError:
        return None
    if not isinstance(spec, list) or not spec:
        return None
    return spec


# ----------------------------------------------------------------------------
# Scoring
# ----------------------------------------------------------------------------


def apply_cell(
    mu: float, sigma: float, cell: dict[str, Any] | None, min_sigma: float
) -> tuple[float, float, float | None]:
    """Mirror apply_calibration's three regimes on a raw (mu, sigma)."""
    widened = max(sigma * UNCALIBRATED_WIDEN, min_sigma)
    if (
        cell is None
        or cell["n_samples"] < MIN_CAL_BIAS_SAMPLES
        or cell["var_a"] is None
        or cell["var_b"] is None
    ):
        return mu, widened, None
    if cell["n_samples"] < MIN_CAL_SAMPLES:
        return mu - cell["bias"], widened, None
    pred_var = cell["var_a"] + cell["var_b"] * sigma**2
    return (
        mu - cell["bias"],
        max(float(np.sqrt(max(pred_var, 0.0))), min_sigma),
        cell["df"],
    )


def cdf_at(mu: float, sigma: float, df: float | None, actual: float) -> float:
    if df is None:
        return float(norm.cdf(actual, loc=mu, scale=sigma))
    z = np.array([(actual - mu) / sigma])
    return float(std_cdf_for(df)(z)[0])


def pit_tail_ratios(pits: list[float]) -> dict[str, float]:
    n = len(pits)
    out: dict[str, float] = {}
    for label, q, upper in (
        ("upper_10", 0.10, True),
        ("lower_10", 0.10, False),
        ("upper_05", 0.05, True),
        ("lower_05", 0.05, False),
    ):
        count = sum(1 for p in pits if (p > 1 - q if upper else p < q))
        out[label] = (count / n) / q if n else 0.0
    return out


def grade_bucket(entry: dict[str, Any], actual: float) -> bool:
    return settles(entry["kind"], entry["lo"], entry["hi"], entry["threshold"], actual)


def score_arm(
    arm: str,
    rows: list[dict[str, Any]],
    pools: dict,
    cal_table: dict,
) -> dict[tuple[str, int], dict[str, Any]]:
    """Per-(variable, lead) Brier + PIT tail ratios for one sigma definition."""
    brier_terms: dict[tuple[str, int], list[float]] = defaultdict(list)
    pits: dict[tuple[str, int], list[float]] = defaultdict(list)
    for r in rows:
        key = (r["run_id"], r["market_id"])
        pool = pools.get(key)
        spec = buckets_from_spec(r["outcome_spec"])
        if not pool or not spec:
            continue
        try:
            # Lead from the run's UTC-start date minus the settlement date:
            # the store records started_at in UTC, so a run started after
            # 20:00 ET already rolls to the next UTC day and reads one lead
            # higher than the local-calendar interpretation the live
            # pipeline's ForecastSample.lead_time_days uses.
            lead = (
                date.fromisoformat(r["settlement_date"]) - date.fromisoformat(r["started_at"][:10])
            ).days
        except ValueError:
            continue
        if lead < 0:
            continue
        groups = _groups([(s, m, v) for s, m, values in pool for v in values])
        if not groups:
            continue
        ens_keys = {(s, m) for s, m, values in pool if len(values) > 1}
        mu = pooled_mu(groups)
        sigma = old_sigma(groups, ens_keys) if arm == "old" else new_sigma(groups)
        cell = cal_table.get((r["city_icao"], r["variable"], lead))
        # Floor the raw sigma at MIN_SIGMA_F before the cell, mirroring
        # fit_gaussian, so the var_b * sigma^2 term sees the same input the
        # live pipeline feeds apply_calibration.
        pmu, psig, pdf = apply_cell(mu, max(sigma, MIN_SIGMA_F), cell, MIN_SIGMA_F)
        grp = (r["variable"], lead)
        pits[grp].append(cdf_at(pmu, psig, pdf, r["actual_value"]))
        for entry in spec:
            kind, lo, hi, thr = entry["kind"], entry["lo"], entry["hi"], entry["threshold"]
            if kind == "below":
                p = (
                    float(norm.cdf(thr + 0.5, loc=pmu, scale=psig))
                    if pdf is None
                    else float(student_t.cdf((thr + 0.5 - pmu) / psig, pdf))
                )
            elif kind == "above":
                p = 1 - (
                    float(norm.cdf(thr - 0.5, loc=pmu, scale=psig))
                    if pdf is None
                    else float(student_t.cdf((thr - 0.5 - pmu) / psig, pdf))
                )
            else:
                hi_c = (
                    float(norm.cdf(hi + 0.5, loc=pmu, scale=psig))
                    if pdf is None
                    else float(student_t.cdf((hi + 0.5 - pmu) / psig, pdf))
                )
                lo_c = (
                    float(norm.cdf(lo - 0.5, loc=pmu, scale=psig))
                    if pdf is None
                    else float(student_t.cdf((lo - 0.5 - pmu) / psig, pdf))
                )
                p = hi_c - lo_c
            won = grade_bucket(entry, r["actual_value"])
            brier_terms[grp].append((p - (1.0 if won else 0.0)) ** 2)
    out: dict[tuple[str, int], dict[str, Any]] = {}
    for grp in sorted(set(brier_terms) | set(pits)):
        terms = brier_terms.get(grp, [])
        out[grp] = {
            "n_runs": len(pits.get(grp, [])),
            "brier": sum(terms) / len(terms) if terms else None,
            **pit_tail_ratios(pits.get(grp, [])),
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dsn-file", default="/tmp/batch424/dsn.env")
    ap.add_argument("--since", default=None, help="restrict to runs started on/after this date")
    ap.add_argument("--output", default="scripts/refit_v2_replay_results.json")
    args = ap.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn and Path(args.dsn_file).exists():
        dsn = Path(args.dsn_file).read_text().strip()
    if not dsn:
        print("DATABASE_URL not set and no --dsn-file readable; aborting", file=sys.stderr)
        return 2
    # Never echo the DSN.
    conn = connect(dsn)
    try:
        rows = fetch_replay_rows(conn, args.since)
        print(f"replay population: {len(rows)} (run, market) rows")
        pools = fetch_forecast_pools(conn, [(r["run_id"], r["market_id"]) for r in rows])
        print(f"sample pools found: {len(pools)}")

        # Calibration cells are keyed by ICAO; the replay rows carry city. Join
        # via the station registry (city -> ICAO), same mapping the live run uses.
        from rainmaker.config import INTL_STATIONS, KALSHI_STATIONS, STATIONS

        all_stations = {**STATIONS, **INTL_STATIONS, **KALSHI_STATIONS}
        for r in rows:
            st = all_stations.get(r["city"])
            r["city_icao"] = st.icao if st else ""

        cal_table = fetch_calibration_table(conn)
        print(f"calibration cells: {len(cal_table)}")

        arms = {
            "old": score_arm("old", rows, pools, cal_table),
            "new": score_arm("new", rows, pools, cal_table),
        }

        result: dict[str, Any] = {
            "since": args.since,
            "population": len(rows),
            "arms": {
                arm: {f"{v}|{lead}": stats for (v, lead), stats in per.items()}
                for arm, per in arms.items()
            },
        }
        Path(args.output).write_text(json.dumps(result, indent=2))

        cols = (
            f"{'var|lead':<10} {'n':>6} {'Brier old':>10} {'Brier new':>10}"
            f" {'d(Brier)':>9} {'u10 o/n':>13} {'l10 o/n':>13} {'u05 o/n':>13}"
        )
        print(cols)
        print("-" * len(cols))
        keys = sorted(set(arms["old"]) | set(arms["new"]))
        for k in keys:
            o, nw = arms["old"].get(k), arms["new"].get(k)
            if not o or not nw:
                continue
            db = (
                (nw["brier"] - o["brier"])
                if (o["brier"] is not None and nw["brier"] is not None)
                else None
            )

            def fmt(x):
                return f"{x:.4f}" if x is not None else "-"

            print(
                f"{k[0] + '|' + str(k[1]):<10} {o['n_runs']:>6} "
                f"{fmt(o['brier']):>10} {fmt(nw['brier']):>10} {fmt(db):>9} "
                f"{o['upper_10']:.2f}/{nw['upper_10']:.2f} "
                f"{o['lower_10']:.2f}/{nw['lower_10']:.2f} "
                f"{o['upper_05']:.2f}/{nw['upper_05']:.2f}"
            )
        print(f"\nresults written to {args.output}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    from rainmaker.store.db import READ_METER, report_reads

    try:
        raise SystemExit(main())
    finally:
        if READ_METER.connections:
            report_reads()
