from datetime import date

import numpy as np
import pytest

from rainmaker.config import MIN_SIGMA_F
from rainmaker.forecasts.base import ForecastSample
from rainmaker.probability.distribution import (
    fit_gaussian,
    inter_family_gap,
    mixture_sigma,
)


def _sample(value_f: float, member: int | None = None) -> ForecastSample:
    return ForecastSample(
        source="x",
        model="m",
        member=member,
        station="KLGA",
        variable="TMAX",
        target_date=date(2026, 5, 31),
        lead_time_days=1,
        value_f=value_f,
        issued_at=None,
    )


def _ens(value_f: float, member: int) -> ForecastSample:
    return _sample(value_f, member=member)


def _grp(value_f: float, source: str, model: str, member: int | None = None) -> ForecastSample:
    return ForecastSample(
        source=source,
        model=model,
        member=member,
        station="KLGA",
        variable="TMAX",
        target_date=date(2026, 5, 31),
        lead_time_days=1,
        value_f=value_f,
        issued_at=None,
    )


def test_fit_gaussian_mean_and_std():
    g = fit_gaussian([_sample(68), _sample(70), _sample(72)], min_sigma=0.5)
    assert g.mu == pytest.approx(70.0)
    assert g.sigma == pytest.approx(2.0)  # sample std (ddof=1) of 68,70,72


def test_fit_gaussian_applies_sigma_floor():
    g = fit_gaussian([_sample(70.0), _sample(70.1)], min_sigma=1.5)
    assert g.mu == pytest.approx(70.05)
    assert g.sigma == 1.5  # raw std ~0.07 floored to 1.5


def test_fit_gaussian_single_sample_uses_floor():
    g = fit_gaussian([_sample(70.0)], min_sigma=1.5)
    assert g.mu == 70.0
    assert g.sigma == 1.5


def test_fit_gaussian_empty_raises():
    with pytest.raises(ValueError, match="no samples"):
        fit_gaussian([], min_sigma=1.5)


def test_fit_gaussian_realistic_mixed_pool():
    values = [68.0, 69.5, 70.0, 70.2, 70.8, 71.0, 71.3, 72.0, 69.0, 70.5]
    g = fit_gaussian([_sample(v) for v in values], min_sigma=0.5)
    assert g.mu == pytest.approx(float(np.mean(values)))
    assert g.sigma == pytest.approx(float(np.std(values, ddof=1)))


# ---------------------------------------------------------------------------
# TDD: ensemble-member spread (new behavior added in #98)
# ---------------------------------------------------------------------------


def test_fit_gaussian_uses_ensemble_sigma_when_members_present():
    # Deterministic samples cluster at 75 (own group), ensemble members spread
    # wider (second group). Since #426 the mixed pool's sigma is the extended
    # mixture over all groups: the det group joins as a singleton component,
    # widening beyond the pure ensemble member spread. Ens-only sigma is
    # asserted by test_fit_gaussian_ens_only_pool_keeps_between_group_sigma.
    det = [_grp(74.9, "nws", "nws"), _grp(75.0, "nws", "nws"), _grp(75.1, "nws", "nws")]
    ens = [_ens(70.0 + i * 0.5, i + 1) for i in range(10)]  # spread ~1.5
    ens_vals = [s.value_f for s in ens]
    ens_std = float(np.std(ens_vals, ddof=1))
    g = fit_gaussian(det + ens, min_sigma=0.0)
    # mu weights the two groups equally (per #239).
    assert g.mu == pytest.approx((75.0 + 72.25) / 2)
    # sigma is the 2-component mixture: ens within-variance + the det-vs-ens
    # mean offset, strictly wider than the ens-only member std.
    assert g.sigma > ens_std
    diag = inter_family_gap(det + ens)
    assert diag["extended_sigma"] is not None
    assert g.sigma == pytest.approx(diag["extended_sigma"])


def test_fit_gaussian_ensemble_sigma_differs_from_pooled_std():
    # Neither pooled std nor ens-only std is the answer for a mixed pool. With
    # tight deterministic samples at 75 (own group) and a 5-member ensemble at
    # 70..74, the extended mixture sigma differs from both candidates.
    det = [_grp(75.0, "nws", "nws")] * 3  # zero det variance
    ens = [_ens(70.0 + i * 1.0, i + 1) for i in range(5)]  # ens std ~1.58
    ens_vals = [s.value_f for s in ens]
    ens_std = float(np.std(ens_vals, ddof=1))
    all_vals = [s.value_f for s in det + ens]
    pooled_std = float(np.std(all_vals, ddof=1))
    g = fit_gaussian(det + ens, min_sigma=0.0)
    # The candidate sigmas pairwise differ.
    assert ens_std != pytest.approx(pooled_std)
    # Result uses the extended mixture sigma: wider than the ens-only std and
    # distinct from the pooled std.
    assert g.sigma > ens_std
    assert g.sigma != pytest.approx(pooled_std)
    diag = inter_family_gap(det + ens)
    assert diag["extended_sigma"] is not None
    assert g.sigma == pytest.approx(diag["extended_sigma"])


def test_fit_gaussian_single_ensemble_member_uses_floor():
    # One ensemble member + one det sample: both groups are singletons with
    # zero within-group variance, so the extended mixture sigma is only the
    # between-mean offset (0.5F) and the floor still applies.
    samples = [_sample(75.0), _ens(76.0, 1)]
    g = fit_gaussian(samples, min_sigma=1.5)
    # mu from all samples
    assert g.mu == pytest.approx(75.5)
    assert g.sigma == 1.5


def test_fit_gaussian_ensemble_sigma_floored():
    # Ensemble members very tight: floor overrides their spread.
    ens = [_ens(75.0, i + 1) for i in range(5)]  # identical values, std=0
    g = fit_gaussian(ens, min_sigma=1.5)
    assert g.mu == pytest.approx(75.0)
    assert g.sigma == 1.5


def test_fit_gaussian_without_ensemble_unchanged():
    # No ensemble members: behavior identical to original (pooled std).
    values = [72.0, 73.0, 74.0, 75.0, 76.0]
    samples = [_sample(v) for v in values]
    g = fit_gaussian(samples, min_sigma=0.5)
    assert g.mu == pytest.approx(float(np.mean(values)))
    assert g.sigma == pytest.approx(float(np.std(values, ddof=1)))


# ---------------------------------------------------------------------------
# TDD: per-model-group weighting (#239, stop diluting NWS / pooling correlated
# ensemble members as independent)
# ---------------------------------------------------------------------------


def test_fit_gaussian_mean_weights_by_group_not_sample():
    # 1 NWS sample at 80 plus a 30-member ensemble centered at 70 (alternating
    # +/-0.5 so the group has real spread) must average the two groups
    # (mu = 75), not weight by sample count (mu ~ 70.3).
    nws = _grp(80.0, "nws", "nws")
    ens = [
        _grp(70.0 + (0.5 if i % 2 == 0 else -0.5), "open-meteo", "gfs_ens", member=i + 1)
        for i in range(30)
    ]
    g = fit_gaussian([nws] + ens, min_sigma=0.5)
    naive_sample_weighted_mean = (80.0 + 30 * 70.0) / 31
    assert g.mu == pytest.approx(75.0)
    assert g.mu != pytest.approx(naive_sample_weighted_mean)


def test_fit_gaussian_mean_is_average_of_three_group_means():
    # Three groups: NWS (mean 71), an Open-Meteo deterministic model (mean 80),
    # and an Open-Meteo ensemble (mean 63). mu is the mean of the three group
    # means, not the mean of all 6 pooled samples.
    nws = [_grp(70.0, "nws", "nws"), _grp(72.0, "nws", "nws")]
    det = [_grp(80.0, "open-meteo", "gfs")]
    ens = [
        _grp(v, "open-meteo", "ecmwf_ens", member=i + 1) for i, v in enumerate([60.0, 63.0, 66.0])
    ]
    g = fit_gaussian(nws + det + ens, min_sigma=0.0)
    assert g.mu == pytest.approx((71.0 + 80.0 + 63.0) / 3)


def test_fit_gaussian_sigma_equal_weights_ensemble_groups():
    # Two ensemble groups of unequal member count and spread. sigma^2 is the
    # equal-weight mixture variance over the two groups (not the member-count-
    # weighted pooled std of all 8 members).
    ens1 = [
        _grp(v, "open-meteo", "gfs_ens", member=i + 1) for i, v in enumerate([70.0, 71.0, 72.0])
    ]
    ens2 = [
        _grp(v, "open-meteo", "ecmwf_ens", member=i + 1)
        for i, v in enumerate([60.0, 65.0, 70.0, 75.0, 80.0])
    ]
    g = fit_gaussian(ens1 + ens2, min_sigma=0.0)
    # m1=71, v1=1; m2=70, v2=62.5; m_bar=70.5
    # sigma^2 = 0.5 * [(1 + 0.25) + (62.5 + 0.25)] = 32.0
    expected_sigma = 32.0**0.5
    all_member_values = [70.0, 71.0, 72.0, 60.0, 65.0, 70.0, 75.0, 80.0]
    pooled_std = float(np.std(all_member_values, ddof=1))
    assert expected_sigma != pytest.approx(pooled_std)
    assert g.sigma == pytest.approx(expected_sigma)
    assert g.sigma != pytest.approx(pooled_std)


# ---------------------------------------------------------------------------
# #400/#426: fit_gaussian extends the ensemble arm's sigma with the
# deterministic-vs-ensemble disagreement (the inter_family_gap counterfactual)
# ---------------------------------------------------------------------------


def test_fit_gaussian_extended_sigma_with_both_families():
    """Mixed det+ens pool: sigma equals inter_family_gap's extended_sigma, the
    mixture variance over ALL groups (det groups join as singleton components),
    not the ens-only between-group sigma."""
    dets = [
        _grp(80.0, "nws", "nws"),
        _grp(79.0, "open-meteo", "gfs"),
        _grp(81.0, "open-meteo", "icon"),
    ]
    ens = [
        _grp(70.0 + (0.5 if i % 2 == 0 else -0.5), "open-meteo", "gfs_ens", member=i + 1)
        for i in range(10)
    ]
    pool = dets + ens
    g = fit_gaussian(pool, min_sigma=0.0)
    ext = inter_family_gap(pool)["extended_sigma"]
    assert ext is not None
    assert g.sigma == pytest.approx(ext)
    # And it strictly exceeds the old ens-only sigma.
    assert g.sigma > inter_family_gap(pool)["current_sigma"]


def test_fit_gaussian_extended_sigma_flags_confidently_wrong_det_block():
    """The scenario pinned by test_inter_family_gap_flags_confidently_wrong_
    det_block: five deterministic models agree tightly at 80F while the
    ensemble sits at 70F. The old fit saw only the ~1F ensemble spread; the
    extended fit must widen sigma enough to cover the family disagreement."""
    dets = [_grp(80.0, "open-meteo", f"det{i}") for i in range(5)]
    ens = [
        _grp(70.0 + (0.5 if i % 2 == 0 else -0.5), "open-meteo", "gfs_ens", member=i + 1)
        for i in range(10)
    ]
    pool = dets + ens
    g = fit_gaussian(pool, min_sigma=0.0)
    diag = inter_family_gap(pool)
    assert diag["extended_sigma"] is not None
    assert diag["current_sigma"] is not None
    assert g.sigma == pytest.approx(diag["extended_sigma"])
    assert g.sigma > diag["current_sigma"] * 3


def test_fit_gaussian_extension_barely_moves_aligned_families():
    """Small agreement case (mirrors test_inter_family_gap_small_agreement_
    case): det and ens consensuses nearly aligned, so the widened sigma stays
    close to the ens-only sigma."""
    dets = [_grp(70.2, "nws", "nws"), _grp(69.8, "open-meteo", "gfs")]
    ens = [
        _grp(70.0 + (0.5 if i % 2 == 0 else -0.5), "open-meteo", "gfs_ens", member=i + 1)
        for i in range(10)
    ]
    pool = dets + ens
    g = fit_gaussian(pool, min_sigma=0.0)
    cur = inter_family_gap(pool)["current_sigma"]
    assert cur is not None
    assert g.sigma == pytest.approx(cur, rel=0.35)


def test_fit_gaussian_ens_only_pool_keeps_between_group_sigma():
    """No deterministic groups: the extension degenerates to today's
    between-ensemble-group mixture sigma (unchanged behavior)."""
    ens1 = [
        _grp(v, "open-meteo", "gfs_ens", member=i + 1) for i, v in enumerate([70.0, 71.0, 72.0])
    ]
    ens2 = [
        _grp(v, "open-meteo", "ecmwf_ens", member=i + 1)
        for i, v in enumerate([60.0, 65.0, 70.0, 75.0, 80.0])
    ]
    g = fit_gaussian(ens1 + ens2, min_sigma=0.0)
    # m1=71, v1=1; m2=70, v2=62.5; m_bar=70.5
    # sigma^2 = 0.5 * [(1 + 0.25) + (62.5 + 0.25)] = 32.0
    assert g.sigma == pytest.approx(32.0**0.5)


def test_fit_gaussian_det_only_pool_stays_pooled_std():
    """Backfill/backtest path: no ensemble members anywhere, so sigma stays
    the pooled std (extension does not engage)."""
    values = [72.0, 73.0, 74.0, 75.0, 76.0]
    samples = [_sample(v) for v in values]
    g = fit_gaussian(samples, min_sigma=0.5)
    assert g.sigma == pytest.approx(float(np.std(values, ddof=1)))


# ---------------------------------------------------------------------------
# #400 diagnostic: deterministic-vs-ensemble dispersion gap the fit ignores
# ---------------------------------------------------------------------------


def test_inter_family_gap_flags_confidently_wrong_det_block():
    """Five deterministic models agree tightly at 80F while the ensemble sits
    at 70F with 1F spread: the fit's sigma sees only the ensemble (~1), the
    diagnostic must reveal the 10-degree family gap (~10 sigma)."""
    dets = [_grp(80.0, "open-meteo", f"det{i}") for i in range(5)]
    ens = [
        _grp(70.0 + (0.5 if i % 2 == 0 else -0.5), "open-meteo", "gfs_ens", member=i + 1)
        for i in range(10)
    ]
    diag = inter_family_gap(dets + ens)
    assert diag["det_consensus"] == pytest.approx(80.0)
    assert diag["ens_consensus"] == pytest.approx(70.0)
    assert diag["gap"] == pytest.approx(10.0)
    assert diag["current_sigma"] == pytest.approx(0.5, abs=0.1)
    assert diag["ratio"] == pytest.approx(20.0, rel=0.3)
    # Extension: joining the five det groups into the mixture widens sigma a lot.
    assert diag["extended_sigma"] is not None
    assert diag["extended_sigma"] > diag["current_sigma"] * 3


def test_inter_family_gap_none_without_cross_family_overlap():
    """Ensemble-only or deterministic-only pools have no cross-family gap."""
    ens = [_grp(70.0, "open-meteo", "gfs_ens", member=i + 1) for i in range(4)]
    diag = inter_family_gap(ens)
    assert diag["gap"] is None and diag["ratio"] is None
    assert diag["det_consensus"] is None
    assert diag["current_sigma"] is not None

    dets = [_grp(70.0, "nws", "nws")]
    diag2 = inter_family_gap(dets)
    assert diag2["gap"] is None
    assert diag2["current_sigma"] is None  # no ensemble arm engaged


def test_inter_family_gap_small_agreement_case():
    """Deterministic and ensemble consensuses nearly aligned: gap ~ 0, the
    extension barely moves sigma (the fit's blindness is harmless here)."""
    dets = [_grp(70.2, "nws", "nws"), _grp(69.8, "open-meteo", "gfs")]
    ens = [
        _grp(70.0 + (0.5 if i % 2 == 0 else -0.5), "open-meteo", "gfs_ens", member=i + 1)
        for i in range(10)
    ]
    diag = inter_family_gap(dets + ens)
    assert diag["gap"] == pytest.approx(0.0, abs=0.5)
    assert diag["ratio"] == pytest.approx(0.0, abs=0.5)


# ---------------------------------------------------------------------------
# #462: one canonical sigma statistic. Parity between the apply side
# (fit_gaussian) and the shared mixture_sigma helper the fit side consumes.
# ---------------------------------------------------------------------------


def test_mixture_sigma_matches_fit_gaussian_exactly():
    """Identical synthetic groups through fit_gaussian and through the shared
    mixture_sigma helper must yield the same sigma (#462 acceptance criterion
    1): the helper IS the fit-side pair builder's statistic, so equality here
    is the fit-vs-apply parity assertion."""
    pool = [
        _grp(80.0, "nws", "nws"),
        _grp(79.0, "open-meteo", "gfs"),
        _grp(81.0, "open-meteo", "icon"),
    ] + [
        _grp(70.0 + (0.5 if i % 2 == 0 else -0.5), "open-meteo", "gfs_ens", member=i + 1)
        for i in range(10)
    ]
    g = fit_gaussian(pool, min_sigma=0.0)
    groups: dict[tuple[str, str], list[float]] = {}
    for s in pool:
        groups.setdefault((s.source, s.model), []).append(s.value_f)
    assert g.sigma == pytest.approx(mixture_sigma(groups))


def test_mixture_sigma_det_only_is_population_std():
    """Det-only pools (the archive fallback's shape): the mixture over
    singleton groups degenerates to the population std of the model scalars,
    ddof=0, replacing the archive path's old ddof=1 stdev (#462)."""
    scalars = [72.0, 74.0, 79.0]
    groups = {("open-meteo", f"m{i}"): [v] for i, v in enumerate(scalars)}
    assert mixture_sigma(groups) == pytest.approx(float(np.std(scalars, ddof=0)))


def test_mixture_sigma_known_two_component_value():
    """Two groups, hand-computed: m1=71,v1=1; m2=70,v2=62.5; m_bar=70.5;
    sigma^2 = 0.5*[(1+0.25)+(62.5+0.25)] = 32.0 (pins the helper to the same
    value the ens-only pin below asserts through fit_gaussian)."""
    groups = {
        ("open-meteo", "gfs_ens"): [70.0, 71.0, 72.0],
        ("open-meteo", "ecmwf_ens"): [60.0, 65.0, 70.0, 75.0, 80.0],
    }
    assert mixture_sigma(groups) == pytest.approx(32.0**0.5)


def test_canonical_statistic_floor_constant_shared():
    """The floor applied on BOTH sides is MIN_SIGMA_F, not the old archive
    1e-6: the unified pair builders must floor with this constant."""
    assert MIN_SIGMA_F == 1.5
