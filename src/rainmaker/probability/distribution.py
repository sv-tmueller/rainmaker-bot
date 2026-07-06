from collections import defaultdict

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from rainmaker.config import MIN_SIGMA_F
from rainmaker.forecasts.base import ForecastSample


class Gaussian(BaseModel):
    model_config = ConfigDict(frozen=True)

    mu: float
    sigma: float = Field(gt=0)


def _group_key(sample: ForecastSample) -> tuple[str, str]:
    return (sample.source, sample.model)


def fit_gaussian(samples: list[ForecastSample], min_sigma: float = MIN_SIGMA_F) -> Gaussian:
    """Fit an uncalibrated Gaussian to the pooled sample values.

    mu: equal weight per (source, model) group, not per sample (see #239). A
    single NWS run and a 30-member ensemble are each one unit of independent
    evidence; averaging by sample would let the ensemble's member count drown
    out NWS. mu is the mean of the per-group means.

    sigma: when ensemble members (member is not None) are present, sigma comes
    from ensemble spread, not inter-model disagreement (inter-model stdev is
    only model disagreement and structurally under-disperses, see #98). With
    multiple ensemble groups (K > 1, e.g. GFS-ens and ECMWF-ens), sigma is the
    equal-weight mixture variance across those groups: for group k with mean
    m_k and within-group sample variance v_k, and m_bar the mean of the m_k,
    sigma^2 = mean_k[v_k + (m_k - m_bar)^2]. This keeps a large-membership
    ensemble from outweighing a smaller one in the spread, the same fix as mu.
    K=1 reduces to that group's own member std (today's single-ensemble
    behavior); a 1-member group contributes v_k=0. When no ensemble members
    exist (backfill/backtest path), sigma falls back to the pooled std,
    unchanged from before.

    sigma is floored at min_sigma so a low-variance pool cannot produce false
    certainty. The bias/spread correction is Phase 4.
    """
    if not samples:
        raise ValueError("cannot fit a distribution with no samples")
    groups: dict[tuple[str, str], list[float]] = defaultdict(list)
    for s in samples:
        groups[_group_key(s)].append(s.value_f)
    group_means = np.array([np.mean(vals) for vals in groups.values()], dtype=float)
    mu = float(group_means.mean())

    ens_groups: dict[tuple[str, str], list[float]] = defaultdict(list)
    for s in samples:
        if s.member is not None:
            ens_groups[_group_key(s)].append(s.value_f)

    if ens_groups:
        m_k = np.array([np.mean(vals) for vals in ens_groups.values()], dtype=float)
        v_k = np.array(
            [np.var(vals, ddof=1) if len(vals) >= 2 else 0.0 for vals in ens_groups.values()],
            dtype=float,
        )
        m_bar = m_k.mean()
        sigma = float(np.sqrt(np.mean(v_k + (m_k - m_bar) ** 2)))
    else:
        values = np.array([s.value_f for s in samples], dtype=float)
        sigma = float(values.std(ddof=1)) if values.size >= 2 else 0.0
    return Gaussian(mu=mu, sigma=max(sigma, min_sigma))
