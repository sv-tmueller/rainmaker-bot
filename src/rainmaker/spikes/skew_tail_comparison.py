"""Spike #382: does a skewed (two-piece/split-t) family fix the TMIN lead 1
lower-tail overpopulation that the live Student-t (fitted df) did not fix?

Dead to the live path, like tail_objective.py and tmax_tail_diagnosis.py:
nothing here is imported by cli.py or any live-path module. Run it directly:

    uv run python -m rainmaker.spikes.skew_tail_comparison

The #289 addendum (docs/architecture/tail-objective-decision.md) classified
TMIN lead 1 as "skew, not kurtosis" (g1=-0.611, Kelly skew=-0.175, n=635) on
GAUSSIAN-residual standardized residuals. A symmetric Student-t thickens both
tails equally and cannot address one-sided skew. This spike tests whether a
two-piece (split-t) distribution, which allows different scales left and right
of the mode, fixes the lower tail without degrading the upper tail, the body,
or Brier, relative to the CURRENT LIVE BASELINE (Student-t with fitted df).

Findings are written up as a third addendum in
docs/architecture/tail-objective-decision.md, not read back by any other code.

## Architecture

Sibling module to tail_objective.py and tmax_tail_diagnosis.py, following the
#289 precedent: imports the #284 harness read-only (scoring engine, data
loading, PIT machinery, types, n-gating) and never edits it. tail_objective.py
is the recorded artifact of #284; tmax_tail_diagnosis.py is the recorded
artifact of #289. Both stay frozen.

The live baseline (Student-t with fitted df) is imported from
probability.calibration (fit_student_t_free_df, apply_calibration), the same
module the live path uses, so the comparison judges the actual shipped fit.

## Two-piece (split-t) distribution

A split-t generalizes the Student-t by allowing different scales on each side
of the location parameter:

    F(x; loc, scale_L, scale_R, df) =
        0.5 * T_cdf((x - loc) / scale_L, df)    for x < loc
        1 - 0.5 * T_cdf(-(x - loc) / scale_R, df)  for x >= loc

where T_cdf is the standard Student-t CDF at df degrees of freedom. At
scale_L == scale_R this reduces to a symmetric Student-t with scale. The
ratio scale_L / scale_R controls the skew: > 1 stretches the left tail
(thicker lower tail), < 1 stretches the right. The distribution is continuous
at loc (both halves meet at CDF=0.5) and integrates to 1 by construction
(each half is a half-Student-t density scaled to 0.5).

Parametrized for EMOS as (bias, var_a, var_b, log_scale_ratio, log_df_minus_2):
the common scale inherits the EMOS variance regression
sqrt(max(var_a + var_b * ev, FIT_MIN_SIGMA^2)), and the left/right scales are
common_scale * exp(+r/2) and common_scale * exp(-r/2) respectively, where r
is the log scale ratio (signed: r > 0 stretches left). df is parametrized as
2 + exp(log_df_minus_2), bounded identically to the live fit. At r=0 the
split-t reduces to the symmetric Student-t, so the baseline is nested.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from math import sqrt
from typing import Any

import numpy as np
from numpy.typing import NDArray
from scipy.optimize import minimize
from scipy.stats import t as student_t

from rainmaker.config import (
    MIN_CAL_BIAS_SAMPLES,
    MIN_CAL_SAMPLES,
    MIN_SIGMA_F,
)
from rainmaker.spikes.tail_objective import (
    FIT_MIN_SIGMA,
    MIN_CELL_PAIRS,
    FitPair,
    _arrays,
    _pool_cell_evals,
    _split,
    _warm_start,
    apply_emos_regime,
    bucket_probability_generic,
    fetch_or_load_cell_data,
    numeric_crps,
    pit_tail_ratios,
    score_candidate_cell,
)
from rainmaker.spikes.tmax_tail_diagnosis import (
    ResidualRow,
    ResidualShape,
    classify_residual_shape,
)

# ---------------------------------------------------------------------------
# Split-t (two-piece Student-t) standardized CDF and CRPS
# ---------------------------------------------------------------------------

_SPLIT_T_EPS = 1e-12


def split_t_std_cdf(
    log_scale_ratio: float, df: float
) -> Callable[[NDArray[np.float64]], NDArray[np.float64]]:
    """Standardized split-t CDF on the u-grid, for use with numeric_crps.

    The standardization maps x to u = (x - loc) / common_scale, where
    common_scale is the geometric mean of the left and right scales. The
    left/right scales are common_scale * exp(+r/2) and common_scale *
    exp(-r/2), so in u-space the left half uses scale exp(+r/2) and the
    right half uses scale exp(-r/2). At r=0 both reduce to 1 (symmetric t).

    Construction: each half is a half-Student-t (truncated to one side of
    the mode), weighted 0.5. The CDF of the left half (u < 0) is
    2 * T_cdf(u / s_left, df) * 0.5 = T_cdf(u / s_left, df), ranging from 0
    to 0.5 as u goes from -inf to 0. The right half (u >= 0) is
    0.5 + (T_cdf(u / s_right, df) - 0.5) * 2 * 0.5 = T_cdf(u / s_right, df),
    ranging from 0.5 to 1. The 0.5 weights cancel against the doubling
    from the half-truncation, so the CDF simplifies to T_cdf(u / s_side, df)
    on each side. At r=0 this is exactly T_cdf(u, df) (symmetric).

    In u-space:
        u < 0:  T_cdf(u / exp(+r/2), df)
        u >= 0: T_cdf(u / exp(-r/2), df)
    """
    r = log_scale_ratio
    s_left = np.exp(r / 2.0)  # left-half scale (>1 stretches left tail)
    s_right = np.exp(-r / 2.0)  # right-half scale (<1 compresses right tail)

    def cdf(u: NDArray[np.float64]) -> NDArray[np.float64]:
        u = np.asarray(u, dtype=np.float64)
        left = u < 0
        right = ~left
        out = np.empty_like(u)
        out[left] = student_t.cdf(u[left] / s_left, df)
        out[right] = student_t.cdf(u[right] / s_right, df)
        return out

    return cdf


def split_t_scalar_cdf(
    x: float, loc: float, common_scale: float, log_scale_ratio: float, df: float
) -> float:
    """Scalar split-t CDF for bucket probability integration."""
    r = log_scale_ratio
    s_left = np.exp(r / 2.0)
    s_right = np.exp(-r / 2.0)
    u = (x - loc) / common_scale
    if u < 0:
        return float(student_t.cdf(u / s_left, df))
    return float(student_t.cdf(u / s_right, df))


# ---------------------------------------------------------------------------
# Split-t EMOS fitters
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SkewEmosFit:
    """Split-t EMOS fit: (bias, var_a, var_b) plus log_scale_ratio and df."""

    bias: float
    var_a: float
    var_b: float
    log_scale_ratio: float  # r; 0 = symmetric Student-t; >0 stretches LEFT tail
    df: float
    n_samples: int


def _split_t_mean_numeric_score(
    log_scale_ratio: float,
    df: float,
    bias: float,
    var_a: float,
    var_b: float,
    mu: NDArray[np.float64],
    ev: NDArray[np.float64],
    actual: NDArray[np.float64],
) -> float:
    """Mean numeric CRPS for the split-t, computed on the standardized grid.

    The numeric_crps engine integrates over a standardized u-grid and multiplies
    by the scale. For the split-t the common_scale is the geometric mean of
    the left and right scales; the standardized CDF encodes the asymmetry. The
    Jacobian from the piecewise rescaling is absorbed by computing CRPS
    numerically on the u-grid with the split-t CDF, which is exact up to the
    trapezoidal-rule error (same approximation as the symmetric candidates).
    """
    sigma = np.sqrt(np.maximum(var_a + var_b * ev, FIT_MIN_SIGMA**2))
    loc = mu - bias
    std_cdf = split_t_std_cdf(log_scale_ratio, df)
    scores = numeric_crps(std_cdf, loc, sigma, actual, weight=None)
    return float(np.mean(scores))


def fit_split_t_fixed_df(pairs: list[FitPair], *, df: float, r0: float = 0.0) -> SkewEmosFit:
    """Split-t EMOS with df held fixed; (bias, var_a, var_b, log_scale_ratio)
    fit by minimizing mean numeric CRPS. r0 is the warm-start log scale ratio.
    """
    mu, ev, actual = _arrays(pairs)
    bias0, var_a0, var_b0 = _warm_start(mu, ev, actual)

    def objective(params: NDArray[np.float64]) -> float:
        b, va, vb, r = params
        return _split_t_mean_numeric_score(r, df, b, va, vb, mu, ev, actual)

    result = minimize(
        objective,
        x0=np.array([bias0, var_a0, var_b0, r0]),
        method="L-BFGS-B",
        bounds=[(None, None), (0.0, None), (0.0, None), (-3.0, 3.0)],
    )
    b, va, vb, r = result.x
    return SkewEmosFit(
        bias=float(b),
        var_a=float(va),
        var_b=float(vb),
        log_scale_ratio=float(r),
        df=df,
        n_samples=len(pairs),
    )


def fit_split_t_free_df(pairs: list[FitPair], *, df0: float = 8.0, r0: float = 0.0) -> SkewEmosFit:
    """Split-t EMOS with df as a 5th parameter, parametrized as log(df - 2)
    (bounded so df stays in (2, 62], same as the live Student-t fit), and
    log_scale_ratio as a 4th, fit jointly with (bias, var_a, var_b) by
    minimizing mean numeric CRPS.
    """
    mu, ev, actual = _arrays(pairs)
    bias0, var_a0, var_b0 = _warm_start(mu, ev, actual)
    log_df0 = np.log(df0 - 2.0)

    def objective(params: NDArray[np.float64]) -> float:
        b, va, vb, r, log_df = params
        df = 2.0 + np.exp(log_df)
        return _split_t_mean_numeric_score(r, float(df), b, va, vb, mu, ev, actual)

    result = minimize(
        objective,
        x0=np.array([bias0, var_a0, var_b0, r0, log_df0]),
        method="L-BFGS-B",
        bounds=[
            (None, None),
            (0.0, None),
            (0.0, None),
            (-3.0, 3.0),
            (np.log(0.5), np.log(60.0)),
        ],
    )
    b, va, vb, r, log_df = result.x
    df_fit = 2.0 + float(np.exp(log_df))
    return SkewEmosFit(
        bias=float(b),
        var_a=float(va),
        var_b=float(vb),
        log_scale_ratio=float(r),
        df=df_fit,
        n_samples=len(pairs),
    )


# ---------------------------------------------------------------------------
# n-gating for the split-t (mirrors apply_emos_regime, generalized to skew)
# ---------------------------------------------------------------------------


_UNCALIBRATED_WIDEN = 1.25  # mirrors config.UNCALIBRATED_WIDEN, kept local to avoid a new import


def apply_split_t_regime(
    *,
    bias: float,
    var_a: float,
    var_b: float,
    log_scale_ratio: float,
    df: float,
    n_samples: int,
    mu: float,
    ensemble_var: float,
    min_sigma: float,
    fallback_df: float = 5.0,
    min_bias_samples: int = MIN_CAL_BIAS_SAMPLES,
    min_samples: int = MIN_CAL_SAMPLES,
) -> tuple[float, float, float, float]:
    """Same three n-gating regimes as apply_emos_regime, but returns
    (loc, common_scale, log_scale_ratio, df) for the split-t. Below
    min_bias_samples the prediction is uncalibrated and symmetric (r=0, large
    df approximating Gaussian). Between min_bias_samples and min_samples it is
    bias-only with a fixed fallback df and r=0 (not enough data to trust a
    fitted skew). At min_samples+ the full split-t fit applies.
    """
    widened = max(sqrt(ensemble_var) * _UNCALIBRATED_WIDEN, min_sigma)
    if n_samples < min_bias_samples:
        return mu, widened, 0.0, 30.0
    if n_samples < min_samples:
        return mu - bias, widened, 0.0, fallback_df
    scale = max(sqrt(max(var_a + var_b * ensemble_var, 0.0)), min_sigma)
    return mu - bias, scale, log_scale_ratio, df


# ---------------------------------------------------------------------------
# Skew-residual confirmation (acceptance criterion: #289 classification on
# Student-t baseline residuals, not just Gaussian)
# ---------------------------------------------------------------------------


def student_t_baseline_residuals(
    cell_data: dict[tuple[str, str, int], list[tuple[date, float, float, float]]],
) -> list[ResidualRow]:
    """Standardized residuals from the LIVE Student-t baseline fit (the
    shipped calibration), on the same 60/40 chronological split and
    per-station-then-pool design as tmax_tail_diagnosis.baseline_eval_residuals,
    but using the live fit_student_t_free_df + apply_emos_regime with df
    instead of the Gaussian fit_calibration.

    This is the population the skew-residual confirmation reads: if TMIN lead 1
    still reads "skew, not kurtosis" on Student-t residuals, the skew motive
    holds against the current live fit, not just the pre-refit Gaussian.
    """
    from rainmaker.probability.calibration import (
        CalibrationPair,
        fit_student_t_free_df,
    )

    out: list[ResidualRow] = []
    for (icao, variable, lead), rows in sorted(cell_data.items()):
        if len(rows) < MIN_CELL_PAIRS:
            continue
        fit_rows, eval_rows = _split(rows)
        if not fit_rows or not eval_rows:
            continue
        # Only TMIN cells use Student-t in the live path (CALIBRATION_FAMILY).
        if variable != "TMIN":
            continue
        cal_pairs = [
            CalibrationPair(mu=mu, sigma=sigma, ensemble_var=sigma**2, actual=actual)
            for _d, mu, sigma, actual in fit_rows
        ]
        fit = fit_student_t_free_df(icao, variable, lead, cal_pairs)
        for d, mu, sigma_raw, actual in eval_rows:
            loc, scale, _df = apply_emos_regime(
                bias=fit.bias,
                var_a=fit.var_a,
                var_b=fit.var_b,
                df=fit.df,
                n_samples=fit.n_samples,
                mu=mu,
                ensemble_var=sigma_raw**2,
                min_sigma=MIN_SIGMA_F,
                fallback_df=5.0,
            )
            z = (actual - loc) / scale
            out.append(ResidualRow(icao=icao, variable=variable, lead=lead, target_date=d, z=z))
    return out


def student_t_residual_shapes(rows: list[ResidualRow]) -> dict[tuple[str, int], ResidualShape]:
    """Pool Student-t residuals per (variable, lead) and compute shape stats.

    Thin wrapper around tmax_tail_diagnosis.residual_shape_by_cell, renamed
    to signal the baseline change. The classification logic is identical.
    """
    from rainmaker.spikes.tmax_tail_diagnosis import residual_shape_by_cell

    return residual_shape_by_cell(rows)


def gaussian_baseline_residuals_for_regression(
    cell_data: dict[tuple[str, str, int], list[tuple[date, float, float, float]]],
) -> list[ResidualRow]:
    """Gaussian-baseline residuals, for the regression-guard test that
    reproduces the #289 addendum's classification on Gaussian residuals.

    Delegates to tmax_tail_diagnosis.baseline_eval_residuals (the #289
    function) unchanged, so the test asserts the #289 numbers reproduce.
    """
    from rainmaker.spikes.tmax_tail_diagnosis import baseline_eval_residuals

    return baseline_eval_residuals(cell_data)


# ---------------------------------------------------------------------------
# Comparison driver
# ---------------------------------------------------------------------------

SKEW_CANDIDATES: tuple[str, ...] = (
    "t_live_baseline",  # live Student-t fitted-df (the thing to beat)
    "split_t_df5",  # split-t, df fixed at 5
    "split_t_free_df",  # split-t, df fitted (5th parameter)
)


def score_split_t_cell_v2(
    candidate: str,
    variable: str,
    lead: int,
    eval_pairs: list[tuple[date, float, float, float]],
    predict: Callable[[float, float], tuple[float, float, float, float]],
) -> Any:
    """Score a split-t candidate. predict returns (loc, scale, r, df)."""
    from rainmaker.backtest import reliability_bins, standard_buckets
    from rainmaker.probability.outcomes import settles
    from rainmaker.spikes.tail_objective import CellEval, TopBinStat, _tail_bin

    pits: list[float] = []
    crps_scores: list[float] = []
    all_pairs: list[tuple[float, bool]] = []
    top_bin: dict[str, list[tuple[float, bool]]] = {"YES": [], "NO": []}

    for _d, mu, sigma_raw, actual in eval_pairs:
        ensemble_var = sigma_raw**2
        loc, scale, r, df = predict(mu, ensemble_var)
        std_cdf = split_t_std_cdf(r, df)
        z = (actual - loc) / scale
        pits.append(float(std_cdf(np.array([z]))[0]))
        crps_scores.append(float(numeric_crps(std_cdf, loc, scale, actual, weight=None)[0]))

        def cdf(
            x: float,
            *,
            sc: Callable[[NDArray[np.float64]], NDArray[np.float64]] = std_cdf,
            lc: float = loc,
            s: float = scale,
        ) -> float:
            return float(sc(np.array([(x - lc) / s]))[0])

        center = round(mu)
        buckets = standard_buckets(float(center))
        for b in buckets:
            p = bucket_probability_generic(cdf, b)
            won = settles(b.kind, b.lo, b.hi, b.threshold, actual)
            all_pairs.append((p, won))
            claim, event_won = (p, won) if p >= 0.5 else (1 - p, not won)
            side = "YES" if p >= 0.5 else "NO"
            if _tail_bin(claim) == "[0.95,1.0]":
                top_bin[side].append((claim, event_won))

    ratios = pit_tail_ratios(pits)
    brier = sum((p - (1.0 if w else 0.0)) ** 2 for p, w in all_pairs) / len(eval_pairs)
    bins = reliability_bins(all_pairs)
    body_bins = [b for b in bins if b.lo < 0.90]
    body_max_dev = max((abs(b.predicted_mean - b.observed_freq) for b in body_bins), default=0.0)

    def top_bin_stat(side: str) -> TopBinStat | None:
        items = top_bin[side]
        if not items:
            return None
        n = len(items)
        return TopBinStat(
            n=n,
            claimed_mean=sum(c for c, _ in items) / n,
            realized_freq=sum(1 for _, w in items if w) / n,
        )

    return CellEval(
        candidate=candidate,
        variable=variable,
        lead=lead,
        n=int(ratios["n"]),
        upper_10=float(ratios["upper_10"]),
        lower_10=float(ratios["lower_10"]),
        upper_05=float(ratios["upper_05"]),
        lower_05=float(ratios["lower_05"]),
        brier=brier,
        mean_crps=sum(crps_scores) / len(crps_scores),
        body_max_dev=body_max_dev,
        top_bin_yes=top_bin_stat("YES"),
        top_bin_no=top_bin_stat("NO"),
    )


def run_skew_comparison(
    cell_data: dict[tuple[str, str, int], list[tuple[date, float, float, float]]],
) -> dict[tuple[str, str, int], Any]:
    """Fit the live Student-t baseline and the split-t candidates per
    (station, variable, lead) cell, pool eval scores per (variable, lead)
    across stations. Focus on TMIN (the variable with the diagnosed skew and
    the live Student-t fit); TMAX is Gaussian in the live path and excluded
    from the skew comparison (it has no skew hypothesis to test).
    """
    from rainmaker.probability.calibration import (
        CalibrationPair,
        fit_student_t_free_df,
    )

    fits: dict[tuple[str, str, str, int], Any] = {}

    for (icao, variable, lead), rows in cell_data.items():
        if variable != "TMIN":
            continue
        if len(rows) < MIN_CELL_PAIRS:
            continue
        fit_rows, eval_rows = _split(rows)
        if not fit_rows or not eval_rows:
            continue
        fit_pairs: list[FitPair] = [(mu, sigma**2, actual) for _d, mu, sigma, actual in fit_rows]
        cal_pairs = [
            CalibrationPair(mu=mu, sigma=sigma, ensemble_var=sigma**2, actual=actual)
            for _d, mu, sigma, actual in fit_rows
        ]

        fits["t_live_baseline", icao, variable, lead] = fit_student_t_free_df(
            icao, variable, lead, cal_pairs
        )
        fits["split_t_df5", icao, variable, lead] = fit_split_t_fixed_df(fit_pairs, df=5.0)
        fits["split_t_free_df", icao, variable, lead] = fit_split_t_free_df(fit_pairs)

    results: dict[tuple[str, str, int], Any] = {}
    for candidate in SKEW_CANDIDATES:
        by_vl: dict[tuple[str, int], list[Any]] = {}
        for (icao, variable, lead), rows in cell_data.items():
            if variable != "TMIN":
                continue
            if len(rows) < MIN_CELL_PAIRS:
                continue
            fit = fits.get((candidate, icao, variable, lead))
            if fit is None:
                continue
            _fit_rows, eval_rows = _split(rows)
            if not eval_rows:
                continue

            if candidate == "t_live_baseline":
                # Symmetric Student-t baseline: use the existing scorer.
                def predict_sym(
                    mu: float,
                    ensemble_var: float,
                    *,
                    fit: Any = fit,
                ) -> tuple[float, float, float | None]:
                    return apply_emos_regime(
                        bias=fit.bias,
                        var_a=fit.var_a,
                        var_b=fit.var_b,
                        df=fit.df,
                        n_samples=fit.n_samples,
                        mu=mu,
                        ensemble_var=ensemble_var,
                        min_sigma=MIN_SIGMA_F,
                        fallback_df=5.0,
                    )

                cell_eval = score_candidate_cell(candidate, variable, lead, eval_rows, predict_sym)
            else:
                # Split-t candidate: use the split-t-aware scorer.
                def predict_split(
                    mu: float,
                    ensemble_var: float,
                    *,
                    fit: Any = fit,
                ) -> tuple[float, float, float, float]:
                    loc, scale, r, df = apply_split_t_regime(
                        bias=fit.bias,
                        var_a=fit.var_a,
                        var_b=fit.var_b,
                        log_scale_ratio=fit.log_scale_ratio,
                        df=fit.df,
                        n_samples=fit.n_samples,
                        mu=mu,
                        ensemble_var=ensemble_var,
                        min_sigma=MIN_SIGMA_F,
                        fallback_df=5.0,
                    )
                    return loc, scale, r, df

                cell_eval = score_split_t_cell_v2(
                    candidate, variable, lead, eval_rows, predict_split
                )

            by_vl.setdefault((variable, lead), []).append(cell_eval)

        for (variable, lead), cell_evals in by_vl.items():
            results[candidate, variable, lead] = _pool_cell_evals(
                candidate, variable, lead, cell_evals
            )

    return results


def render_skew_table(
    results: dict[tuple[str, str, int], Any],
    shapes: dict[tuple[str, int], ResidualShape] | None = None,
) -> str:
    """Render the comparison table as markdown."""
    header = (
        "| Candidate | Var | Lead | n | Up.10 | Lo.10 | Up.05 | Lo.05 | Brier | "
        "CRPS | BodyMaxDev | NO[.95,1] claim/real | YES[.95,1] claim/real |"
    )
    rule = "| --- " * 13 + "|"
    lines = [header, rule]

    def fmt_top(stat: Any) -> str:
        if stat is None:
            return "-"
        return f"{stat.claimed_mean:.3f}/{stat.realized_freq:.3f} (n={stat.n})"

    for key in sorted(results, key=lambda k: (k[1], k[2])):
        c = results[key]
        row = (
            f"| {c.candidate} | {c.variable} | {c.lead} | {c.n} | {c.upper_10:.2f} | "
            f"{c.lower_10:.2f} | {c.upper_05:.2f} | {c.lower_05:.2f} | {c.brier:.3f} | "
            f"{c.mean_crps:.3f} | {c.body_max_dev:.3f} | "
        )
        row += f"{fmt_top(c.top_bin_no)} | {fmt_top(c.top_bin_yes)} |"
        lines.append(row)

    if shapes:
        lines.append("")
        lines.append("## Skew-residual confirmation (Student-t baseline)")
        lines.append("")
        lines.append("| Var | Lead | n | g1 | se(g1) | g2 | se(g2) | Kelly | Classification |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for (variable, lead), s in sorted(shapes.items()):
            cls = classify_residual_shape(s, contrast_g2=None)
            lines.append(
                f"| {variable} | {lead} | {s.n} | {s.g1:.3f} | {sqrt(6.0 / s.n):.3f} | "
                f"{s.g2:.3f} | {sqrt(24.0 / s.n):.3f} | {s.kelly:.3f} | {cls} |"
            )

    return "\n".join(lines) + "\n"


def main() -> None:
    """Run the skew comparison and print results to stdout.

    Steps:
    1. Load (or fetch) the cached archive pairs.
    2. Compute Student-t baseline residuals and confirm the skew classification.
    3. Run the split-t vs live-Student-t comparison.
    4. Render the comparison table + skew confirmation.
    """
    cell_data = fetch_or_load_cell_data()

    # Step 1: skew-residual confirmation on Student-t baseline
    st_residuals = student_t_baseline_residuals(cell_data)
    st_shapes = student_t_residual_shapes(st_residuals)
    print("## Skew-residual confirmation (Student-t baseline)", file=sys.stderr)
    for (variable, lead), s in sorted(st_shapes.items()):
        cls = classify_residual_shape(s, contrast_g2=None)
        print(
            f"  {variable} L{lead}: n={s.n} g1={s.g1:.3f} g2={s.g2:.3f} "
            f"kelly={s.kelly:.3f} -> {cls}",
            file=sys.stderr,
        )

    # Step 2: comparison run
    results = run_skew_comparison(cell_data)
    print(render_skew_table(results, st_shapes))


if __name__ == "__main__":
    main()
