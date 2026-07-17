"""Sanity tests for the #284 tail-objective comparison harness.

Synthetic data only: this suite never hits a live endpoint (the harness's own
data fetch is exercised by running `python -m rainmaker.spikes.tail_objective`
by hand, not by pytest). These tests exist so the decision doc's numbers can be
trusted: if the scoring engine were wrong, every downstream comparison would be
wrong too.
"""

import numpy as np
from scipy.stats import norm, t

from rainmaker.backtest import crps_gaussian
from rainmaker.spikes.tail_objective import (
    apply_emos_regime,
    bucket_probability_generic,
    fit_student_t_fixed_df,
    fit_student_t_free_df,
    fit_twcrps_gaussian,
    numeric_crps,
    pit_tail_ratios,
    std_cdf_for,
)


def test_numeric_crps_matches_closed_form_gaussian() -> None:
    """Unweighted numeric CRPS must match backtest.crps_gaussian's closed form."""
    cases = [(50.0, 3.0, 52.0), (70.0, 5.0, 55.0), (10.0, 1.5, 10.0), (0.0, 2.0, -6.0)]
    mu = np.array([c[0] for c in cases])
    sigma = np.array([c[1] for c in cases])
    actual = np.array([c[2] for c in cases])

    numeric = numeric_crps(lambda u: norm.cdf(u), mu, sigma, actual)
    closed_form = np.array([crps_gaussian(m, s, a) for m, s, a in cases])

    assert np.allclose(numeric, closed_form, atol=3e-3)


def test_numeric_twcrps_with_unit_weight_equals_crps() -> None:
    """A weight function that is always 1 must reduce twCRPS to plain CRPS."""
    mu = np.array([50.0, 20.0])
    sigma = np.array([3.0, 4.0])
    actual = np.array([48.0, 25.0])

    unweighted = numeric_crps(lambda u: norm.cdf(u), mu, sigma, actual)
    weighted = numeric_crps(
        lambda u: norm.cdf(u), mu, sigma, actual, weight=lambda x: np.ones_like(x)
    )

    assert np.allclose(unweighted, weighted)


def test_std_cdf_for_gaussian_and_student_t() -> None:
    gaussian_cdf = std_cdf_for(None)
    t_cdf = std_cdf_for(5.0)
    u = np.array([-1.0, 0.0, 1.0])
    assert np.allclose(gaussian_cdf(u), norm.cdf(u))
    assert np.allclose(t_cdf(u), t.cdf(u, 5.0))


def test_pit_tail_ratios_well_calibrated_gaussian_near_one() -> None:
    """Draw PIT values from Uniform(0,1) (the well-calibrated case): ratios ~ 1.0."""
    rng = np.random.default_rng(42)
    pits = list(rng.uniform(0.0, 1.0, size=20000))
    ratios = pit_tail_ratios(pits)
    assert ratios["n"] == 20000
    for key in ("upper_10", "lower_10", "upper_05", "lower_05"):
        assert 0.85 < ratios[key] < 1.15, (key, ratios[key])


def test_pit_tail_ratios_matches_hand_count() -> None:
    pits = [0.01, 0.02, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.96, 0.99]
    ratios = pit_tail_ratios(pits)
    assert ratios["n"] == 10
    # lower_10: count(p < 0.10) = 2 -> (2/10)/0.10 = 2.0
    assert ratios["lower_10"] == 2.0
    # upper_05: count(p > 0.95) = 2 -> (2/10)/0.05 = 4.0
    assert ratios["upper_05"] == 4.0


def test_bucket_probability_generic_matches_gaussian_outcomes() -> None:
    """Ported continuity correction must match outcomes.bucket_probability exactly."""
    from rainmaker.domain import Bucket
    from rainmaker.probability.calibration import Predictive
    from rainmaker.probability.outcomes import bucket_probability

    g = Predictive(mu=55.0, sigma=4.0)
    buckets = [
        Bucket(
            label="54° or below",
            kind="below",
            lo=None,
            hi=None,
            threshold=54,
            yes_token_id="",
            best_ask=None,
            best_bid=None,
            yes_price=0.0,
        ),
        Bucket(
            label="55-56°F",
            kind="range",
            lo=55,
            hi=56,
            threshold=None,
            yes_token_id="",
            best_ask=None,
            best_bid=None,
            yes_price=0.0,
        ),
        Bucket(
            label="57° or higher",
            kind="above",
            lo=None,
            hi=None,
            threshold=57,
            yes_token_id="",
            best_ask=None,
            best_bid=None,
            yes_price=0.0,
        ),
    ]
    gaussian_cdf = lambda x: float(norm.cdf(x, loc=g.mu, scale=g.sigma))  # noqa: E731
    for b in buckets:
        expected = bucket_probability(g, b)
        got = bucket_probability_generic(gaussian_cdf, b)
        assert abs(expected - got) < 1e-12


def test_fit_student_t_recovers_heavier_lower_tail_better_than_gaussian_baseline() -> None:
    """On synthetic Student-t data with fat tails, the t-fit's PIT tail ratios
    must land closer to 1.0 (the ideal) than a plain Gaussian fit's.

    Fit on a moderate sample, then score both on a large *independent* draw
    from the same generating distribution: a q=0.05 tail ratio from a few
    hundred eval points is dominated by count noise (a handful of tail hits
    either way swings it), so a small eval set makes this comparison flip by
    seed. A large, separately-drawn eval set is what makes the comparison
    reflect the fitted distributions rather than which few points happened to
    land in the tail.
    """
    rng = np.random.default_rng(5)
    n_fit = 500
    df_true = 3.0
    mu_true = 60.0
    sigma_true = 3.0

    mu_fc = mu_true + rng.normal(0, 0.5, size=n_fit)  # small forecast noise
    ev_fc = np.full(n_fit, sigma_true**2)
    actual = mu_true + sigma_true * t.rvs(df_true, size=n_fit, random_state=rng)
    fit_pairs = list(zip(mu_fc.tolist(), ev_fc.tolist(), actual.tolist(), strict=True))

    from rainmaker.probability.calibration import CalibrationPair, fit_calibration

    cal_pairs = [
        CalibrationPair(mu=m, sigma=e**0.5, ensemble_var=e, actual=a) for m, e, a in fit_pairs
    ]
    baseline = fit_calibration("TEST", "TMAX", 0, cal_pairs)
    t_fit = fit_student_t_fixed_df(fit_pairs, df=5.0)

    n_eval = 20000
    mu_eval = mu_true + rng.normal(0, 0.5, size=n_eval)
    actual_eval = mu_true + sigma_true * t.rvs(df_true, size=n_eval, random_state=rng)

    def pit_ratios_for(
        bias: float, var_a: float, var_b: float, df: float | None
    ) -> dict[str, float | int]:
        scale = max(var_a + var_b * sigma_true**2, 1e-9) ** 0.5
        z = (actual_eval - (mu_eval - bias)) / scale
        return pit_tail_ratios(std_cdf_for(df)(z).tolist())

    gaussian_ratios = pit_ratios_for(baseline.bias, baseline.var_a, baseline.var_b, None)
    t_ratios = pit_ratios_for(t_fit.bias, t_fit.var_a, t_fit.var_b, t_fit.df)

    # The t-fit's lower tail ratio should land closer to the ideal (1.0) than the
    # Gaussian baseline's, since the synthetic data has genuinely heavier tails.
    assert abs(t_ratios["lower_05"] - 1.0) < abs(gaussian_ratios["lower_05"] - 1.0)


def test_fit_student_t_free_df_bounds_df_above_two() -> None:
    rng = np.random.default_rng(3)
    n = 200
    mu_fc = 50.0 + rng.normal(0, 1, size=n)
    ev_fc = np.full(n, 9.0)
    actual = 50.0 + 3.0 * rng.standard_normal(n)
    pairs = list(zip(mu_fc.tolist(), ev_fc.tolist(), actual.tolist(), strict=True))
    fit = fit_student_t_free_df(pairs)
    assert fit.df is not None
    assert fit.df > 2.0


def test_fit_twcrps_gaussian_returns_gaussian_family() -> None:
    rng = np.random.default_rng(11)
    n = 200
    mu_fc = 50.0 + rng.normal(0, 1, size=n)
    ev_fc = np.full(n, 9.0)
    actual = 50.0 + 3.0 * rng.standard_normal(n)
    pairs = list(zip(mu_fc.tolist(), ev_fc.tolist(), actual.tolist(), strict=True))
    fit = fit_twcrps_gaussian(pairs, multiplier=5.0)
    assert fit.df is None
    assert fit.var_a >= 0.0
    assert fit.var_b >= 0.0


def test_no_candidate_degrades_body_calibration_on_well_calibrated_gaussian_data() -> None:
    """On genuinely Gaussian synthetic data, none of the candidate fits should
    push the reliability of the body (mid-range claims) meaningfully off the
    diagonal relative to the baseline: this is the "no collateral damage" guard.
    """
    rng = np.random.default_rng(99)
    n = 500
    mu_true = 60.0
    sigma_true = 3.0
    mu_fc = mu_true + rng.normal(0, 0.5, size=n)
    ev_fc = np.full(n, sigma_true**2)
    actual = mu_true + sigma_true * rng.standard_normal(n)
    pairs = list(zip(mu_fc.tolist(), ev_fc.tolist(), actual.tolist(), strict=True))
    split = int(n * 0.6)
    fit_pairs, eval_pairs = pairs[:split], pairs[split:]

    from rainmaker.probability.calibration import CalibrationPair, fit_calibration

    cal_pairs = [
        CalibrationPair(mu=m, sigma=e**0.5, ensemble_var=e, actual=a) for m, e, a in fit_pairs
    ]
    baseline = fit_calibration("TEST", "TMAX", 0, cal_pairs)
    t_fit = fit_student_t_fixed_df(fit_pairs, df=5.0)
    tw_fit = fit_twcrps_gaussian(fit_pairs, multiplier=5.0)

    def mean_abs_pit_dev(fit, is_t: bool) -> float:  # type: ignore[no-untyped-def]
        pits = []
        for m, e, a in eval_pairs:
            loc = m - fit.bias
            scale = max(fit.var_a + fit.var_b * e, 1e-9) ** 0.5
            df = fit.df if is_t else None
            z = (a - loc) / scale
            pits.append(float(std_cdf_for(df)(np.array([z]))[0]))
        # Body proxy: mean |pit - 0.5| should stay close across fits (~0.25 for
        # a uniform PIT); a broken body would inflate this well past baseline.
        return float(np.mean(np.abs(np.array(pits) - 0.5)))

    baseline_dev = mean_abs_pit_dev(
        type(
            "F",
            (),
            {"bias": baseline.bias, "var_a": baseline.var_a, "var_b": baseline.var_b, "df": None},
        )(),
        is_t=False,
    )
    t_dev = mean_abs_pit_dev(t_fit, is_t=True)
    tw_dev = mean_abs_pit_dev(tw_fit, is_t=False)

    assert abs(t_dev - baseline_dev) < 0.03
    assert abs(tw_dev - baseline_dev) < 0.03


def test_apply_emos_regime_matches_calibration_when_uncalibrated() -> None:
    """Below the bias-sample floor, apply_emos_regime must match apply_calibration
    exactly (both fall back to mu unchanged, sigma widened-raw): this is the
    "same n-gating regimes" parity the sub-plan requires.
    """
    from rainmaker.config import MIN_SIGMA_F
    from rainmaker.probability.calibration import Calibration, apply_calibration
    from rainmaker.probability.distribution import Gaussian

    cal = Calibration(
        station="X", variable="TMAX", lead_time=0, bias=2.0, var_a=1.0, var_b=1.0, n_samples=3
    )
    g = Gaussian(mu=50.0, sigma=4.0)
    expected, _ = apply_calibration(g, cal, min_sigma=MIN_SIGMA_F)

    loc, scale, df = apply_emos_regime(
        bias=cal.bias,
        var_a=cal.var_a,
        var_b=cal.var_b,
        df=None,
        n_samples=cal.n_samples,
        mu=g.mu,
        ensemble_var=g.sigma**2,
        min_sigma=MIN_SIGMA_F,
        fallback_df=None,
    )
    assert loc == expected.mu
    assert scale == expected.sigma
    assert df is None
