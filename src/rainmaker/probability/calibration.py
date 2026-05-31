"""Per-(station, variable, lead_time) bias and spread-scale correction.

Fit from historical forecast-vs-actual pairs, then applied to the raw predictive
Gaussian. Raw ensemble spread is reliably overconfident, so spread_scale rescales
it while keeping the day-to-day width signal. Until a cell has enough pairs we do
not trust the fit and fall back to a conservatively widened raw spread.
"""

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from rainmaker.config import MIN_CAL_SAMPLES, UNCALIBRATED_WIDEN
from rainmaker.probability.distribution import Gaussian


class CalibrationPair(BaseModel):
    model_config = ConfigDict(frozen=True)

    mu: float  # raw forecast mean
    sigma: float = Field(gt=0)  # raw forecast sigma
    actual: float


class Calibration(BaseModel):
    model_config = ConfigDict(frozen=True)

    station: str
    variable: str
    lead_time: int
    bias: float
    spread_scale: float = Field(gt=0)
    n_samples: int


def fit_calibration(
    station: str, variable: str, lead_time: int, pairs: list[CalibrationPair]
) -> Calibration:
    """Fit bias (mean signed error) and spread_scale (rms standardized residual)."""
    if not pairs:
        raise ValueError("cannot fit calibration with no pairs")
    mu = np.array([p.mu for p in pairs])
    sigma = np.array([p.sigma for p in pairs])
    actual = np.array([p.actual for p in pairs])
    bias = float(np.mean(mu - actual))
    residual = actual - (mu - bias)
    spread_scale = float(np.sqrt(np.mean((residual / sigma) ** 2)))
    return Calibration(
        station=station,
        variable=variable,
        lead_time=lead_time,
        bias=bias,
        spread_scale=max(spread_scale, 1e-6),  # keep > 0 for degenerate (perfect-fit) cells
        n_samples=len(pairs),
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
    sigma = max(g.sigma * cal.spread_scale, min_sigma)
    return Gaussian(mu=mu, sigma=sigma), True
