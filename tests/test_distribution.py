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
    # mu uses all samples
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
