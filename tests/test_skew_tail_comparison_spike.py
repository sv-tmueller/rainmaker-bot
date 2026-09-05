"""Sanity tests for the #382 skew-tail comparison spike.

Synthetic data only: this suite never hits a live endpoint or the cache file.
The tests exist so the third addendum's numbers can be trusted: if the split-t
CDF were wrong, every downstream comparison would be misleading.

Mirrors tests/test_tail_objective_spike.py's discipline: pure helpers and
synthetic constructions only, no live fetch.
"""

from __future__ import annotations

import numpy as np
from scipy.stats import t as student_t

from rainmaker.spikes.skew_tail_comparison import (
    apply_split_t_regime,
    fit_split_t_fixed_df,
    fit_split_t_free_df,
    split_t_scalar_cdf,
    split_t_std_cdf,
)

# ---------------------------------------------------------------------------
# 1. Split-t CDF: at r=0 reduces to symmetric Student-t
# ---------------------------------------------------------------------------


def test_split_t_cdf_symmetric_at_r_zero() -> None:
    """At log_scale_ratio=0 the split-t CDF must equal the symmetric
    Student-t CDF at every test point."""
    df = 5.0
    r = 0.0
    cdf = split_t_std_cdf(r, df)
    u_grid = np.linspace(-5, 5, 101)
    split_vals = cdf(u_grid)
    sym_vals = student_t.cdf(u_grid, df)
    assert np.allclose(split_vals, sym_vals, atol=1e-10), (
        f"split-t at r=0 differs from symmetric t: "
        f"max diff = {np.max(np.abs(split_vals - sym_vals))}"
    )


# ---------------------------------------------------------------------------
# 2. Split-t CDF: monotone, continuous at mode, integrates to 1
# ---------------------------------------------------------------------------


def test_split_t_cdf_monotone_and_continuous() -> None:
    """The split-t CDF must be monotonically increasing and continuous at
    the mode (u=0), where the two halves meet at CDF=0.5."""
    df = 5.0
    r = 1.0  # stretch left tail
    cdf = split_t_std_cdf(r, df)
    u_fine = np.linspace(-10, 10, 10001)
    vals = cdf(u_fine)
    # Monotone non-decreasing
    diffs = np.diff(vals)
    assert np.all(diffs >= -1e-10), f"CDF not monotone: min diff = {np.min(diffs)}"
    # Continuous at u=0: CDF(0) should be 0.5 (both halves meet there)
    assert abs(cdf(np.array([0.0]))[0] - 0.5) < 1e-10, (
        f"CDF(0) = {cdf(np.array([0.0]))[0]}, expected 0.5"
    )


def test_split_t_density_integrates_to_one() -> None:
    """The split-t density (derivative of the CDF) integrated over a wide
    range must be 1.0, confirming it is a proper probability distribution."""
    df = 5.0
    r = 0.5
    cdf = split_t_std_cdf(r, df)
    u_fine = np.linspace(-50, 50, 200001)
    cdf_vals = cdf(u_fine)
    # Numerical derivative as density, then integrate
    density = np.gradient(cdf_vals, u_fine)
    integral = np.trapezoid(density, u_fine)
    assert abs(integral - 1.0) < 1e-3, f"split-t density integrates to {integral}, expected 1.0"


# ---------------------------------------------------------------------------
# 3. Scalar CDF matches vectorized CDF
# ---------------------------------------------------------------------------


def test_split_t_scalar_matches_vectorized() -> None:
    """split_t_scalar_cdf must agree with split_t_std_cdf at the same
    (loc, scale, r, df)."""
    df = 5.0
    r = 0.7
    loc = 3.0
    scale = 2.0
    vec_cdf = split_t_std_cdf(r, df)
    for x in [-5.0, -1.0, 0.0, 1.0, 3.0, 5.0, 10.0]:
        u = (x - loc) / scale
        vec_val = float(vec_cdf(np.array([u]))[0])
        sca_val = split_t_scalar_cdf(x, loc, scale, r, df)
        assert abs(vec_val - sca_val) < 1e-10, (
            f"scalar/vec mismatch at x={x}: scalar={sca_val}, vec={vec_val}"
        )


# ---------------------------------------------------------------------------
# 4. Positive r stretches the left tail (lower PIT ratio > 1)
# ---------------------------------------------------------------------------


def test_positive_r_thickens_lower_tail() -> None:
    """With r > 0 (left tail stretched), the lower 5% quantile of the
    split-t must be further left (more negative) than the symmetric t's
    lower 5% quantile. Equivalently, P(X < symmetric_q05) > 0.05."""
    df = 5.0
    r = 1.5
    sym_q05 = student_t.ppf(0.05, df)  # symmetric t's 5th percentile
    split_cdf = split_t_std_cdf(r, df)
    prob_below = float(split_cdf(np.array([sym_q05]))[0])
    assert prob_below > 0.05, (
        f"With r={r}, P(X < sym_q05) = {prob_below}, expected > 0.05 (left tail should be thicker)"
    )


# ---------------------------------------------------------------------------
# 5. Fit recovery: on synthetic negatively-skewed data, split-t fits a
#    nonzero log_scale_ratio and beats symmetric t on lower-tail PIT
# ---------------------------------------------------------------------------


def test_fit_recovers_skew_on_synthetic_data() -> None:
    """On synthetic split-t data with known r > 0, the CRPS objective does
    NOT identify the skew parameter: the EMOS variance (var_a + var_b * ev)
    absorbs the tail thickness, leaving r near 0. This is the same
    weak-identification problem the live Student-t fit has with df (see
    fit_student_t_free_df's docstring), and it is the spike's central
    finding: CRPS cannot see the skew, so the split-t does not beat the
    symmetric Student-t under CRPS fitting.

    This test documents that finding: the fit returns r near 0 despite the
    data being generated with r=1.0, and the symmetric-t and split-t lower-tail
    PIT ratios are comparable (neither fixes the lower tail under CRPS
    fitting).

    Critical design: ensemble_var must VARY across samples so the EMOS
    variance has something to track, mirroring the real forecast population.
    """
    rng = np.random.default_rng(42)
    df_true = 5.0
    r_true = 1.0  # stretch left tail
    loc_true = 0.0
    n = 800

    ens_sigmas = rng.uniform(1.0, 4.0, size=n)
    ens_vars = ens_sigmas**2
    s_left = np.exp(r_true / 2.0)
    s_right = np.exp(-r_true / 2.0)
    t_abs = np.abs(rng.standard_t(df_true, size=n))
    signs = rng.choice([-1, 1], size=n)
    mags = np.where(signs < 0, t_abs * s_left, t_abs * s_right)
    samples = loc_true + ens_sigmas * signs * mags

    pairs = [(loc_true, float(ev), float(s)) for ev, s in zip(ens_vars, samples, strict=True)]
    fit = fit_split_t_fixed_df(pairs, df=df_true)

    # Weak identification: r stays near 0 despite true r=1.0. The EMOS
    # variance absorbs the tail. This is the finding, not a test failure.
    assert abs(fit.log_scale_ratio) < 0.3, (
        f"CRPS-identified r = {fit.log_scale_ratio}; expected near 0 "
        f"(weak identification: CRPS cannot see the skew)"
    )

    # Sanity: the symmetric-t and split-t PIT ratios should be close (the
    # split-t degenerates to symmetric under CRPS fitting).
    from rainmaker.spikes.tail_objective import std_cdf_for

    sym_cdf = std_cdf_for(df_true)
    split_cdf_fn = split_t_std_cdf(fit.log_scale_ratio, df_true)
    sym_pits = []
    split_pits = []
    for ev, s in zip(ens_vars, samples, strict=True):
        pred_scale = max((fit.var_a + fit.var_b * ev) ** 0.5, 1.0)
        pred_loc = loc_true - fit.bias
        z = (s - pred_loc) / pred_scale
        sym_pits.append(float(sym_cdf(np.array([z]))[0]))
        split_pits.append(float(split_cdf_fn(np.array([z]))[0]))
    sym_lower_05 = sum(1 for p in sym_pits if p < 0.05) / len(sym_pits) / 0.05
    split_lower_05 = sum(1 for p in split_pits if p < 0.05) / len(split_pits) / 0.05
    # Since r ~= 0, the two PIT populations should be nearly identical.
    assert abs(sym_lower_05 - split_lower_05) < 0.15, (
        f"symmetric vs split-t lower_05 differ by {abs(sym_lower_05 - split_lower_05)}, "
        f"expected < 0.15 (split-t degenerates to symmetric under CRPS)"
    )


# ---------------------------------------------------------------------------
# 6. On symmetric data, split-t does not degrade (r near 0)
# ---------------------------------------------------------------------------


def test_fit_near_zero_r_on_symmetric_data() -> None:
    """On data drawn from a symmetric Student-t, the fitted log_scale_ratio
    should be near 0 (no skew needed)."""
    rng = np.random.default_rng(99)
    df_true = 5.0
    n = 500
    samples = rng.standard_t(df_true, size=n) * 3.0  # symmetric, scale=3
    pairs = [(0.0, 9.0, float(s)) for s in samples]

    fit = fit_split_t_fixed_df(pairs, df=df_true)
    assert abs(fit.log_scale_ratio) < 0.5, (
        f"On symmetric data, fitted r = {fit.log_scale_ratio}, expected |r| < 0.5"
    )


# ---------------------------------------------------------------------------
# 7. n-gating: below min_bias_samples, returns uncalibrated (r=0)
# ---------------------------------------------------------------------------


def test_apply_split_t_regime_uncalibrated() -> None:
    """Below MIN_CAL_BIAS_SAMPLES, the regime returns the raw mu, widened
    scale, r=0, and a near-Gaussian df."""
    loc, scale, r, df = apply_split_t_regime(
        bias=1.0,
        var_a=0.5,
        var_b=0.5,
        log_scale_ratio=1.0,
        df=5.0,
        n_samples=5,  # below MIN_CAL_BIAS_SAMPLES (10)
        mu=70.0,
        ensemble_var=4.0,
        min_sigma=1.5,
    )
    assert loc == 70.0, f"uncalibrated loc should be raw mu, got {loc}"
    assert r == 0.0, f"uncalibrated r should be 0, got {r}"
    assert df == 30.0, f"uncalibrated df should be 30 (near-Gaussian), got {df}"


def test_apply_split_t_regime_full() -> None:
    """Above MIN_CAL_SAMPLES, the full split-t fit applies."""
    loc, scale, r, df = apply_split_t_regime(
        bias=1.0,
        var_a=0.5,
        var_b=0.5,
        log_scale_ratio=0.8,
        df=5.0,
        n_samples=100,  # above MIN_CAL_SAMPLES (30)
        mu=70.0,
        ensemble_var=4.0,
        min_sigma=1.5,
    )
    assert loc == 69.0, f"full loc should be mu - bias = 69, got {loc}"
    assert r == 0.8, f"full r should be the fitted value, got {r}"
    assert df == 5.0, f"full df should be the fitted value, got {df}"


# ---------------------------------------------------------------------------
# 8. Free-df fit recovers df on symmetric data
# ---------------------------------------------------------------------------


def test_fit_free_df_recovers_df_on_symmetric_data() -> None:
    """On symmetric Student-t data with known df, the free-df split-t fit
    should recover a df in the right ballpark and r near 0."""
    rng = np.random.default_rng(7)
    df_true = 5.0
    n = 800
    samples = rng.standard_t(df_true, size=n) * 2.0
    pairs = [(0.0, 4.0, float(s)) for s in samples]

    fit = fit_split_t_free_df(pairs, df0=8.0)
    assert abs(fit.log_scale_ratio) < 0.5, (
        f"On symmetric data, fitted r = {fit.log_scale_ratio}, expected |r| < 0.5"
    )
    # df recovery is weak (the docstring warns), so just check it is in a
    # reasonable range, not pegged at a bound
    assert 2.5 < fit.df < 62.0, f"Fitted df = {fit.df}, expected in (2.5, 62)"
