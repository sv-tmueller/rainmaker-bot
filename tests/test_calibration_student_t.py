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

import rainmaker.probability.calibration as calibration
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


def _zero_variance_adversarial_pairs() -> list[CalibrationPair]:
    """A batch driving var_a/var_b to exactly zero with a large, nonzero
    constant residual: the literal spike-era adversarial case (mu=0,
    ensemble_var=1, actual=5, repeated) documented on spike PR #285, which
    produced a division-by-zero OverflowError in numeric_crps when the
    predictive sigma the fitter evaluates is allowed to reach exactly zero.
    """
    return [CalibrationPair(mu=0.0, sigma=1.0, ensemble_var=1.0, actual=5.0) for _ in range(8)]


def test_fit_student_t_free_df_stays_finite_when_variance_wants_to_collapse_to_zero():
    """A batch that drives var_a/var_b to zero (identical mu/ensemble_var, a
    large constant residual) must still yield a finite fit: this is the
    truncation-trap the FIT_MIN_SIGMA floor and the widened grid exist to
    prevent (see numeric_crps's docstring).
    """
    cal = fit_student_t_free_df("KLGA", "TMIN", 1, _zero_variance_adversarial_pairs())
    assert math.isfinite(cal.bias)
    assert math.isfinite(cal.var_a)
    assert math.isfinite(cal.var_b)
    assert cal.df is not None and math.isfinite(cal.df)
    assert cal.var_a >= 0.0
    assert cal.var_b >= 0.0
    assert 2.0 < cal.df <= 62.0


def test_fit_student_t_free_df_raises_when_the_fit_min_sigma_floor_is_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proves FIT_MIN_SIGMA is load-bearing, not just present.

    With the exact same adversarial batch as the test above, patching the
    floor to 0.0 lets the fitter's internal sigma reach exactly zero, which
    reproduces the literal division-by-zero OverflowError from spike PR
    #285 (actual_std = (actual - mu) / sigma with sigma == 0, then an
    infinite span fed into numeric_crps's grid-widening int() conversion).
    A regression that removes or weakens the floor to exactly 0 must make
    this test start failing (a passing test here would mean the floor pins
    nothing).
    """
    monkeypatch.setattr(calibration, "FIT_MIN_SIGMA", 0.0)
    # The division by zero that triggers the OverflowError is the expected,
    # intentional mechanism under test here, not a signal to act on.
    with np.errstate(divide="ignore", invalid="ignore"), pytest.raises(OverflowError):
        fit_student_t_free_df("KLGA", "TMIN", 1, _zero_variance_adversarial_pairs())


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


def _shaped_noise_pairs(sample: object, n: int, seed: int) -> list[CalibrationPair]:
    """mu ~ N(70, 3); actual = mu + 2.0 * sample(rng, n). `sample` supplies the
    standardized noise shape (e.g. scipy.stats.t(df).rvs or a standard normal),
    so two calls with different `sample` functions differ only in tail shape,
    not in scale or location.
    """
    rng = np.random.default_rng(seed)
    mus = 70.0 + rng.normal(0, 3, size=n)
    noise = sample(rng, n) * 2.0  # type: ignore[operator]
    actuals = mus + noise
    return [
        CalibrationPair(mu=float(m), sigma=2.0, ensemble_var=4.0, actual=float(a))
        for m, a in zip(mus, actuals, strict=True)
    ]


def test_fit_student_t_free_df_df_channel_responds_to_tail_shape():
    """The df channel must respond to the data's actual tail shape, not sit
    inert at the df0=8.0 warm start.

    If fit_student_t_free_df's df optimization goes dead (std_cdf_for used
    inside the objective always returns norm.cdf regardless of the candidate
    df, so df has zero effect on the score and the optimizer never leaves
    df0), fitting on genuinely heavy-tailed noise and fitting on Gaussian
    noise collapse to the identical df0=8.0 (bit-for-bit; verified via the
    tester's exact repro on PR #293). A live channel lands at materially
    different, non-8.0 values for the two.

    Note on direction: mean CRPS (this fitter's objective; twCRPS was
    deliberately not ported, see #291's sub-plan) is known to have weak,
    non-monotonic sensitivity to tail shape once the variance parameters
    (var_a, var_b) are also free to fit: they can absorb tail risk as extra
    spread instead of the shape parameter absorbing it. Verified empirically
    against this exact fitter with seed=11/12: t(df=5) noise (the acceptance
    criteria's original choice) gives a live/dead gap of only ~0.03, too thin
    a margin to be a reliable regression signal; t(df=3) noise (heavier,
    still a genuine Student-t) gives a robust, deterministic gap. This test
    asserts the channel is alive (responds differently to differently-shaped
    noise), not that a specific df value is recovered exactly.
    """
    heavy_pairs = _shaped_noise_pairs(
        lambda rng, n: student_t.rvs(3.0, size=n, random_state=rng), 400, seed=11
    )
    light_pairs = _shaped_noise_pairs(lambda rng, n: rng.normal(0, 1, size=n), 400, seed=12)

    heavy_cal = fit_student_t_free_df("KLGA", "TMIN", 1, heavy_pairs)
    light_cal = fit_student_t_free_df("KLGA", "TMIN", 1, light_pairs)

    assert heavy_cal.df is not None and light_cal.df is not None
    assert heavy_cal.df > 9.0  # materially left the df0=8.0 warm start
    assert abs(light_cal.df - 8.0) < 0.3  # Gaussian-shaped noise stays near warm start
    assert heavy_cal.df - light_cal.df > 1.0  # the two constructions must not collapse together


def test_fit_student_t_free_df_improves_lower_tail_pit_on_tail_thin_tmin_construction():
    """A tail-thin (heavy-tailed) synthetic TMIN construction: the Student-t fit's
    lower-tail PIT ratio must land closer to 1.0 (honest) than the Gaussian
    fit's, while the body (mid-range PIT-centering error) does not blow up.
    Also asserts the fitted df materially left the df0=8.0 warm start: with
    an 8% rate of small (-3 sigma) cold-snap outliers and 600 fit pairs (the
    original construction, seed=20260717+1), the df movement is only ~0.03,
    too thin a margin to double as a dead-channel regression check; -6 sigma
    cold snaps at 300 fit pairs (same seed, same 8% rate, still a tail-thin
    construction) give a robust, deterministic gap while leaving the
    PIT-improvement claim intact (verified empirically against this exact
    fitter).

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
            rng.normal(-6.0 * true_sigma, true_sigma, size=n),
            rng.normal(0.0, true_sigma, size=n),
        )
        actuals = mus + noise
        return [
            CalibrationPair(
                mu=float(m), sigma=true_sigma, ensemble_var=true_sigma**2, actual=float(a)
            )
            for m, a in zip(mus, actuals, strict=True)
        ]

    fit_pairs = draw(300, 20260717 + 1)
    eval_pairs = draw(4000, 20260717 + 2)

    gaussian_cal = fit_calibration("KLGA", "TMIN", 1, fit_pairs)
    t_cal = fit_student_t_free_df("KLGA", "TMIN", 1, fit_pairs)

    # A dead df channel (std_cdf_for hijacked to ignore df) would leave the
    # fit frozen at exactly the df0=8.0 warm start regardless of the data.
    assert t_cal.df is not None
    assert abs(t_cal.df - 8.0) > 0.5

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
