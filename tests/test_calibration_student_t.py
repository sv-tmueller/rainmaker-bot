"""TDD for the Student-t family core ported from the frozen spike (#284/#291).

The numeric engine (numeric_crps, std_cdf_for, FIT_MIN_SIGMA) is a faithful port
of src/rainmaker/spikes/tail_objective.py: same widened-grid truncation-trap fix,
same sigma floor. Do not re-derive; if a number here disagrees with the spike's
own sanity tests (tests/test_tail_objective_spike.py), the port has a bug.
"""

import math

import numpy as np
import pytest
from scipy.stats import norm
from scipy.stats import t as student_t

from rainmaker.probability.calibration import (
    FIT_MIN_SIGMA,
    CalibrationPair,
    fit_calibration,
    fit_student_t_free_df,
    numeric_crps,
    std_cdf_for,
)

# ---------------------------------------------------------------------------
# std_cdf_for
# ---------------------------------------------------------------------------


def test_std_cdf_for_none_is_standard_normal():
    cdf = std_cdf_for(None)
    u = np.array([-1.0, 0.0, 1.5])
    assert np.allclose(cdf(u), norm.cdf(u))


def test_std_cdf_for_df_is_student_t():
    cdf = std_cdf_for(5.0)
    u = np.array([-1.0, 0.0, 1.5])
    assert np.allclose(cdf(u), student_t.cdf(u, 5.0))


# ---------------------------------------------------------------------------
# numeric_crps: validated against the closed-form Gaussian CRPS
# ---------------------------------------------------------------------------


def _crps_gaussian_closed_form(mu: float, sigma: float, actual: float) -> float:
    z = (actual - mu) / sigma
    phi_z = float(norm.pdf(z))
    Phi_z = float(norm.cdf(z))
    return sigma * (z * (2 * Phi_z - 1) + 2 * phi_z - 1 / math.sqrt(math.pi))


def test_numeric_crps_matches_closed_form_gaussian_crps():
    mu, sigma, actual = 70.0, 2.0, 71.3
    closed_form = _crps_gaussian_closed_form(mu, sigma, actual)
    numeric = float(numeric_crps(std_cdf_for(None), mu, sigma, actual)[0])
    assert numeric == pytest.approx(closed_form, abs=3e-3)


def test_numeric_crps_is_vectorized_over_batches():
    mu = np.array([70.0, 71.0])
    sigma = np.array([2.0, 3.0])
    actual = np.array([71.3, 68.0])
    result = numeric_crps(std_cdf_for(None), mu, sigma, actual)
    assert result.shape == (2,)
    for i in range(2):
        expected = _crps_gaussian_closed_form(float(mu[i]), float(sigma[i]), float(actual[i]))
        assert float(result[i]) == pytest.approx(expected, abs=3e-3)


# ---------------------------------------------------------------------------
# Truncation-trap fix: widened grid + FIT_MIN_SIGMA floor (coverage gap flagged
# by the tester on spike PR #285).
# ---------------------------------------------------------------------------


def test_numeric_crps_widens_grid_for_overconfident_sigma():
    """An actual far outside the default span (in standardized units) must not
    silently truncate to a near-zero score: the true CRPS as sigma -> 0 is
    |actual - mu|, and the grid must widen to capture that, not clip it.
    """
    mu, sigma, actual = 0.0, 0.01, 5.0  # standardized deviation = 500, default span=8
    result = float(numeric_crps(std_cdf_for(None), mu, sigma, actual)[0])
    assert math.isfinite(result)
    # Without widening, the truncated integral would collapse toward ~sigma (near
    # zero); the honest CRPS at this extreme z is close to |actual - mu|.
    assert result == pytest.approx(abs(actual - mu), rel=0.05)


def test_fit_student_t_free_df_stays_finite_when_variance_wants_to_collapse_to_zero():
    """A batch that would otherwise drive var_a/var_b toward zero (near-identical
    mu/actual pairs, near-zero ensemble variance) plus nonzero residuals must
    still yield a finite fit: this is the truncation-trap the FIT_MIN_SIGMA floor
    and the widened grid exist to prevent (see numeric_crps's docstring).
    """
    # Near-zero ensemble variance; residuals are small but nonzero, so an
    # unconstrained CRPS-minimizer would want to shrink the predictive sigma
    # toward zero to chase them.
    pairs = [
        CalibrationPair(mu=70.0, sigma=0.05, ensemble_var=1e-6, actual=70.0 + d)
        for d in [0.1, -0.1, 0.15, -0.05, 0.08, -0.12, 0.05, -0.07]
    ]
    cal = fit_student_t_free_df("KLGA", "TMIN", 1, pairs)
    assert math.isfinite(cal.bias)
    assert math.isfinite(cal.var_a)
    assert math.isfinite(cal.var_b)
    assert cal.df is not None and math.isfinite(cal.df)
    assert cal.var_a >= 0.0
    assert cal.var_b >= 0.0
    assert 2.0 < cal.df <= 62.0


def test_fit_min_sigma_matches_the_spike_floor():
    # FIT_MIN_SIGMA is a physical floor (degrees F) on the fitted predictive
    # sigma, ported unchanged from the spike (see tail_objective.py's own
    # FIT_MIN_SIGMA and its docstring on the truncation trap).
    assert FIT_MIN_SIGMA == 1.0


# ---------------------------------------------------------------------------
# fit_student_t_free_df: df in (2, 62], log(df - 2) parametrization
# ---------------------------------------------------------------------------


def test_fit_student_t_free_df_empty_raises():
    with pytest.raises(ValueError, match="no pairs"):
        fit_student_t_free_df("KLGA", "TMIN", 1, [])


def test_fit_student_t_free_df_returns_calibration_with_df_set():
    pairs = [
        CalibrationPair(mu=70.0 + i, sigma=2.0, ensemble_var=4.0, actual=70.0 + i - 1.0)
        for i in range(40)
    ]
    cal = fit_student_t_free_df("KLGA", "TMIN", 1, pairs)
    assert cal.station == "KLGA"
    assert cal.variable == "TMIN"
    assert cal.lead_time == 1
    assert cal.n_samples == len(pairs)
    assert cal.df is not None
    assert 2.0 < cal.df <= 62.0


def _t_quantile_pairs(
    *, bias: float, var_a: float, var_b: float, df: float, ens_sigmas: list[float], n_q: int = 9
) -> list[CalibrationPair]:
    """Noise-free Student-t quantile pairs, mirroring test_calibration.py's
    _emos_pairs Gaussian helper: places actuals at equally spaced quantiles of
    the true predictive t distribution so CRPS minimization can recover the
    parameters cleanly without sampling noise.
    """
    pairs: list[CalibrationPair] = []
    levels = np.linspace(1 / (n_q + 1), n_q / (n_q + 1), n_q)
    for i, ens_sigma in enumerate(ens_sigmas):
        true_var = var_a + var_b * ens_sigma**2
        true_sigma = math.sqrt(true_var)
        mu_raw = 70.0 + i
        for q in levels:
            actual = (mu_raw - bias) + float(student_t.ppf(q, df)) * true_sigma
            pairs.append(
                CalibrationPair(
                    mu=mu_raw, sigma=ens_sigma, ensemble_var=ens_sigma**2, actual=actual
                )
            )
    return pairs


def test_fit_student_t_free_df_recovers_a_heavy_tail_on_t_df5_noise():
    """Fitting on t(df=5)-shaped noise recovers a low (heavy-tailed) df, not a
    near-Gaussian one, per the acceptance criteria's t-recovery test.
    """
    pairs = _t_quantile_pairs(bias=1.0, var_a=1.0, var_b=1.0, df=5.0, ens_sigmas=[1.5] * 20)
    cal = fit_student_t_free_df("KLGA", "TMIN", 1, pairs)
    assert cal.df is not None
    # Heavy-tailed data should not fit as an essentially-Gaussian df (near the
    # 62 ceiling); allow headroom since this is a single noise-free split, not
    # an exact-recovery claim.
    assert cal.df < 30.0
    assert cal.bias == pytest.approx(1.0, abs=0.5)


def test_fit_student_t_free_df_improves_lower_tail_pit_on_tail_thin_tmin_construction():
    """A tail-thin (heavy-tailed) synthetic TMIN construction: the Student-t fit's
    lower-tail PIT ratio must land closer to 1.0 (honest) than the Gaussian
    fit's, while the body (mid-range PIT-centering error) does not blow up.

    "Tail-thin" construction: a Gaussian body contaminated with an 8% rate of
    cold-snap outliers (a genuinely fatter, more realistic lower tail than a
    pure Student-t noise-free ladder gives the CRPS-minimizing fitter, per
    empirical tuning against this exact fitter). Fit and eval draws are
    independent (different RNG seeds), same generating distribution.
    """
    true_sigma = 2.0

    def draw(n: int, seed: int) -> list[CalibrationPair]:
        rng = np.random.default_rng(seed)
        mus = 70.0 + rng.normal(0, 3, size=n)
        is_cold_snap = rng.random(n) < 0.08
        noise = np.where(
            is_cold_snap,
            rng.normal(-3.0 * true_sigma, true_sigma, size=n),
            rng.normal(0.0, true_sigma, size=n),
        )
        actuals = mus + noise
        return [
            CalibrationPair(
                mu=float(m), sigma=true_sigma, ensemble_var=true_sigma**2, actual=float(a)
            )
            for m, a in zip(mus, actuals, strict=True)
        ]

    fit_pairs = draw(600, 20260717 + 1)
    eval_pairs = draw(4000, 20260717 + 2)

    gaussian_cal = fit_calibration("KLGA", "TMIN", 1, fit_pairs)
    t_cal = fit_student_t_free_df("KLGA", "TMIN", 1, fit_pairs)

    def lower_tail_pit_ratio(cal, *, use_t: bool) -> float:
        q = 0.10
        hits = 0
        for p in eval_pairs:
            mu = p.mu - cal.bias
            sigma = max(math.sqrt(max(cal.var_a + cal.var_b * p.ensemble_var, 0.0)), 0.5)
            z = (p.actual - mu) / sigma
            pit = float(student_t.cdf(z, cal.df)) if use_t else float(norm.cdf(z))
            if pit < q:
                hits += 1
        return (hits / len(eval_pairs)) / q

    gaussian_ratio = lower_tail_pit_ratio(gaussian_cal, use_t=False)
    t_ratio = lower_tail_pit_ratio(t_cal, use_t=True)

    assert abs(t_ratio - 1.0) < abs(gaussian_ratio - 1.0)

    # Body check: median-region PIT centering (|PIT - 0.5|) must not regress much.
    def body_dev(cal, *, use_t: bool) -> float:
        total = 0.0
        for p in eval_pairs:
            mu = p.mu - cal.bias
            sigma = max(math.sqrt(max(cal.var_a + cal.var_b * p.ensemble_var, 0.0)), 0.5)
            z = (p.actual - mu) / sigma
            pit = float(student_t.cdf(z, cal.df)) if use_t else float(norm.cdf(z))
            total += (pit - 0.5) ** 2
        return total / len(eval_pairs)

    gaussian_body = body_dev(gaussian_cal, use_t=False)
    t_body = body_dev(t_cal, use_t=True)
    assert t_body < gaussian_body * 1.5  # within tolerance, not a large regression
