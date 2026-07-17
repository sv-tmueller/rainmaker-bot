"""Sanity tests for the #289 TMAX lower-tail diagnosis module.

Synthetic data only: this suite never hits a live endpoint or the spike's
cache file. `tail_objective.py` is imported read-only here (fetch_or_load_cell_data,
DEFAULT_CACHE_PATH, fit_student_t_free_df, apply_emos_regime, pit_tail_ratios,
score_candidate_cell, CellEval) and must never be edited by this package: a
concurrent package ports code from it, so a shared edit would race.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
from scipy.stats import skewnorm, t as student_t

from rainmaker.spikes.tmax_tail_diagnosis import (
    ResidualShape,
    classify_residual_shape,
    decile_skewness,
    excess_kurtosis,
    moment_skewness,
    se_kurt,
    se_skew,
    season_of,
)

# -----------------------------------------------------------------------------
# Season tagging
# -----------------------------------------------------------------------------


def test_season_of_djf_spans_the_year_boundary() -> None:
    """Dec 31 and the following Jan 15 are the same meteorological winter
    (DJF), tagged to the December that started it, not split by calendar
    year.
    """
    dec_year, dec_month, dec_name = season_of(date(2025, 12, 31))
    jan_year, jan_month, jan_name = season_of(date(2026, 1, 15))
    assert (dec_year, dec_month, dec_name) == (2025, 12, "DJF")
    assert (jan_year, jan_month, jan_name) == (2025, 12, "DJF")


def test_season_of_mam_and_jja() -> None:
    assert season_of(date(2026, 3, 18)) == (2026, 3, "MAM")
    assert season_of(date(2026, 5, 31)) == (2026, 3, "MAM")
    assert season_of(date(2026, 6, 1)) == (2026, 6, "JJA")
    assert season_of(date(2026, 7, 16)) == (2026, 6, "JJA")


def test_season_of_son() -> None:
    assert season_of(date(2026, 9, 1)) == (2026, 9, "SON")
    assert season_of(date(2026, 11, 30)) == (2026, 9, "SON")


# -----------------------------------------------------------------------------
# Diagnostic A estimators: recover known shapes on synthetic samples
# -----------------------------------------------------------------------------


def test_moment_skewness_near_zero_on_normal_sample() -> None:
    rng = np.random.default_rng(1)
    x = rng.standard_normal(20000)
    g1 = moment_skewness(x)
    assert abs(g1) < 3 * se_skew(len(x))


def test_moment_skewness_significantly_negative_on_left_skewed_sample() -> None:
    """scipy.stats.skewnorm with a negative shape parameter has a left tail;
    the moment skewness must land clearly negative and beyond the 3*se band.
    """
    rng = np.random.default_rng(2)
    x = skewnorm.rvs(a=-6.0, size=20000, random_state=rng)
    g1 = moment_skewness(x)
    assert g1 < -3 * se_skew(len(x))


def test_excess_kurtosis_near_zero_on_normal_sample() -> None:
    rng = np.random.default_rng(3)
    x = rng.standard_normal(20000)
    g2 = excess_kurtosis(x)
    assert abs(g2) < 3 * se_kurt(len(x))


def test_excess_kurtosis_significantly_positive_on_student_t_sample() -> None:
    """A Student-t(df=5) sample is symmetric but heavier-tailed than Gaussian:
    excess kurtosis must be clearly positive while moment skewness stays near
    zero (kurtosis without skew, the TMIN-side contrast in the addendum).
    """
    rng = np.random.default_rng(4)
    x = student_t.rvs(df=5, size=20000, random_state=rng)
    g2 = excess_kurtosis(x)
    g1 = moment_skewness(x)
    assert g2 > 3 * se_kurt(len(x))
    assert abs(g1) < 3 * se_skew(len(x))


def test_decile_skewness_matches_hand_computed_example() -> None:
    # P10=1, median=5, P90=9 by construction (symmetric) -> Kelly skew = 0.
    x = list(range(1, 10)) * 20  # 1..9 repeated, deciles land cleanly
    kelly = decile_skewness(x)
    assert abs(kelly) < 1e-9


def test_decile_skewness_negative_on_left_skewed_sample() -> None:
    rng = np.random.default_rng(5)
    x = skewnorm.rvs(a=-6.0, size=20000, random_state=rng)
    assert decile_skewness(x) < 0.0


def test_se_skew_and_se_kurt_formulas() -> None:
    assert abs(se_skew(600) - (6 / 600) ** 0.5) < 1e-12
    assert abs(se_kurt(600) - (24 / 600) ** 0.5) < 1e-12


# -----------------------------------------------------------------------------
# Decision rule: classify_residual_shape (pre-stated in the addendum)
# -----------------------------------------------------------------------------


def test_classify_shape_skew_not_kurtosis() -> None:
    n = 635
    shape = ResidualShape(variable="TMAX", lead=1, n=n, g1=-0.6, g2=0.1, kelly=-0.2)
    assert classify_residual_shape(shape, contrast_g2=1.0) == "skew, not kurtosis"


def test_classify_shape_kurtosis_not_skew() -> None:
    n = 635
    shape = ResidualShape(variable="TMIN", lead=1, n=n, g1=0.02, g2=1.2, kelly=0.01)
    assert classify_residual_shape(shape, contrast_g2=None) == "kurtosis, not skew"


def test_classify_shape_inconclusive_when_neither_flag_fires() -> None:
    n = 635
    shape = ResidualShape(variable="TMAX", lead=0, n=n, g1=-0.05, g2=0.05, kelly=-0.02)
    assert classify_residual_shape(shape, contrast_g2=None) == "inconclusive"


def test_classify_shape_requires_kelly_sign_agreement() -> None:
    """A large negative g1 with a robust measure that disagrees in sign
    should not read as a clean skew finding: the whole point of the robust
    companion is to catch a moment estimate driven by a few outliers.
    """
    n = 635
    shape = ResidualShape(variable="TMAX", lead=1, n=n, g1=-0.6, g2=0.1, kelly=0.05)
    assert classify_residual_shape(shape, contrast_g2=1.0) != "skew, not kurtosis"
