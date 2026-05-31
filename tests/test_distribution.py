from datetime import date

import numpy as np
import pytest

from rainmaker.forecasts.base import ForecastSample
from rainmaker.probability.distribution import fit_gaussian


def _sample(value_f: float) -> ForecastSample:
    return ForecastSample(
        source="x",
        model="m",
        member=None,
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
