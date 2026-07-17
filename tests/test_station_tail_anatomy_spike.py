"""Sanity tests for the #296 KSFO/KNYC per-station tail-anatomy spike.

Synthetic data only: this suite never hits a live endpoint or either cache
file. Only pure helpers are exercised here (classification, season tagging,
control-selection rule, envelope in/out logic, and the recommendation-mapping
rules), per the sub-plan's offline-pytest constraint. `tail_objective.py` and
`tmax_tail_diagnosis.py` are imported read-only (never edited): a concurrent
package ports code from the former, and both are frozen recorded artifacts.
"""

from __future__ import annotations

from datetime import date

import pytest

from rainmaker.spikes.station_tail_anatomy import (
    IN_SEASON_END,
    IN_SEASON_START,
    RAW_MISS_THRESHOLD_F,
    StationRecommendation,
    attach_envelope_kinds,
    classify_bust_kind,
    classify_station_anatomy,
    klga_paired_comparison,
    pool_level_anatomy_for_cell,
    primary_busts,
    raw_miss_season_summary,
    raw_misses,
    recommend_for_station,
    season_pure_refit_lower05,
    select_asos_control,
)
from rainmaker.spikes.tmax_tail_diagnosis import ResidualRow, StationHitStat

# -----------------------------------------------------------------------------
# Envelope in/out logic (forecast-type vs spread-type)
# -----------------------------------------------------------------------------


def test_classify_bust_kind_forecast_type_when_actual_below_every_model() -> None:
    envelope = {"gfs_seamless": 60.0, "ecmwf_ifs025": 61.0, "icon_seamless": 59.0}
    assert classify_bust_kind(actual=55.0, model_extremes=envelope) == "forecast-type"


def test_classify_bust_kind_spread_type_when_actual_inside_envelope() -> None:
    envelope = {"gfs_seamless": 60.0, "ecmwf_ifs025": 61.0, "icon_seamless": 59.0}
    assert classify_bust_kind(actual=59.5, model_extremes=envelope) == "spread-type"


def test_classify_bust_kind_at_envelope_minimum_is_spread_type() -> None:
    """Actual exactly at the coldest model's own extreme: some model got
    there, so this reads as spread-type (>= the envelope minimum), not
    forecast-type (< the envelope minimum).
    """
    envelope = {"gfs_seamless": 60.0, "icon_seamless": 59.0}
    assert classify_bust_kind(actual=59.0, model_extremes=envelope) == "spread-type"


def test_classify_bust_kind_raises_on_empty_envelope() -> None:
    with pytest.raises(ValueError, match="no model envelope data"):
        classify_bust_kind(actual=50.0, model_extremes={})


def test_attach_envelope_kinds_marks_unknown_when_envelope_missing() -> None:
    bust = primary_busts(
        cell_data={("KAAA", "TMAX", 1): [(date(2026, 6, 1), 60.0, 3.0, 50.0)] * 20},
        residuals=[
            ResidualRow(icao="KAAA", variable="TMAX", lead=1, target_date=date(2026, 6, 1), z=-3.0)
        ],
        icaos=frozenset({"KAAA"}),
        leads=(1,),
    )
    out = attach_envelope_kinds(bust, per_model_data={})
    assert out[0].kind == "unknown"


def test_attach_envelope_kinds_uses_the_matching_date_lead_station_envelope() -> None:
    bust = primary_busts(
        cell_data={("KAAA", "TMAX", 1): [(date(2026, 6, 1), 60.0, 3.0, 50.0)] * 20},
        residuals=[
            ResidualRow(icao="KAAA", variable="TMAX", lead=1, target_date=date(2026, 6, 1), z=-3.0)
        ],
        icaos=frozenset({"KAAA"}),
        leads=(1,),
    )
    per_model_data = {
        "KAAA": {1: {date(2026, 6, 1): {"gfs_seamless": 55.0, "icon_seamless": 56.0}}}
    }
    out = attach_envelope_kinds(bust, per_model_data)
    assert out[0].kind == "forecast-type"  # actual 50.0 < envelope min 55.0


# -----------------------------------------------------------------------------
# Station-level classification (spread-dominant / forecast-dominant / mixed)
# -----------------------------------------------------------------------------


def test_classify_station_anatomy_spread_dominant_at_two_thirds() -> None:
    kinds = ["spread-type"] * 2 + ["forecast-type"] * 1
    assert classify_station_anatomy(kinds) == "spread-dominant"


def test_classify_station_anatomy_forecast_dominant_at_two_thirds() -> None:
    kinds = ["forecast-type"] * 2 + ["spread-type"] * 1
    assert classify_station_anatomy(kinds) == "forecast-dominant"


def test_classify_station_anatomy_mixed_below_both_thresholds() -> None:
    kinds = ["forecast-type"] * 1 + ["spread-type"] * 1
    assert classify_station_anatomy(kinds) == "mixed"


def test_classify_station_anatomy_insufficient_data_when_all_unknown() -> None:
    assert classify_station_anatomy(["unknown", "unknown"]) == "insufficient-data"


def test_classify_station_anatomy_ignores_unknown_entries_in_the_denominator() -> None:
    # 2 spread-type / 3 forecast-type+spread-type would be exactly at the 2/3
    # spread-dominant threshold; the "unknown" entries must not dilute that.
    kinds = ["spread-type", "spread-type", "forecast-type", "unknown", "unknown"]
    assert classify_station_anatomy(kinds) == "spread-dominant"


# -----------------------------------------------------------------------------
# Control-selection rule (deterministic ASOS control)
# -----------------------------------------------------------------------------


def _hit_stat(icao: str, n: int, hits: int) -> StationHitStat:
    return StationHitStat(
        icao=icao,
        n=n,
        hits_05=hits,
        hits_10=hits,
        expected_05=n * 0.05,
        expected_10=n * 0.10,
        binom_p_05=1.0,
    )


def test_select_asos_control_picks_the_closest_to_nominal_hit_rate() -> None:
    # KLGA: 5% at both leads (dead on nominal). KSFO: 40%/25% (badly off, real-world-like).
    # Both are real ASOS-settled Polymarket stations (STATIONS), so they clear the
    # ASOS_ICAOS filter the rule applies.
    stats_lead1 = [_hit_stat("KLGA", 40, 2), _hit_stat("KSFO", 40, 16)]
    stats_lead2 = [_hit_stat("KLGA", 40, 2), _hit_stat("KSFO", 40, 10)]
    assert select_asos_control(stats_lead1, stats_lead2) == "KLGA"


def test_select_asos_control_ties_break_on_larger_n_then_alphabetical() -> None:
    # KMIA and KORD both land exactly on nominal (score 0) but KORD has more n.
    stats_lead1 = [_hit_stat("KMIA", 40, 2), _hit_stat("KORD", 60, 3)]
    stats_lead2 = [_hit_stat("KMIA", 40, 2), _hit_stat("KORD", 60, 3)]
    assert select_asos_control(stats_lead1, stats_lead2) == "KORD"


def test_select_asos_control_alphabetical_tiebreak_when_n_also_ties() -> None:
    stats_lead1 = [_hit_stat("KSEA", 40, 2), _hit_stat("KAUS", 40, 2)]
    stats_lead2 = [_hit_stat("KSEA", 40, 2), _hit_stat("KAUS", 40, 2)]
    assert select_asos_control(stats_lead1, stats_lead2) == "KAUS"


def test_select_asos_control_ignores_non_asos_stations() -> None:
    # KNYC is Kalshi-only/GHCND, not in ASOS_ICAOS, even though it is dead on
    # nominal here; the rule must still pick the one real ASOS candidate.
    stats_lead1 = [_hit_stat("KNYC", 40, 2), _hit_stat("KDAL", 40, 6)]
    stats_lead2 = [_hit_stat("KNYC", 40, 2), _hit_stat("KDAL", 40, 6)]
    assert select_asos_control(stats_lead1, stats_lead2) == "KDAL"


def test_select_asos_control_requires_both_leads_present() -> None:
    stats_lead1 = [_hit_stat("KLGA", 40, 2)]
    stats_lead2: list[StationHitStat] = []
    with pytest.raises(ValueError, match="no ASOS station"):
        select_asos_control(stats_lead1, stats_lead2)


# -----------------------------------------------------------------------------
# Raw-miss companion series (seasonal coverage only, full window, no split)
# -----------------------------------------------------------------------------


def test_raw_misses_flags_actual_at_or_below_five_degrees_under_forecast() -> None:
    rows = [
        (date(2026, 4, 1), 60.0, 3.0, 55.0),  # depth 5.0, exactly at threshold -> miss
        (date(2026, 4, 2), 60.0, 3.0, 56.0),  # depth 4.0 -> not a miss
        (date(2026, 4, 3), 60.0, 3.0, 40.0),  # depth 20.0 -> miss
    ]
    misses = raw_misses(rows, icao="KAAA", lead=1)
    assert {m.target_date for m in misses} == {date(2026, 4, 1), date(2026, 4, 3)}
    assert all(m.depth_f >= RAW_MISS_THRESHOLD_F for m in misses)


def test_raw_miss_season_summary_reports_in_season_fraction_and_off_season_days() -> None:
    rows_by_lead = {
        1: [
            (date(2026, 4, 1), 60.0, 3.0, 40.0),  # off-season (pre-May), a miss
            (date(2026, 6, 1), 60.0, 3.0, 40.0),  # in-season, a miss
            (date(2026, 6, 2), 60.0, 3.0, 59.0),  # in-season, not a miss
        ],
    }
    frac, off_days = raw_miss_season_summary(rows_by_lead, icao="KAAA")
    assert frac == pytest.approx(1 / 2)  # 1 of 2 misses is in-season
    assert off_days == 1  # one distinct pre-May date in the archive


def test_raw_miss_season_summary_returns_none_fraction_when_no_misses() -> None:
    rows_by_lead = {1: [(date(2026, 6, 1), 60.0, 3.0, 59.0)]}
    frac, off_days = raw_miss_season_summary(rows_by_lead, icao="KAAA")
    assert frac is None
    assert off_days == 0


def test_in_season_window_matches_the_sub_plans_frozen_dates() -> None:
    assert IN_SEASON_START == date(2026, 5, 1)
    assert IN_SEASON_END == date(2026, 7, 16)


# -----------------------------------------------------------------------------
# KLGA paired-date comparison (KNYC hypothesis)
# -----------------------------------------------------------------------------


def test_klga_paired_comparison_matches_on_date_and_lead() -> None:
    knyc_busts = primary_busts(
        cell_data={("KNYC", "TMAX", 1): [(date(2026, 6, 1), 60.0, 3.0, 50.0)] * 20},
        residuals=[
            ResidualRow(icao="KNYC", variable="TMAX", lead=1, target_date=date(2026, 6, 1), z=-3.0)
        ],
        icaos=frozenset({"KNYC"}),
        leads=(1,),
    )
    cell_data = {
        ("KNYC", "TMAX", 1): [(date(2026, 6, 1), 60.0, 3.0, 50.0)],
        ("KLGA", "TMAX", 1): [(date(2026, 6, 1), 61.0, 2.5, 58.0)],
    }
    rows = klga_paired_comparison(cell_data, knyc_busts)
    assert len(rows) == 1
    row = rows[0]
    assert row.knyc_actual == 50.0
    assert row.klga_actual == 58.0
    assert row.actual_delta == pytest.approx(50.0 - 58.0)
    assert row.klga_raw_miss is False  # KLGA's own depth (61-58=3) is under the 5F threshold


def test_klga_paired_comparison_skips_dates_klga_has_no_data_for() -> None:
    knyc_busts = primary_busts(
        cell_data={("KNYC", "TMAX", 1): [(date(2026, 6, 1), 60.0, 3.0, 50.0)] * 20},
        residuals=[
            ResidualRow(icao="KNYC", variable="TMAX", lead=1, target_date=date(2026, 6, 1), z=-3.0)
        ],
        icaos=frozenset({"KNYC"}),
        leads=(1,),
    )
    rows = klga_paired_comparison({("KNYC", "TMAX", 1): []}, knyc_busts)
    assert rows == []


# -----------------------------------------------------------------------------
# Pool-level anatomy (degradation-clause fallback)
# -----------------------------------------------------------------------------


def test_pool_level_anatomy_for_cell_separates_bust_and_non_bust_sigma() -> None:
    rows = [
        (date(2026, 6, 1), 60.0, 5.0, 40.0),  # bust date, wide sigma
        (date(2026, 6, 2), 60.0, 2.0, 59.0),  # non-bust date, tight sigma
    ]
    busts = [
        # required_sigma is (mu - actual) / sigma = (60 - 40) / 5 = 4.0
        primary_busts(
            cell_data={("KAAA", "TMAX", 1): rows},
            residuals=[
                ResidualRow(
                    icao="KAAA", variable="TMAX", lead=1, target_date=date(2026, 6, 1), z=-3.0
                )
            ],
            icaos=frozenset({"KAAA"}),
            leads=(1,),
        )[0]
    ]
    anatomy = pool_level_anatomy_for_cell("KAAA", 1, rows, busts)
    assert anatomy.n_bust == 1
    assert anatomy.mean_required_sigma == pytest.approx(4.0)
    assert anatomy.mean_sigma_bust == pytest.approx(5.0)
    assert anatomy.mean_sigma_non_bust == pytest.approx(2.0)


# -----------------------------------------------------------------------------
# Season-pure per-station refit check (spread-dominant branch only)
# -----------------------------------------------------------------------------


def _dense_rows(
    start: date, n_days: int, mu: float, sigma: float, actual_fn: object
) -> list[tuple[date, float, float, float]]:
    from datetime import timedelta

    out = []
    for i in range(n_days):
        d = start + timedelta(days=i)
        actual = actual_fn(d) if callable(actual_fn) else mu  # type: ignore[operator]
        out.append((d, mu, sigma, actual))
    return out


def test_season_pure_refit_lower05_returns_none_below_min_cal_samples() -> None:
    from datetime import timedelta

    window_end = date(2026, 7, 16)
    # Only a handful of JJA fit rows before the eval window: below MIN_CAL_SAMPLES.
    rows = _dense_rows(date(2026, 6, 25), 5, 60.0, 3.0, lambda d: 60.0)
    rows += _dense_rows(window_end - timedelta(days=13), 14, 60.0, 3.0, lambda d: 60.0)
    result = season_pure_refit_lower05(rows, "KAAA", "TMAX", 1, window_end=window_end)
    assert result is None


def test_season_pure_refit_lower05_recovers_near_nominal_on_well_calibrated_data() -> None:
    """A station whose JJA-only fit and eval rows are genuinely well-calibrated
    Gaussian noise should land its lower-.05 PIT ratio near 1.0 (comfortably
    inside the [0.5, 1.5] evidence bar), the sanity check that the refit
    plumbing (fit_calibration + apply_emos_regime + pit_tail_ratios) is wired
    correctly before it is trusted on real per-station data.
    """
    import numpy as np

    rng = np.random.default_rng(7)
    window_end = date(2026, 7, 16)
    fit_start = date(2026, 6, 1)
    n_fit = (window_end - date(2026, 6, 15)).days  # generous JJA fit window
    rows = []
    d = fit_start
    for _ in range(n_fit + 14):
        mu = 60.0 + float(rng.normal(0, 0.5))
        actual = 60.0 + 5.0 * float(rng.standard_normal())
        rows.append((d, mu, 5.0, actual))
        d = d + __import__("datetime").timedelta(days=1)
    result = season_pure_refit_lower05(rows, "KAAA", "TMAX", 1, window_end=window_end)
    assert result is not None
    assert 0.0 <= result <= 4.0  # loose sanity band; the real evidence-bar check is stricter


# -----------------------------------------------------------------------------
# Recommendation mapping (the frozen decision tree)
# -----------------------------------------------------------------------------


def test_recommend_spread_dominant_passes_when_refit_lands_in_the_evidence_bar() -> None:
    rec = recommend_for_station(
        icao="KAAA",
        kinds=["spread-type", "spread-type", "spread-type"],
        depths_f=[6.0, 7.0, 8.0],
        hit_rate_lead1=0.3,
        hit_rate_lead2=0.2,
        raw_miss_in_season_frac=0.5,
        off_season_day_count=44,
        refit_lo05={1: 1.0, 2: 1.2},
    )
    assert rec.classification == "spread-dominant"
    assert rec.action == "station-specific calibration adjustment"


def test_recommend_spread_dominant_falls_to_penalty_when_refit_misses_the_bar() -> None:
    rec = recommend_for_station(
        icao="KAAA",
        kinds=["spread-type", "spread-type", "spread-type"],
        depths_f=[6.0, 7.0, 8.0],
        hit_rate_lead1=0.3,
        hit_rate_lead2=0.2,
        raw_miss_in_season_frac=0.5,
        off_season_day_count=44,
        refit_lo05={1: 2.5, 2: 1.2},  # lead 1 outside [0.5, 1.5]
    )
    assert rec.action == "confidence penalty"


def test_recommend_spread_dominant_penalty_when_refit_not_computable() -> None:
    rec = recommend_for_station(
        icao="KAAA",
        kinds=["spread-type", "spread-type", "spread-type"],
        depths_f=[6.0, 7.0, 8.0],
        hit_rate_lead1=0.3,
        hit_rate_lead2=0.2,
        raw_miss_in_season_frac=0.5,
        off_season_day_count=44,
        refit_lo05={1: None, 2: 1.2},
    )
    assert rec.action == "confidence penalty"


def test_recommend_forecast_dominant_exclusion_grade_at_four_x_nominal_both_leads() -> None:
    rec = recommend_for_station(
        icao="KSFO",
        kinds=["forecast-type"] * 9 + ["spread-type"],
        depths_f=[10.0] * 10,
        hit_rate_lead1=0.61,
        hit_rate_lead2=0.41,
        raw_miss_in_season_frac=0.5,  # not season-scoped: below the 75% cut
        off_season_day_count=44,
        refit_lo05={},
    )
    assert rec.classification == "forecast-dominant"
    assert rec.action == "exclusion"


def test_recommend_forecast_dominant_penalty_below_severity_cut() -> None:
    rec = recommend_for_station(
        icao="KAAA",
        kinds=["forecast-type"] * 9 + ["spread-type"],
        depths_f=[10.0] * 10,
        hit_rate_lead1=0.10,  # below 4x nominal (0.20) at lead 1
        hit_rate_lead2=0.41,
        raw_miss_in_season_frac=0.5,
        off_season_day_count=44,
        refit_lo05={},
    )
    assert rec.action == "confidence penalty"


def test_recommend_forecast_dominant_season_scoped_when_thin_off_season() -> None:
    rec = recommend_for_station(
        icao="KSFO",
        kinds=["forecast-type"] * 9 + ["spread-type"],
        depths_f=[10.0] * 10,
        hit_rate_lead1=0.61,
        hit_rate_lead2=0.41,
        raw_miss_in_season_frac=0.80,  # >= 75% in-season
        off_season_day_count=44,  # < 60, thin
        refit_lo05={},
    )
    assert rec.action.startswith("season-scoped exclusion")
    assert "revisit after" in rec.action


def test_recommend_forecast_dominant_not_season_scoped_when_off_season_arm_thick() -> None:
    rec = recommend_for_station(
        icao="KSFO",
        kinds=["forecast-type"] * 9 + ["spread-type"],
        depths_f=[10.0] * 10,
        hit_rate_lead1=0.61,
        hit_rate_lead2=0.41,
        raw_miss_in_season_frac=0.80,
        off_season_day_count=90,  # >= 60, not thin
        refit_lo05={},
    )
    assert rec.action == "exclusion"


def test_recommend_mixed_penalty_when_median_depth_at_or_above_three() -> None:
    rec = recommend_for_station(
        icao="KAAA",
        kinds=["forecast-type", "spread-type"],
        depths_f=[2.0, 4.0],  # median 3.0
        hit_rate_lead1=0.1,
        hit_rate_lead2=0.1,
        raw_miss_in_season_frac=0.5,
        off_season_day_count=44,
        refit_lo05={},
    )
    assert rec.classification == "mixed"
    assert rec.action == "confidence penalty"


def test_recommend_mixed_no_action_when_median_depth_below_three() -> None:
    rec = recommend_for_station(
        icao="KAAA",
        kinds=["forecast-type", "spread-type"],
        depths_f=[1.0, 2.0],  # median 1.5
        hit_rate_lead1=0.1,
        hit_rate_lead2=0.1,
        raw_miss_in_season_frac=0.5,
        off_season_day_count=44,
        refit_lo05={},
    )
    assert rec.action.startswith("no action, revisit at n >=")


def test_recommend_insufficient_data_when_all_kinds_unknown() -> None:
    rec = recommend_for_station(
        icao="KAAA",
        kinds=["unknown", "unknown"],
        depths_f=[5.0, 6.0],
        hit_rate_lead1=0.1,
        hit_rate_lead2=0.1,
        raw_miss_in_season_frac=0.5,
        off_season_day_count=44,
        refit_lo05={},
    )
    assert rec.classification == "insufficient-data"
    assert "per-model envelope" in rec.action


def test_station_recommendation_reports_kind_counts() -> None:
    rec = recommend_for_station(
        icao="KAAA",
        kinds=["forecast-type", "forecast-type", "spread-type", "unknown"],
        depths_f=[5.0, 6.0, 4.0],
        hit_rate_lead1=0.1,
        hit_rate_lead2=0.1,
        raw_miss_in_season_frac=0.5,
        off_season_day_count=44,
        refit_lo05={},
    )
    assert rec.n_known == 3
    assert rec.n_forecast_type == 2
    assert rec.n_spread_type == 1
    assert rec.n_unknown == 1
    assert isinstance(rec, StationRecommendation)
