import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from rainmaker.config import MIN_SIGMA_F
from rainmaker.forecasts.base import ForecastSample


class Gaussian(BaseModel):
    model_config = ConfigDict(frozen=True)

    mu: float
    sigma: float = Field(gt=0)


def fit_gaussian(samples: list[ForecastSample], min_sigma: float = MIN_SIGMA_F) -> Gaussian:
    """Fit an uncalibrated Gaussian to the pooled sample values.

    mu: equal-weight mean over all samples.

    sigma: when ensemble members (member is not None) are present, sigma is the
    sample std of the ensemble member values only. Ensemble spread is a genuine
    measure of forecast uncertainty; inter-model stdev is only model disagreement
    and structurally under-disperses (see #98). When no ensemble members exist
    (backfill/backtest path), sigma falls back to the pooled std.

    sigma is floored at min_sigma so a low-variance pool cannot produce false
    certainty. The bias/spread correction is Phase 4.
    """
    if not samples:
        raise ValueError("cannot fit a distribution with no samples")
    values = np.array([s.value_f for s in samples], dtype=float)
    mu = float(values.mean())
    ens_values = np.array([s.value_f for s in samples if s.member is not None], dtype=float)
    if ens_values.size >= 2:
        sigma = float(ens_values.std(ddof=1))
    elif ens_values.size == 1:
        sigma = 0.0  # single ensemble member: floor applies
    else:
        sigma = float(values.std(ddof=1)) if values.size >= 2 else 0.0
    return Gaussian(mu=mu, sigma=max(sigma, min_sigma))
