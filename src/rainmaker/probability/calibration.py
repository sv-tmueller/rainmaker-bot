"""Per-(station, variable, lead_time) bias and EMOS variance correction.

Fit from historical forecast-vs-actual pairs, then applied to the raw predictive
Gaussian. EMOS (Ensemble Model Output Statistics, Gneiting et al. 2005):
  predictive mean = mu - bias
  predictive var  = var_a + var_b * ensemble_var   (a, b >= 0)

Parameters (bias, var_a, var_b) are fit by minimizing mean CRPS over the cell's
pairs. Three regimes gated on n_samples:
  < MIN_CAL_BIAS_SAMPLES  -> uncalibrated: mu unchanged, sigma widened-raw.
  < MIN_CAL_SAMPLES       -> bias-only: mu - bias, sigma still widened-raw.
                             var_a/var_b are not used (they overfit on < 30 points).
  >= MIN_CAL_SAMPLES      -> full EMOS: mu - bias, sigma from sqrt(var_a + var_b*sigma^2).
"""

from collections.abc import Callable
from math import pi, sqrt
from typing import Literal

import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict, Field
from scipy.optimize import minimize
from scipy.stats import norm, t

from rainmaker.config import MIN_CAL_BIAS_SAMPLES, MIN_CAL_SAMPLES, UNCALIBRATED_WIDEN
from rainmaker.probability.distribution import Gaussian


class CalibrationPair(BaseModel):
    model_config = ConfigDict(frozen=True)

    mu: float  # raw forecast mean
    sigma: float = Field(gt=0)  # raw forecast sigma (ensemble spread)
    ensemble_var: float  # sigma^2; stored separately to make the EMOS objective explicit
    actual: float


class Calibration(BaseModel):
    model_config = ConfigDict(frozen=True)

    station: str
    variable: str
    lead_time: int
    bias: float
    var_a: float = Field(ge=0)  # EMOS intercept: irreducible variance floor
    var_b: float = Field(ge=0)  # EMOS slope: ensemble-variance amplification
    # Student-t degrees of freedom. None means Gaussian; apply_calibration trusts
    # this presence/absence to dispatch family, not a separate variable lookup, so
    # rollback is a refit (write df=None again), not a code change.
    df: float | None = None
    n_samples: int


class Predictive(BaseModel):
    """A predictive distribution: Gaussian if df is None, else Student-t(df).

    apply_calibration's return type. Gaussian (distribution.py) stays
    fit_gaussian's raw-pool output; Predictive is the (possibly calibrated,
    possibly heavier-tailed) distribution bucket_probability integrates over.
    """

    model_config = ConfigDict(frozen=True)

    mu: float
    sigma: float = Field(gt=0)
    df: float | None = None


class Accuracy(BaseModel):
    model_config = ConfigDict(frozen=True)

    n: int
    mae_f: float  # mean absolute error, degrees F
    bias_f: float  # mean signed error (mu - actual), degrees F
    # Probability-calibration metrics; None for rows computed without dist_params.
    crps: float | None = None
    coverage_50: float | None = None
    coverage_80: float | None = None
    coverage_90: float | None = None
    reliability_bins: list[dict[str, object]] | None = None  # serialised ReliabilityBin dicts


def _crps_gaussian(mu: float, sigma: float, actual: float) -> float:
    """CRPS for a Gaussian predictive distribution (Gneiting et al. 2005).

    sigma must be > 0; the objective floors it before calling.
    """
    z = (actual - mu) / sigma
    phi_z = float(norm.pdf(z))
    Phi_z = float(norm.cdf(z))
    return sigma * (z * (2 * Phi_z - 1) + 2 * phi_z - 1 / sqrt(pi))


def fit_calibration(
    station: str, variable: str, lead_time: int, pairs: list[CalibrationPair]
) -> Calibration:
    """Fit EMOS parameters (bias, var_a, var_b) by minimizing mean CRPS.

    Predictive mean = mu - bias.
    Predictive var  = var_a + var_b * ensemble_var.
    Both var_a and var_b are constrained to >= 0.

    Warm start: bias = mean signed error, var_b = 1 (unit amplification),
    var_a = max(0, residual variance - mean(ensemble_var)).
    """
    if not pairs:
        raise ValueError("cannot fit calibration with no pairs")

    mu_arr = np.array([p.mu for p in pairs])
    actual_arr = np.array([p.actual for p in pairs])
    ev_arr = np.array([p.ensemble_var for p in pairs])

    # Warm-start: bias is mean error; var_b=1 maps ensemble to predictive scale.
    bias0 = float(np.mean(mu_arr - actual_arr))
    residuals0 = actual_arr - (mu_arr - bias0)
    resid_var = float(np.mean(residuals0**2))
    mean_ev = float(np.mean(ev_arr))
    var_b0 = 1.0
    var_a0 = max(0.0, resid_var - var_b0 * mean_ev)

    def objective(params: np.ndarray) -> float:
        b, va, vb = params
        total = 0.0
        for mu_i, ev_i, act_i in zip(mu_arr, ev_arr, actual_arr, strict=True):
            pred_var = va + vb * ev_i
            sigma_i = sqrt(max(pred_var, 1e-9))
            total += _crps_gaussian(mu_i - b, sigma_i, act_i)
        return total / len(pairs)

    result = minimize(
        objective,
        x0=np.array([bias0, var_a0, var_b0]),
        method="L-BFGS-B",
        bounds=[(None, None), (0.0, None), (0.0, None)],
    )
    bias_fit, var_a_fit, var_b_fit = result.x
    return Calibration(
        station=station,
        variable=variable,
        lead_time=lead_time,
        bias=float(bias_fit),
        var_a=float(var_a_fit),
        var_b=float(var_b_fit),
        n_samples=len(pairs),
    )


def compute_accuracy(pairs: list[CalibrationPair]) -> Accuracy:
    """Degrees-space forecast accuracy over forecast-vs-actual pairs."""
    if not pairs:
        raise ValueError("cannot compute accuracy with no pairs")
    errors = np.array([p.mu - p.actual for p in pairs])
    return Accuracy(
        n=len(pairs), mae_f=float(np.mean(np.abs(errors))), bias_f=float(np.mean(errors))
    )


# -----------------------------------------------------------------------------
# Student-t numeric CRPS engine, ported from src/rainmaker/spikes/tail_objective.py
# (#284/#291). Do not re-derive the widened-grid / FIT_MIN_SIGMA truncation-trap
# fix here: it was a real bug found and fixed during the spike's own TDD (see
# docs/architecture/tail-objective-decision.md), ported unchanged.
# -----------------------------------------------------------------------------

FIT_MIN_SIGMA = 1.0  # floor (degrees F) on the *fitted* predictive sigma; see numeric_crps

_MAX_GRID = 400_001  # hard cap on numeric_crps's widened grid size
_MAX_MATRIX_ELEMENTS = 20_000_000  # cap on (batch size * grid size); bounds memory/time per call

_StdCdf = Callable[[NDArray[np.float64]], NDArray[np.float64]]


def numeric_crps(
    std_cdf: _StdCdf,
    mu: NDArray[np.float64] | float,
    sigma: NDArray[np.float64] | float,
    actual: NDArray[np.float64] | float,
    *,
    weight: _StdCdf | None = None,
    n_grid: int = 20001,
    span: float = 8.0,
) -> NDArray[np.float64]:
    """Vectorized CRPS via a shared standardized grid (Allen et al., arXiv:2407.03167):

        CRPS(F, y) = integral over x of (F(x) - 1{y <= x})^2 dx

    computed via the substitution x = loc + scale*u so every candidate in a batch
    shares one standardized u-grid. `std_cdf` is the *standardized* (loc=0,
    scale=1) predictive CDF (e.g. scipy.stats.norm.cdf, or scipy.stats.t.cdf at
    some df). Returns one score per (mu, sigma, actual) triple.

    n_grid defaults high (20001, step 0.0008 over span 16) because the indicator
    term has a jump discontinuity at the standardized actual: the trapezoid
    rule's error there is O(step), not O(step^2), so a coarse grid is visibly off
    against the closed-form Gaussian CRPS.

    `span` is a floor, not a hard cutoff: if some (actual - mu)/sigma falls
    outside +/- span (an overconfident candidate scale, or a genuine outlier),
    the grid widens to cover it (resolution held roughly constant by scaling
    n_grid with the widened span). Without this, an optimizer minimizing this
    objective could shrink sigma toward zero: with a *fixed* span, that pushes
    the standardized actual off the truncated grid entirely, so the integral
    silently drops the true (large) penalty for being that overconfident, and
    the whole thing is multiplied by a near-zero sigma on top -- a numerical
    trap disguised as a global minimum. The true integral (span=infinity) does
    not have this problem; this is what makes the finite grid behave like it.
    """
    mu_arr = np.atleast_1d(np.asarray(mu, dtype=np.float64))
    sigma_arr = np.atleast_1d(np.asarray(sigma, dtype=np.float64))
    actual_arr = np.atleast_1d(np.asarray(actual, dtype=np.float64))
    actual_std = (actual_arr - mu_arr) / sigma_arr  # (N,)
    span_needed = float(np.max(np.abs(actual_std))) + 1.0 if actual_std.size else span
    span_eff = max(span, span_needed)
    n_grid_eff = n_grid if span_eff == span else int(round(n_grid * span_eff / span))
    # Cap both the absolute grid size and the batch x grid matrix size: a large
    # fit batch combined with a wide span (many outliers, or an optimizer probing
    # a tiny sigma before the FIT_MIN_SIGMA floor engages) must not allocate an
    # unbounded (N, G) array. Extreme cases lose resolution rather than memory.
    n_grid_eff = min(
        n_grid_eff, _MAX_GRID, max(1001, _MAX_MATRIX_ELEMENTS // max(len(actual_std), 1))
    )
    u = np.linspace(-span_eff, span_eff, n_grid_eff)
    cdf_u = std_cdf(u)  # (G,)
    indicator = (u[None, :] >= actual_std[:, None]).astype(np.float64)  # (N, G)
    integrand = (cdf_u[None, :] - indicator) ** 2  # (N, G)
    if weight is not None:
        x = mu_arr[:, None] + sigma_arr[:, None] * u[None, :]
        integrand = integrand * weight(x)
    result: NDArray[np.float64] = sigma_arr * np.trapezoid(integrand, u, axis=1)
    return result


def std_cdf_for(df: float | None) -> _StdCdf:
    """Standardized predictive CDF: Gaussian if df is None, else Student-t(df)."""
    if df is None:
        return lambda u: np.asarray(norm.cdf(u), dtype=np.float64)
    return lambda u: np.asarray(t.cdf(u, df), dtype=np.float64)


def fit_student_t_free_df(
    station: str, variable: str, lead_time: int, pairs: list[CalibrationPair], *, df0: float = 8.0
) -> Calibration:
    """Student-t EMOS with df as a 4th fit parameter, parametrized as
    log(df - 2) (bounded so df stays in (2, 62]), jointly fit with
    (bias, var_a, var_b) by minimizing mean numeric CRPS.

    Same (bias, var_a, var_b) EMOS parametrization and warm start as
    fit_calibration; the predictive family is the only difference. The fitted
    sigma is floored at FIT_MIN_SIGMA during fitting (not an epsilon): numeric_crps's
    grid is finite, so an optimizer minimizing this objective could otherwise
    walk sigma toward zero to exploit truncation rather than genuinely fitting
    the data (see numeric_crps's docstring). A physical floor forecloses that
    regardless of grid width.
    """
    if not pairs:
        raise ValueError("cannot fit calibration with no pairs")

    mu_arr = np.array([p.mu for p in pairs], dtype=np.float64)
    ev_arr = np.array([p.ensemble_var for p in pairs], dtype=np.float64)
    actual_arr = np.array([p.actual for p in pairs], dtype=np.float64)

    bias0 = float(np.mean(mu_arr - actual_arr))
    residuals0 = actual_arr - (mu_arr - bias0)
    resid_var = float(np.mean(residuals0**2))
    mean_ev = float(np.mean(ev_arr))
    var_b0 = 1.0
    var_a0 = max(0.0, resid_var - var_b0 * mean_ev)
    log_df0 = np.log(df0 - 2.0)

    def objective(params: NDArray[np.float64]) -> float:
        b, va, vb, log_df = params
        df = 2.0 + np.exp(log_df)
        std_cdf = std_cdf_for(float(df))
        sigma = np.sqrt(np.maximum(va + vb * ev_arr, FIT_MIN_SIGMA**2))
        loc = mu_arr - b
        scores = numeric_crps(std_cdf, loc, sigma, actual_arr, weight=None)
        return float(np.mean(scores))

    result = minimize(
        objective,
        x0=np.array([bias0, var_a0, var_b0, log_df0]),
        method="L-BFGS-B",
        bounds=[(None, None), (0.0, None), (0.0, None), (np.log(0.5), np.log(60.0))],
    )
    b, va, vb, log_df = result.x
    df_fit = 2.0 + float(np.exp(log_df))
    return Calibration(
        station=station,
        variable=variable,
        lead_time=lead_time,
        bias=float(b),
        var_a=float(va),
        var_b=float(vb),
        df=df_fit,
        n_samples=len(pairs),
    )


def apply_calibration(
    g: Gaussian,
    cal: Calibration | None,
    *,
    min_sigma: float,
    min_samples: int = MIN_CAL_SAMPLES,
    min_bias_samples: int = MIN_CAL_BIAS_SAMPLES,
) -> tuple[Predictive, Literal["uncalibrated", "bias_only", "full"]]:
    """Return (predictive distribution, calibration state).

    Three regimes on n_samples:
    - "uncalibrated": cal is None or n < min_bias_samples. mu unchanged, sigma widened-raw.
    - "bias_only": min_bias_samples <= n < min_samples. mu shifted by bias, sigma widened-raw.
      var_a/var_b are ignored (they overfit on fewer than min_samples points).
    - "full": n >= min_samples. Full EMOS: mu - bias and sigma from sqrt(var_a + var_b*sigma^2).

    df (the predictive family) applies only in the full regime, straight from
    the calibration row: uncalibrated and bias_only stay the literal existing
    Gaussian-widened branches regardless of cal.df, since there is not enough
    data in those regimes to trust a fitted family shape either.
    """
    widened = max(g.sigma * UNCALIBRATED_WIDEN, min_sigma)
    if cal is None or cal.n_samples < min_bias_samples:
        return Predictive(mu=g.mu, sigma=widened), "uncalibrated"
    if cal.n_samples < min_samples:
        return Predictive(mu=g.mu - cal.bias, sigma=widened), "bias_only"
    mu = g.mu - cal.bias
    pred_var = cal.var_a + cal.var_b * g.sigma**2
    sigma = max(sqrt(max(pred_var, 0.0)), min_sigma)
    return Predictive(mu=mu, sigma=sigma, df=cal.df), "full"
