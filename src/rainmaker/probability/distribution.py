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


def inter_family_gap(samples: list[ForecastSample]) -> dict[str, float | None]:
    """Diagnostic for #400: deterministic-vs-ensemble dispersion the fit ignores.

    fit_gaussian's ensemble-arm sigma counts only BETWEEN-ensemble-group
    dispersion: the gap between the deterministic models (NWS, the seamless
    deterministic models) and the ensemble consensus contributes nothing. K
    deterministic models that agree with each other but sit far from the
    ensemble mean therefore produce a confidently narrow sigma.

    Reports:
    - det_consensus / ens_consensus: equal-weight mean of the family's
      per-group means (same weighting fit_gaussian uses for mu).
    - gap: det_consensus - ens_consensus, in degrees.
    - current_sigma: what fit_gaussian's ensemble arm yields (before flooring).
    - extended_sigma: the mixture variance if deterministic groups joined the
      mixture as additional components (equal weight per group, v_k = within-
      group sample variance, 0 for singletons). This is what the fit WOULD
      give under the #400 extension, computable without changing anything.
    - ratio: |gap| / current_sigma when both exist; the actionable number
      (how many sigmas the families disagree by).

    Either family missing (or a single group overall) yields None for the
    cross-family fields: there is no gap to speak of.
    """
    if not samples:
        raise ValueError("cannot measure dispersion with no samples")
    groups: dict[tuple[str, str], list[float]] = defaultdict(list)
    for s in samples:
        groups[_group_key(s)].append(s.value_f)
    # Classify each (source, model) group by whether it carries ensemble members.
    det_means: list[float] = []
    ens_means: list[float] = []
    ens_group_vals: list[list[float]] = []
    for key, vals in groups.items():
        has_members = any(s.member is not None for s in samples if _group_key(s) == key)
        if has_members:
            ens_means.append(float(np.mean(vals)))
            ens_group_vals.append(vals)
        else:
            det_means.append(float(np.mean(vals)))

    out: dict[str, float | None] = {
        "det_consensus": None,
        "ens_consensus": None,
        "gap": None,
        "current_sigma": None,
        "extended_sigma": None,
        "ratio": None,
    }
    ens_present = bool(ens_means)
    if ens_present:
        m_e = np.array(ens_means, dtype=float)
        v_e = np.array(
            [float(np.var(vals, ddof=1)) if len(vals) >= 2 else 0.0 for vals in ens_group_vals],
            dtype=float,
        )
        current_sigma = float(np.sqrt(np.mean(v_e + (m_e - m_e.mean()) ** 2)))
        out["ens_consensus"] = float(m_e.mean())
        out["current_sigma"] = current_sigma
    if det_means:
        m_d = np.array(det_means, dtype=float)
        out["det_consensus"] = float(m_d.mean())
    if det_means and ens_present:
        gap = (out["det_consensus"] or 0.0) - (out["ens_consensus"] or 0.0)
        out["gap"] = gap
        cs = out["current_sigma"]
        if cs is not None and cs > 0:
            out["ratio"] = abs(gap) / cs
        # Extended mixture: every group (det + ens) joins as one component.
        all_means: list[float] = []
        all_vars: list[float] = []
        for vals in groups.values():
            all_means.append(float(np.mean(vals)))
            all_vars.append(float(np.var(vals, ddof=1)) if len(vals) >= 2 else 0.0)
        am = np.array(all_means, dtype=float)
        av = np.array(all_vars, dtype=float)
        out["extended_sigma"] = float(np.sqrt(np.mean(av + (am - am.mean()) ** 2)))
    return out
