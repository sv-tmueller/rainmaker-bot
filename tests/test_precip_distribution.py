import pytest
from scipy.stats import gamma as _gamma

from rainmaker.probability.precip_distribution import fit_gamma


def test_fit_gamma_recovers_known_mean_and_var():
    g = fit_gamma(4.0, 2.0, floor=0.01)
    assert g.k == pytest.approx(8.0)  # mean^2 / var
    assert g.scale == pytest.approx(0.5)  # var / mean
    assert _gamma(a=g.k, scale=g.scale).mean() == pytest.approx(4.0)
    assert _gamma(a=g.k, scale=g.scale).var() == pytest.approx(2.0)
    assert g.degenerate is False


def test_fit_gamma_applies_variance_floor():
    g = fit_gamma(4.0, 1e-9, floor=0.01)
    assert g.scale == pytest.approx(0.01 / 4.0)
    assert g.k == pytest.approx(4.0**2 / 0.01)


def test_fit_gamma_dry_month_is_degenerate():
    assert fit_gamma(0.0, 0.5, floor=0.01).degenerate is True
    assert fit_gamma(-1.0, 0.5, floor=0.01).degenerate is True
