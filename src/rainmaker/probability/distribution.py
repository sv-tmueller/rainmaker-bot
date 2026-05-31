import numpy as np
from pydantic import BaseModel

from rainmaker.config import MIN_SIGMA_F
from rainmaker.forecasts.base import ForecastSample


class Gaussian(BaseModel):
    mu: float
    sigma: float


def fit_gaussian(samples: list[ForecastSample], min_sigma: float = MIN_SIGMA_F) -> Gaussian:
    """Fit an uncalibrated Gaussian to the pooled sample values.

    Equal-weight mean and sample std (ddof=1), with sigma floored at min_sigma so a
    low-variance pool cannot produce false certainty. Spread is knowingly
    overconfident here; the bias/spread correction is Phase 4.
    """
    if not samples:
        raise ValueError("cannot fit a distribution with no samples")
    values = np.array([s.value_f for s in samples], dtype=float)
    mu = float(values.mean())
    sigma = float(values.std(ddof=1)) if values.size >= 2 else 0.0
    return Gaussian(mu=mu, sigma=max(sigma, min_sigma))
