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
    B_CELLS,
    ResidualRow,
    ResidualShape,
    baseline_eval_residuals,
    classify_residual_shape,
    date_concentration,
    decile_skewness,
    excess_kurtosis,
    moment_skewness,
    residual_shape_by_cell,
    se_kurt,
    se_skew,
    season_of,
    station_concentration,
    top2_station_share,
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


# -----------------------------------------------------------------------------
# Diagnostic A driver: baseline_eval_residuals + residual_shape_by_cell
# -----------------------------------------------------------------------------


def _synthetic_cell(
    n: int, mu_true: float, sigma_true: float, seed: int
) -> list[tuple[date, float, float, float]]:
    rng = np.random.default_rng(seed)
    base = date(2026, 1, 1)
    mu_fc = mu_true + rng.normal(0, 0.5, size=n)
    sigma_fc = np.full(n, sigma_true)
    actual = mu_true + sigma_true * rng.standard_normal(n)
    return [
        (base + timedelta(days=i), float(mu_fc[i]), float(sigma_fc[i]), float(actual[i]))
        for i in range(n)
    ]


def test_baseline_eval_residuals_covers_only_the_eval_window() -> None:
    """The 60/40 chronological split must leave the newest 40% as eval rows;
    baseline_eval_residuals returns exactly that many rows for one cell.
    """
    rows = _synthetic_cell(n=200, mu_true=60.0, sigma_true=4.0, seed=10)
    cell_data = {("KTEST", "TMAX", 1): rows}
    residuals = baseline_eval_residuals(cell_data)
    expected_eval_n = 200 - int(200 * 0.60)
    assert len(residuals) == expected_eval_n
    assert all(r.icao == "KTEST" and r.variable == "TMAX" and r.lead == 1 for r in residuals)


def test_baseline_eval_residuals_skips_thin_cells() -> None:
    rows = _synthetic_cell(n=5, mu_true=50.0, sigma_true=3.0, seed=11)
    cell_data = {("KTHIN", "TMAX", 0): rows}
    assert baseline_eval_residuals(cell_data) == []


def test_baseline_eval_residuals_z_is_well_scaled_on_gaussian_data() -> None:
    """On genuinely Gaussian synthetic data, the baseline-fit standardized
    residuals should land close to a standard normal (mean ~0, std ~1): this
    is the sanity check that the fit/apply plumbing is wired correctly.
    """
    rows = _synthetic_cell(n=600, mu_true=55.0, sigma_true=5.0, seed=12)
    cell_data = {("KTEST", "TMAX", 1): rows}
    residuals = baseline_eval_residuals(cell_data)
    z = np.array([r.z for r in residuals])
    assert abs(z.mean()) < 0.3
    assert 0.7 < z.std() < 1.3


def test_residual_shape_by_cell_pools_across_stations() -> None:
    rows_a = _synthetic_cell(n=200, mu_true=50.0, sigma_true=3.0, seed=20)
    rows_b = _synthetic_cell(n=200, mu_true=70.0, sigma_true=4.0, seed=21)
    cell_data = {
        ("KAAA", "TMAX", 1): rows_a,
        ("KBBB", "TMAX", 1): rows_b,
    }
    residuals = baseline_eval_residuals(cell_data)
    shapes = residual_shape_by_cell(residuals)
    assert set(shapes) == {("TMAX", 1)}
    shape = shapes[("TMAX", 1)]
    expected_n = len(residuals)
    assert shape.n == expected_n


def test_residual_shape_by_cell_from_hand_built_rows() -> None:
    rows = [
        ResidualRow(
            icao="KA", variable="TMAX", lead=1, target_date=date(2026, 1, 1) + timedelta(days=i), z=z
        )
        for i, z in enumerate([-2.0, -1.0, 0.0, 1.0, 2.0] * 10)
    ]
    shapes = residual_shape_by_cell(rows)
    shape = shapes[("TMAX", 1)]
    assert shape.n == 50
    assert abs(shape.g1) < 1e-9  # symmetric construction -> zero skew


# -----------------------------------------------------------------------------
# Diagnostic B: concentration of lower-tail hits
# -----------------------------------------------------------------------------


def test_b_cells_is_tmax_leads_1_2_and_tmin_leads_0_1() -> None:
    assert set(B_CELLS) == {("TMAX", 1), ("TMAX", 2), ("TMIN", 0), ("TMIN", 1)}


def test_station_concentration_counts_hits_below_quantile_thresholds() -> None:
    """Station KA has 3 of its 20 residuals below the q=0.05 standard-normal
    threshold (an inflated rate); station KB has 0. The per-station table must
    report that split exactly, plus the expected count under a fair q=0.05.
    """
    q05 = float(-1.6448536269514729)  # norm.ppf(0.05)
    rows = [
        ResidualRow(icao="KA", variable="TMAX", lead=1, target_date=date(2026, 1, 1), z=z)
        for z in ([q05 - 1.0] * 3 + [0.0] * 17)
    ] + [
        ResidualRow(icao="KB", variable="TMAX", lead=1, target_date=date(2026, 1, 1), z=z)
        for z in [0.0] * 20
    ]
    stats = station_concentration(rows, "TMAX", 1)
    by_icao = {s.icao: s for s in stats}
    assert by_icao["KA"].n == 20
    assert by_icao["KA"].hits_05 == 3
    assert by_icao["KA"].expected_05 == 1.0
    assert by_icao["KB"].hits_05 == 0


def test_station_concentration_ignores_other_cells() -> None:
    rows = [
        ResidualRow(icao="KA", variable="TMAX", lead=1, target_date=date(2026, 1, 1), z=-5.0),
        ResidualRow(icao="KA", variable="TMIN", lead=1, target_date=date(2026, 1, 1), z=-5.0),
    ]
    stats = station_concentration(rows, "TMAX", 1)
    assert len(stats) == 1
    assert stats[0].hits_05 == 1


def test_date_concentration_flags_a_shared_cold_snap_date() -> None:
    """Three different stations all bust low on the same target date: a
    synoptic-event signature, distinct from one bad station busting low on
    three different dates.
    """
    d = date(2026, 1, 5)
    rows = [
        ResidualRow(icao=icao, variable="TMAX", lead=1, target_date=d, z=-5.0)
        for icao in ("KA", "KB", "KC")
    ] + [
        ResidualRow(icao="KD", variable="TMAX", lead=1, target_date=date(2026, 1, 6), z=0.0)
    ]
    stats = date_concentration(rows, "TMAX", 1)
    by_date = {s.target_date: s for s in stats}
    assert by_date[d].hits_05 == 3
    assert by_date[d].n == 3


def test_top2_station_share_of_hits_vs_share_of_n() -> None:
    stats = station_concentration(
        [
            ResidualRow(icao="KA", variable="TMAX", lead=1, target_date=date(2026, 1, 1), z=z)
            for z in [-5.0] * 4 + [0.0] * 16
        ]
        + [
            ResidualRow(icao="KB", variable="TMAX", lead=1, target_date=date(2026, 1, 1), z=z)
            for z in [-5.0] * 2 + [0.0] * 18
        ]
        + [
            ResidualRow(icao="KC", variable="TMAX", lead=1, target_date=date(2026, 1, 1), z=0.0)
            for _ in range(20)
        ],
        "TMAX",
        1,
    )
    hit_share, n_share = top2_station_share(stats)
    # KA (4 hits) + KB (2 hits) = 6 of 6 total hits -> 100% of hits.
    assert abs(hit_share - 1.0) < 1e-9
    # KA + KB hold 40 of 60 total rows -> 2/3 of n.
    assert abs(n_share - (2.0 / 3.0)) < 1e-9
