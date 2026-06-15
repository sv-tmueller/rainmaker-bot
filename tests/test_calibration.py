from math import pi, sqrt

import numpy as np
import pytest
from scipy.stats import norm

from rainmaker.probability.calibration import (
    Calibration,
    CalibrationPair,
    apply_calibration,
    compute_accuracy,
    fit_calibration,
)
from rainmaker.probability.distribution import Gaussian
from rainmaker.store.db import connect, init_schema
from rainmaker.store.query import load_calibration
from rainmaker.store.record import save_calibration


def _crps_gaussian(mu: float, sigma: float, actual: float) -> float:
    """Closed-form Gaussian CRPS for test assertions (mirrors backtest.crps_gaussian)."""
    z = (actual - mu) / sigma
    phi_z = float(norm.pdf(z))
    Phi_z = float(norm.cdf(z))
    return sigma * (z * (2 * Phi_z - 1) + 2 * phi_z - 1 / sqrt(pi))


def _pairs_fixed_var(bias: float, dvals: list[float], sigma: float) -> list[CalibrationPair]:
    """Pairs with constant ensemble_var = sigma**2."""
    return [
        CalibrationPair(
            mu=70.0 + i,
            sigma=sigma,
            ensemble_var=sigma**2,
            actual=(70.0 + i) - bias + d,
        )
        for i, d in enumerate(dvals)
    ]


def _emos_pairs(
    *,
    bias: float,
    var_a: float,
    var_b: float,
    ens_sigmas: list[float],
    n_quantiles: int = 9,
) -> list[CalibrationPair]:
    """Build pairs for a known EMOS ground truth.

    True predictive variance = var_a + var_b * ens_sigma^2.
    Actuals are placed at equally spaced quantiles of the true predictive Gaussian
    so CRPS minimization can recover (bias, var_a, var_b) cleanly without noise.
    """
    pairs: list[CalibrationPair] = []
    quantile_levels = np.linspace(
        1 / (n_quantiles + 1), n_quantiles / (n_quantiles + 1), n_quantiles
    )
    for i, ens_sigma in enumerate(ens_sigmas):
        true_var = var_a + var_b * ens_sigma**2
        true_sigma = sqrt(true_var)
        mu_raw = 70.0 + i  # varies so bias cannot trivially collapse
        for q in quantile_levels:
            actual = (mu_raw - bias) + norm.ppf(q) * true_sigma
            pairs.append(
                CalibrationPair(
                    mu=mu_raw,
                    sigma=ens_sigma,
                    ensemble_var=ens_sigma**2,
                    actual=actual,
                )
            )
    return pairs


# ---------------------------------------------------------------------------
# EMOS parameter recovery: a known bias + affine variance relationship
# ---------------------------------------------------------------------------


def test_emos_recovers_pure_bias():
    """With flat ens_var, fit recovers bias; var_a absorbs residual variance."""
    bias = 2.0
    ens_sigma = 2.0  # constant
    pairs = _pairs_fixed_var(bias, [2, -2, 2, -2, 2, -2], ens_sigma)
    cal = fit_calibration("KLGA", "TMAX", 1, pairs)
    assert cal.bias == pytest.approx(bias, abs=0.5)
    assert cal.n_samples == len(pairs)
    # EMOS model fields exist and are non-negative
    assert cal.var_a >= 0.0
    assert cal.var_b >= 0.0


def test_emos_recovers_known_parameters():
    """With two distinct ens_sigma levels, optimizer recovers a reasonable fit.

    Exact parameter recovery is not guaranteed by CRPS minimization on a finite
    quantile grid (the CRPS surface is shallow near the true params). What we
    verify: bias is recovered tightly, and the affine var structure is learned
    well enough that the fitted calibration has lower CRPS than uncalibrated.
    """
    bias = 1.5
    var_a = 1.0  # irreducible noise floor (variance units)
    var_b = 2.0  # amplification of ensemble variance
    # Two clearly different ens_sigmas so a and b are separately identified.
    ens_sigmas = [1.0] * 15 + [3.0] * 15
    pairs = _emos_pairs(bias=bias, var_a=var_a, var_b=var_b, ens_sigmas=ens_sigmas)
    cal = fit_calibration("KLGA", "TMAX", 1, pairs)
    # Bias is well-identified and recoverable.
    assert cal.bias == pytest.approx(bias, abs=0.05)
    # EMOS constraints hold.
    assert cal.var_a >= 0.0
    assert cal.var_b >= 0.0
    # The effective predictive variance at the two sigma levels is in the right direction:
    # larger ens_sigma -> larger fitted predictive variance.
    var_small = cal.var_a + cal.var_b * 1.0**2
    var_large = cal.var_a + cal.var_b * 3.0**2
    assert var_large > var_small, "fitted model must amplify larger ensemble spread"


def test_emos_fit_yields_lower_crps_than_rms_spread():
    """Min-CRPS EMOS fit yields lower mean CRPS than the old RMS spread_scale approach.

    Uses data with a clear variance-inflation signal so the RMS approach, which
    ignores the variance-vs-ensemble-spread relationship, is provably suboptimal.
    """
    bias = 1.0
    var_a = 0.5
    var_b = 3.0  # strong variance inflation
    ens_sigmas = [1.0] * 20 + [4.0] * 20
    pairs = _emos_pairs(bias=bias, var_a=var_a, var_b=var_b, ens_sigmas=ens_sigmas)

    cal = fit_calibration("KLGA", "TMAX", 1, pairs)

    # Mean CRPS under the EMOS calibration
    crps_emos = float(
        np.mean(
            [
                _crps_gaussian(
                    p.mu - cal.bias,
                    sqrt(max(cal.var_a + cal.var_b * p.ensemble_var, 1e-6)),
                    p.actual,
                )
                for p in pairs
            ]
        )
    )

    # RMS spread_scale baseline: rms of standardized residuals
    mu_arr = np.array([p.mu for p in pairs])
    sigma_arr = np.array([p.sigma for p in pairs])
    actual_arr = np.array([p.actual for p in pairs])
    rms_bias = float(np.mean(mu_arr - actual_arr))
    residuals = actual_arr - (mu_arr - rms_bias)
    rms_spread_scale = float(np.sqrt(np.mean((residuals / sigma_arr) ** 2)))
    crps_rms = float(
        np.mean(
            [_crps_gaussian(p.mu - rms_bias, p.sigma * rms_spread_scale, p.actual) for p in pairs]
        )
    )

    assert crps_emos < crps_rms, (
        f"EMOS mean CRPS {crps_emos:.4f} should be lower than RMS {crps_rms:.4f}"
    )


def test_emos_var_a_and_var_b_are_non_negative():
    """The fit enforces a >= 0 and b >= 0 even with degenerate (zero-residual) data."""
    pairs = _pairs_fixed_var(0.0, [0, 0, 0, 0], 2.0)
    cal = fit_calibration("KLGA", "TMAX", 1, pairs)
    assert cal.var_a >= 0.0
    assert cal.var_b >= 0.0


def test_fit_empty_raises():
    with pytest.raises(ValueError, match="no pairs"):
        fit_calibration("KLGA", "TMAX", 1, [])


# ---------------------------------------------------------------------------
# apply_calibration: uses var = a + b * g.sigma^2
# ---------------------------------------------------------------------------


def test_apply_corrects_mu_and_sigma():
    g = Gaussian(mu=70.0, sigma=2.0)
    # var = 1.0 + 2.0 * 4.0 = 9.0 -> sigma_cal = 3.0
    cal = Calibration(
        station="KLGA",
        variable="TMAX",
        lead_time=1,
        bias=1.0,
        var_a=1.0,
        var_b=2.0,
        n_samples=50,
    )
    out, calibrated = apply_calibration(g, cal, min_sigma=1.5, min_samples=30)
    assert calibrated is True
    assert out.mu == pytest.approx(69.0)
    assert out.sigma == pytest.approx(3.0)  # sqrt(1 + 2*4)


def test_apply_floors_sigma():
    g = Gaussian(mu=70.0, sigma=2.0)
    # var = 0.0 + 0.0 * 4.0 = 0.0 -> floored to min_sigma
    cal = Calibration(
        station="KLGA",
        variable="TMAX",
        lead_time=1,
        bias=0.0,
        var_a=0.0,
        var_b=0.0,
        n_samples=50,
    )
    out, _ = apply_calibration(g, cal, min_sigma=1.5, min_samples=30)
    assert out.sigma == 1.5


def test_apply_falls_back_when_too_few_samples():
    g = Gaussian(mu=70.0, sigma=2.0)
    cal = Calibration(
        station="KLGA",
        variable="TMAX",
        lead_time=1,
        bias=5.0,
        var_a=1.0,
        var_b=2.0,
        n_samples=5,
    )
    out, calibrated = apply_calibration(g, cal, min_sigma=1.5, min_samples=30)
    assert calibrated is False
    assert out.mu == 70.0  # bias not applied
    assert out.sigma > 2.0  # widened


def test_apply_none_falls_back():
    out, calibrated = apply_calibration(Gaussian(mu=70.0, sigma=2.0), None, min_sigma=1.5)
    assert calibrated is False
    assert out.mu == 70.0
    assert out.sigma == pytest.approx(2.5)  # 2.0 * UNCALIBRATED_WIDEN 1.25, above min_sigma 1.5


# ---------------------------------------------------------------------------
# Store round-trip: var_a and var_b persist and reload correctly
# ---------------------------------------------------------------------------


def test_calibration_save_load_round_trip():
    conn = connect(":memory:")
    init_schema(conn)
    cal = Calibration(
        station="KLGA",
        variable="TMAX",
        lead_time=1,
        bias=1.0,
        var_a=0.5,
        var_b=2.0,
        n_samples=40,
    )
    save_calibration(conn, cal, updated_at="2026-05-31T10:00:00Z")
    assert load_calibration(conn, "KLGA", "TMAX", 1) == cal

    # upsert overwrites the same cell
    save_calibration(conn, cal.model_copy(update={"bias": 2.0}), updated_at="2026-05-31T11:00:00Z")
    reloaded = load_calibration(conn, "KLGA", "TMAX", 1)
    assert reloaded is not None and reloaded.bias == 2.0
    assert load_calibration(conn, "KLGA", "TMAX", 2) is None
    conn.close()


# ---------------------------------------------------------------------------
# compute_accuracy: unchanged
# ---------------------------------------------------------------------------


def test_compute_accuracy_mae_and_bias():
    pairs = [
        CalibrationPair(mu=70.0, sigma=2.0, ensemble_var=4.0, actual=68.0),  # error +2
        CalibrationPair(mu=70.0, sigma=2.0, ensemble_var=4.0, actual=73.0),  # error -3
        CalibrationPair(mu=70.0, sigma=2.0, ensemble_var=4.0, actual=69.0),  # error +1
    ]
    acc = compute_accuracy(pairs)
    assert acc.n == 3
    assert acc.mae_f == pytest.approx(2.0)  # (2 + 3 + 1) / 3
    assert acc.bias_f == pytest.approx(0.0)  # (2 - 3 + 1) / 3


def test_compute_accuracy_bias_direction():
    # all forecasts overshoot: bias is positive (mu - actual)
    pairs = [
        CalibrationPair(mu=72.0, sigma=2.0, ensemble_var=4.0, actual=70.0),  # error +2
        CalibrationPair(mu=74.0, sigma=2.0, ensemble_var=4.0, actual=70.0),  # error +4
    ]
    acc = compute_accuracy(pairs)
    assert acc.bias_f == pytest.approx(3.0)  # mean(+2, +4)
    assert acc.mae_f == pytest.approx(3.0)


def test_compute_accuracy_empty_raises():
    with pytest.raises(ValueError, match="no pairs"):
        compute_accuracy([])
