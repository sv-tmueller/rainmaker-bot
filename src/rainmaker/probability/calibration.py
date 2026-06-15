"""Per-(station, variable, lead_time) bias and EMOS variance correction.

Fit from historical forecast-vs-actual pairs, then applied to the raw predictive
Gaussian. EMOS (Ensemble Model Output Statistics, Gneiting et al. 2005):
  predictive mean = mu - bias
  predictive var  = var_a + var_b * ensemble_var   (a, b >= 0)

Parameters (bias, var_a, var_b) are fit by minimizing mean CRPS over the cell's
pairs. Until a cell has enough pairs we do not trust the fit and fall back to a
conservatively widened raw spread.
"""

from math import pi, sqrt

import numpy as np
from pydantic import BaseModel, ConfigDict, Field
from scipy.optimize import minimize
from scipy.stats import norm

from rainmaker.config import MIN_CAL_SAMPLES, UNCALIBRATED_WIDEN
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
    n_samples: int


class Accuracy(BaseModel):
    model_config = ConfigDict(frozen=True)

    n: int
    mae_f: float  # mean absolute error, degrees F
    bias_f: float  # mean signed error (mu - actual), degrees F


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


def apply_calibration(
    g: Gaussian,
    cal: Calibration | None,
    *,
    min_sigma: float,
    min_samples: int = MIN_CAL_SAMPLES,
) -> tuple[Gaussian, bool]:
    """Return (corrected gaussian, calibrated?).

    Falls back to a widened raw spread with calibrated=False when the cell is
    missing or has too few pairs to trust.
    """
    if cal is None or cal.n_samples < min_samples:
        widened = max(g.sigma * UNCALIBRATED_WIDEN, min_sigma)
        return Gaussian(mu=g.mu, sigma=widened), False
    mu = g.mu - cal.bias
    pred_var = cal.var_a + cal.var_b * g.sigma**2
    sigma = max(sqrt(max(pred_var, 0.0)), min_sigma)
    return Gaussian(mu=mu, sigma=sigma), True
