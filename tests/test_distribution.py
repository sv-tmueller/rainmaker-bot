from datetime import date

import numpy as np
import pytest

from rainmaker.forecasts.base import ForecastSample
from rainmaker.probability.distribution import fit_gaussian


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
    # Deterministic samples cluster at 75 (tight), ensemble members spread wider.
    # sigma must come from ensemble member spread, not the whole pool.
    det = [_sample(74.9), _sample(75.0), _sample(75.1)]  # tight std ~0.1
    ens = [_ens(70.0 + i * 0.5, i + 1) for i in range(10)]  # spread ~1.5
    ens_vals = [s.value_f for s in ens]
    expected_sigma = float(np.std(ens_vals, ddof=1))
    g = fit_gaussian(det + ens, min_sigma=0.0)
    # mu uses all samples: holds here because det and ens are one pooled group
    all_vals = [s.value_f for s in det + ens]
    assert g.mu == pytest.approx(float(np.mean(all_vals)))
    # sigma uses ensemble members only
    assert g.sigma == pytest.approx(expected_sigma)


def test_fit_gaussian_ensemble_sigma_differs_from_pooled_std():
    # This test proves the behavior changed: pooled std != ensemble-only std.
    # With tight deterministic samples pulling the spread down, pooled would be lower.
    det = [_sample(75.0), _sample(75.0), _sample(75.0)]  # zero det variance
    ens = [_ens(70.0 + i * 1.0, i + 1) for i in range(5)]  # ens std ~1.58
    ens_vals = [s.value_f for s in ens]
    ens_std = float(np.std(ens_vals, ddof=1))
    all_vals = [s.value_f for s in det + ens]
    pooled_std = float(np.std(all_vals, ddof=1))
    # The two stds differ (pooled is diluted by the tight det cluster)
    assert ens_std != pytest.approx(pooled_std)
    g = fit_gaussian(det + ens, min_sigma=0.0)
    # Result should use ensemble std, not pooled std
    assert g.sigma == pytest.approx(ens_std)


def test_fit_gaussian_single_ensemble_member_uses_floor():
    # One ensemble member + one det sample: ensemble std is 0 (single member),
    # so the floor should apply.
    samples = [_sample(75.0), _ens(76.0, 1)]
    g = fit_gaussian(samples, min_sigma=1.5)
    # mu from all samples
    assert g.mu == pytest.approx(75.5)
    # ensemble std with 1 member is undefined; floor applies
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
