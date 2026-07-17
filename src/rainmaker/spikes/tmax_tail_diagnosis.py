"""Issue #289: why does TMAX's lead 1-2 lower tail resist every heavier-tailed
variant tried in spike #284, while TMIN's lower tail is fixed by the same
family change? Three diagnostics over the #284 harness's cached archive pairs,
written up in `docs/architecture/tail-objective-decision.md`'s addendum.

Dead to the live path, like tail_objective.py itself: nothing here is imported
by cli.py or any live-path module. Run it directly:

    uv run python -m rainmaker.spikes.tmax_tail_diagnosis

`tail_objective.py` is imported read-only (fetch_or_load_cell_data,
DEFAULT_CACHE_PATH, fit_student_t_free_df, apply_emos_regime, pit_tail_ratios,
score_candidate_cell, CellEval) and is never edited: it is the recorded
artifact of #284, and a concurrent #280-batch package ports code from it, so
this module stays a sibling rather than a patch to that file.

## Diagnostic A: residual shape per (variable, lead) cell

On the baseline (Gaussian EMOS) fit's eval-window standardized residuals z
(the same population the decision doc's PIT ratios come from: same 60/40
chronological split, same per-station fit, pooled per (variable, lead) across
stations), report moment skewness g1 (se = sqrt(6/n)), excess kurtosis g2
(se = sqrt(24/n)), and a robust companion, decile (Kelly) skewness
(P90 + P10 - 2*median)/(P90 - P10), stable when tail counts are in the tens.

Decision rule, stated before any number is read (`classify_residual_shape`):
a cell reads "skew, not kurtosis" when g1 < -3*se_skew with the robust measure
agreeing in sign, while g2 is not significant (|g2| < 3*se_kurt) or clearly
below a named contrast cell's g2 (TMIN's same lead, by convention here). The
mirror case ("kurtosis, not skew") is g2 > 3*se_kurt with no matching skew
signal. Anything else is "inconclusive" at this sample size.

## Diagnostic B: concentration of lower-tail hits

Same z population as A, split per station and per target date for a fixed set
of (variable, lead) cells (TMAX leads 1-2, the diagnosis target; TMIN leads
0-1, the #244-flagged contrast): observed vs expected hit counts at q=0.05 and
q=0.10, the top-2-stations' share of hits vs their share of n, and a
descriptive (not multiplicity-corrected) exact-binomial p-value per station.
The per-date table separates "a synoptic cold snap hit every city the same
week" from "a few bad stations" -- the correlated-week caveat #244 and the
tail-objective decision doc both flag.

## Diagnostic C: season A/B on fit windows

The decision doc's lead suspect for TMAX's resistance is season mismatch: the
spike's 120-day window (~75 days MAM, ~46 days JJA at the archive used here)
means its 60/40 chronological split fits on a MAM-dominated window and
evaluates on a JJA-dominated one. This diagnostic holds the eval rows fixed
per (variable, lead) cell and station, and varies only the fit window:

- JJA arm (`jja_arm_windows`): eval = the newest ~14 days of the archive.
  Two fits scored on the *same* eval rows: "mixed" (all prior pairs, the
  spike's own MAM-heavy design) and "season-pure" (JJA-only pairs before the
  eval start). The season-pure fit window's length is chosen so each
  station's fit sample stays >= MIN_CAL_SAMPLES: a station that falls short is
  gated (excluded, counted, never silently scored with a below-floor fit).
- MAM arm (`mam_arm_window_or_none`): fit on the archive's earliest ~44 days
  (in-season by construction once the archive's first date is in MAM), eval
  on the remainder of that meteorological spring. Answers "does an in-season
  Gaussian fit have an honest TMAX lower tail at all", independent of the
  mixed-vs-pure question above. Returns None (and the addendum notes it) if
  the archive's first date is not itself in MAM -- the pre-authorized #287-
  pattern trip-wire the sub-plan calls out for a refetched window.

Candidates per arm: `baseline` (`fit_calibration`, Gaussian) and `t_free_df`
(`fit_student_t_free_df`). Reported per arm/candidate/cell: PIT tail ratios
(via `tail_objective.score_candidate_cell`, so the same PIT/Brier/BodyMaxDev
machinery scores every candidate here) plus n and the gated-station count.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from math import sqrt
from typing import Sequence

import numpy as np
from scipy.stats import binomtest, norm

from rainmaker.config import MIN_CAL_SAMPLES, MIN_SIGMA_F, season_start_month
from rainmaker.probability.calibration import CalibrationPair, fit_calibration
from rainmaker.spikes.tail_objective import (
    CellEval,
    apply_emos_regime,
    fit_student_t_free_df,
    score_candidate_cell,
)

CellData = dict[tuple[str, str, int], list[tuple[date, float, float, float]]]

FIT_FRACTION = 0.60  # mirrors tail_objective's own single chronological split
MIN_CELL_PAIRS = 10  # mirrors tail_objective's cell-skip floor

_SEASON_NAMES = {12: "DJF", 3: "MAM", 6: "JJA", 9: "SON"}

# -----------------------------------------------------------------------------
# Season tagging
# -----------------------------------------------------------------------------


def season_of(d: date) -> tuple[int, int, str]:
    """(season_year, season_start_month, season_name) for the meteorological
    season containing `d`, via config.season_start_month. DJF spans a
    calendar-year boundary: a January/February date shares season_year with
    the December that started its winter, not its own calendar year.
    """
    year, month = season_start_month(d)
    return year, month, _SEASON_NAMES[month]


# -----------------------------------------------------------------------------
# Diagnostic A: residual shape estimators
# -----------------------------------------------------------------------------


def moment_skewness(values: Sequence[float]) -> float:
    """g1, the standard (Fisher-Pearson) moment skewness."""
    arr = np.asarray(values, dtype=np.float64)
    if arr.size < 2:
        raise ValueError("need at least 2 values")
    std = arr.std(ddof=0)
    if std == 0:
        return 0.0
    return float(np.mean(((arr - arr.mean()) / std) ** 3))


def excess_kurtosis(values: Sequence[float]) -> float:
    """g2, excess kurtosis (0 for a Gaussian, > 0 for a heavier-than-Gaussian
    symmetric tail such as Student-t).
    """
    arr = np.asarray(values, dtype=np.float64)
    if arr.size < 2:
        raise ValueError("need at least 2 values")
    std = arr.std(ddof=0)
    if std == 0:
        return 0.0
    return float(np.mean(((arr - arr.mean()) / std) ** 4) - 3.0)


def decile_skewness(values: Sequence[float]) -> float:
    """Kelly (decile) skewness: (P90 + P10 - 2*median) / (P90 - P10). Robust
    to the moment estimator's sensitivity to a handful of extreme points,
    which matters when tail counts are in the tens.
    """
    arr = np.asarray(values, dtype=np.float64)
    p10, p50, p90 = np.percentile(arr, [10, 50, 90])
    spread = p90 - p10
    if spread == 0:
        return 0.0
    return float((p90 + p10 - 2 * p50) / spread)


def se_skew(n: int) -> float:
    """Standard error of the moment skewness estimator under normality."""
    return sqrt(6.0 / n)


def se_kurt(n: int) -> float:
    """Standard error of the excess-kurtosis estimator under normality."""
    return sqrt(24.0 / n)


@dataclass(frozen=True)
class ResidualShape:
    variable: str
    lead: int
    n: int
    g1: float
    g2: float
    kelly: float


def classify_residual_shape(shape: ResidualShape, contrast_g2: float | None) -> str:
    """Pre-stated decision rule (see the module docstring and the addendum):

    "skew, not kurtosis": g1 < -3*se_skew, the robust (Kelly) measure agrees
    in sign, and g2 is not significant (|g2| < 3*se_kurt) or clearly below
    `contrast_g2` (by more than 3*se_kurt).

    "kurtosis, not skew": the mirror -- g2 > 3*se_kurt (a heavier, symmetric
    tail) with no matching skew signal.

    Anything else: "inconclusive" at this sample size.
    """
    skew_flag = shape.g1 < -3 * se_skew(shape.n) and shape.kelly < 0
    kurt_not_significant = abs(shape.g2) < 3 * se_kurt(shape.n)
    kurt_clearly_below_contrast = (
        contrast_g2 is not None and contrast_g2 - shape.g2 > 3 * se_kurt(shape.n)
    )
    if skew_flag and (kurt_not_significant or kurt_clearly_below_contrast):
        return "skew, not kurtosis"
    kurt_flag = shape.g2 > 3 * se_kurt(shape.n)
    if kurt_flag and not skew_flag:
        return "kurtosis, not skew"
    return "inconclusive"


# -----------------------------------------------------------------------------
# Diagnostic A driver: baseline eval-window standardized residuals
# -----------------------------------------------------------------------------


def _chronological_split(
    rows: list[tuple[date, float, float, float]],
) -> tuple[list[tuple[date, float, float, float]], list[tuple[date, float, float, float]]]:
    """Same split as tail_objective._split (fit oldest FIT_FRACTION, eval the
    rest); reimplemented locally rather than importing a private helper.
    """
    split_i = int(len(rows) * FIT_FRACTION)
    return rows[:split_i], rows[split_i:]


@dataclass(frozen=True)
class ResidualRow:
    icao: str
    variable: str
    lead: int
    target_date: date
    z: float  # standardized residual: (actual - loc) / scale, baseline Gaussian fit


def baseline_eval_residuals(cell_data: CellData) -> list[ResidualRow]:
    """One row per eval-window pair from the baseline (Gaussian EMOS) fit, on
    the same 60/40 chronological split and per-station-then-pool design
    `tail_objective.run_comparison` uses for its baseline candidate. This is
    the same population the decision doc's PIT ratios come from; Diagnostics
    A and B both read off these z-values rather than refitting per diagnostic.
    """
    out: list[ResidualRow] = []
    for (icao, variable, lead), rows in sorted(cell_data.items()):
        if len(rows) < MIN_CELL_PAIRS:
            continue
        fit_rows, eval_rows = _chronological_split(rows)
        if not fit_rows or not eval_rows:
            continue
        cal_pairs = [
            CalibrationPair(mu=mu, sigma=sigma, ensemble_var=sigma**2, actual=actual)
            for _d, mu, sigma, actual in fit_rows
        ]
        cal = fit_calibration(icao, variable, lead, cal_pairs)
        for d, mu, sigma_raw, actual in eval_rows:
            loc, scale, _df = apply_emos_regime(
                bias=cal.bias,
                var_a=cal.var_a,
                var_b=cal.var_b,
                df=None,
                n_samples=cal.n_samples,
                mu=mu,
                ensemble_var=sigma_raw**2,
                min_sigma=MIN_SIGMA_F,
                fallback_df=None,
            )
            z = (actual - loc) / scale
            out.append(ResidualRow(icao=icao, variable=variable, lead=lead, target_date=d, z=z))
    return out


def residual_shape_by_cell(rows: list[ResidualRow]) -> dict[tuple[str, int], ResidualShape]:
    """Pool ResidualRows per (variable, lead) across stations and compute the
    Diagnostic A shape statistics for each cell.
    """
    by_cell: dict[tuple[str, int], list[float]] = {}
    for r in rows:
        by_cell.setdefault((r.variable, r.lead), []).append(r.z)
    out: dict[tuple[str, int], ResidualShape] = {}
    for (variable, lead), zs in by_cell.items():
        out[(variable, lead)] = ResidualShape(
            variable=variable,
            lead=lead,
            n=len(zs),
            g1=moment_skewness(zs),
            g2=excess_kurtosis(zs),
            kelly=decile_skewness(zs),
        )
    return out


# -----------------------------------------------------------------------------
# Diagnostic B: concentration of lower-tail hits, per station and per date
# -----------------------------------------------------------------------------

B_CELLS: tuple[tuple[str, int], ...] = (("TMAX", 1), ("TMAX", 2), ("TMIN", 0), ("TMIN", 1))
# TMAX leads 1-2 are the diagnosis target; TMIN leads 0-1 are the #244-flagged
# contrast cells the sub-plan asks for.


@dataclass(frozen=True)
class StationHitStat:
    icao: str
    n: int
    hits_05: int
    hits_10: int
    expected_05: float
    expected_10: float
    binom_p_05: float  # descriptive only; not multiplicity-corrected (13-way)


@dataclass(frozen=True)
class DateHitStat:
    target_date: date
    n: int
    hits_05: int


def station_concentration(rows: list[ResidualRow], variable: str, lead: int) -> list[StationHitStat]:
    """Per-station observed vs expected lower-tail hit counts for one
    (variable, lead) cell, sorted by station.
    """
    q05 = float(norm.ppf(0.05))
    q10 = float(norm.ppf(0.10))
    by_station: dict[str, list[float]] = {}
    for r in rows:
        if r.variable == variable and r.lead == lead:
            by_station.setdefault(r.icao, []).append(r.z)
    out: list[StationHitStat] = []
    for icao, zs in sorted(by_station.items()):
        n = len(zs)
        hits_05 = sum(1 for z in zs if z < q05)
        hits_10 = sum(1 for z in zs if z < q10)
        p_05 = float(binomtest(hits_05, n, 0.05, alternative="greater").pvalue) if n else 1.0
        out.append(
            StationHitStat(
                icao=icao,
                n=n,
                hits_05=hits_05,
                hits_10=hits_10,
                expected_05=n * 0.05,
                expected_10=n * 0.10,
                binom_p_05=p_05,
            )
        )
    return out


def date_concentration(rows: list[ResidualRow], variable: str, lead: int) -> list[DateHitStat]:
    """Per-target-date lower-.05 hit counts across stations, for one
    (variable, lead) cell: a date where several stations bust low at once is
    a synoptic-event signature, distinct from one station busting on several
    different dates.
    """
    q05 = float(norm.ppf(0.05))
    by_date: dict[date, list[float]] = {}
    for r in rows:
        if r.variable == variable and r.lead == lead:
            by_date.setdefault(r.target_date, []).append(r.z)
    return [
        DateHitStat(target_date=d, n=len(zs), hits_05=sum(1 for z in zs if z < q05))
        for d, zs in sorted(by_date.items())
    ]


def top2_station_share(stats: list[StationHitStat]) -> tuple[float, float]:
    """(share of lower-.05 hits, share of n) held by the two stations with
    the most hits: separates "a few bad stations" from a systemic miss.
    """
    total_hits = sum(s.hits_05 for s in stats)
    total_n = sum(s.n for s in stats)
    if total_hits == 0 or total_n == 0:
        return 0.0, 0.0
    top2 = sorted(stats, key=lambda s: -s.hits_05)[:2]
    hit_share = sum(s.hits_05 for s in top2) / total_hits
    n_share = sum(s.n for s in top2) / total_n
    return hit_share, n_share
