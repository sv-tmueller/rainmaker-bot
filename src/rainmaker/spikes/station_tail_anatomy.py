"""Issue #296: per-station anatomy of the #289 addendum's headline finding --
two stations, KSFO and KNYC, account for the majority of TMAX lead 1-2's
broken lower tail. This diagnoses *why* at the station level (forecast-model
skill vs pooled-spread miscalibration) before any fix is proposed, and maps
the answer to one recommendation per station via rules frozen below, before
any number in this module is read.

Dead to the live path, like the two spikes it reads. Run it directly:

    uv run python -m rainmaker.spikes.station_tail_anatomy

`tail_objective.py` and `tmax_tail_diagnosis.py` are imported read-only
(`fetch_or_load_cell_data`, `DEFAULT_CACHE_PATH`, `apply_emos_regime`,
`pit_tail_ratios`, `distinct_backfill_stations` from the former;
`baseline_eval_residuals`, `ResidualRow`, `StationHitStat`,
`station_concentration`, `season_of` from the latter) and neither is ever
edited: both are frozen recorded artifacts, and a concurrent package ports
code from `tail_objective.py`, so a shared edit would race. Any other helper
this module needs (the season-pure refit fit/score, the raw-miss companion
series, the ASOS-control rule) is reimplemented locally rather than importing
a private (leading-underscore) helper from either spike, per the established
convention.

## Frozen bust definitions (verbatim from the architect's sub-plan)

**Primary bust definition**: the exact Diagnostic-B hit set from #289 --
baseline (Gaussian EMOS) eval-window standardized residual z with PIT < 0.05
(equivalently z < norm.ppf(0.05)), on the same 60/40 chronological split
`baseline_eval_residuals` rebuilds, at TMAX leads 1 and 2. This module's
anatomy explains literally the hits #289 counted; bust depth in degrees F is a
reported attribute of each hit, not a second bust definition.

**Companion series (seasonal coverage only)**: raw misses -- actual <=
mu_raw - 5.0 F, no calibration and no chronological split -- over the full
121-day archive window (2026-03-18 to 2026-07-16), because the primary
eval window (roughly 2026-05-28 to 2026-07-16) sits entirely inside
meteorological-summer (JJA) / late-spring marine-layer season and cannot by
itself confirm or rule out a seasonal mechanism.

## Frozen bust-anatomy classification

Each primary bust is one of:

- **forecast-type**: the actual value falls below every one of the 5
  Open-Meteo models' own daily extreme for that station/date/lead (the pooled
  mean could not have produced this value even at its most extreme member --
  a forecast-skill problem, not a spread problem).
- **spread-type**: the actual value falls at or above the envelope's minimum
  (some model reached it; the pooled mean/sigma reduction is what is
  overconfident -- a calibration/spread problem, not a forecast-skill one).

"Source" in this classification means "5-model Open-Meteo agreement", a
caveat repeated in the addendum: NWS and true ensemble-member agreement live
in the prod forecasts table, not recoverable here without the prod DSN.

A station classifies **spread-dominant** if >= 2/3 of its known-kind busts
(excluding any "unknown", i.e. missing-envelope, busts from the denominator)
are spread-type; **forecast-dominant** if >= 2/3 are forecast-type;
**mixed** otherwise; **insufficient-data** if every bust is "unknown"
(no envelope data recovered for any of them).

Each bust also reports its **required-sigma multiple**,
(mu_raw - actual) / sigma_raw: how many raw (multi-model-disagreement) sigma
the miss represents, independent of the envelope classification above.

## Frozen recommendation rules, per station classification

- **spread-dominant**: evidence bar = a single season-pure per-station refit
  check (JJA-only fit pairs before a newest-14-day eval window, mirroring
  `tmax_tail_diagnosis`'s own jja_season_pure arm but for one station) moves
  that station's lower-.05 PIT ratio into [0.5, 1.5] at both TMAX lead 1 and
  lead 2. Inside the bar at both leads -> "station-specific calibration
  adjustment"; missing or outside the bar at either lead -> "confidence
  penalty". This refit check runs *only* in this branch.
- **forecast-dominant**: calibration cannot fix a forecast-skill problem, so
  the choice is penalty vs exclusion, by a pre-stated severity cut: observed
  lower-.05 hit rate >= 4x nominal (>= 0.20) at *both* TMAX lead 1 and lead 2
  -> exclusion-grade; below that at either lead -> confidence-penalty-grade.
  Independently, if >= 75% of the station's full-window raw misses (the
  companion series) fall in the in-season range [2026-05-01, 2026-07-16] and
  the off-season arm is thin (< 60 distinct off-season days in the archive),
  the action is season-scoped (penalty or exclusion, plus an explicit
  off-season revisit date) rather than a permanent, all-season verdict.
- **mixed**: confidence penalty if the median bust depth >= 3.0 F; otherwise
  "no action, revisit at n >= REVISIT_MIN_N busts (or after REVISIT_DATE)".
- **insufficient-data**: no action; revisit once per-model envelope data is
  available for this station's busts.

The threshold numbers above (2/3, [0.5, 1.5], 4x/0.20, 75%/60 days, 3.0 F) are
the architect's proposal, frozen here verbatim before any live number in this
module is read; `REVISIT_MIN_N` and `REVISIT_DATE` are this module's own
named values for the "no action" branches (not tuned to any observed data):
`REVISIT_MIN_N = 60` doubles the sub-plan's own `MIN_CAL_SAMPLES` floor (so a
future revisit refit would itself clear that floor with margin), and
`REVISIT_DATE = 2026-11-15` is chosen for the same reason bullet 6 of the
sub-plan names for KSFO/KNYC's season-scoped verdicts: roughly 75 days into
meteorological autumn (SON, starting 2026-09-01), enough off-season accrual to
revisit a season-scoped verdict without waiting for a full SON season.

## Degradation clause

If the per-model refetch (below) fails for any target station, this module
degrades to **pool-level anatomy only**: per (station, lead) mean
required-sigma multiple among primary busts, plus mean raw sigma
(multi-model disagreement) at bust dates vs non-bust dates. No
forecast-vs-spread classification and no per-station recommendation are
produced in that case; the degradation is stated explicitly in the addendum
and in this module's stderr output.

## Per-model data: one new fetch variant, 4 stations, TMAX only

`fetch_historical_lead_forecasts` (in `backfill.py`) already receives
per-model hourly series from the Previous Runs API and reduces each
(lead, date) to a Gaussian (mean/stdev across models) before this module ever
sees it. `fetch_per_model_lead_extremes` below is the same endpoint, the same
daily-extreme reduction, but keeps each model's own extreme rather than
reducing it, for exactly 4 stations (KSFO, KNYC, KMDW, and the ASOS control
selected by the rule below), TMAX only, leads 1-2 -- 4 HTTP requests. It has
its own cache file (`DEFAULT_PER_MODEL_CACHE_PATH`, outside the repo, not
committed), separate from `tail_objective.DEFAULT_CACHE_PATH`.

The per-model fetch must cover the *frozen* window's explicit dates
(2026-03-18 to 2026-07-16), not a today-relative window: `verify_pooled_window`
is a trip-wire that aborts before any fetch if the *pooled* cache's own window
has drifted from the frozen dates (the same pattern
`tmax_tail_diagnosis.mam_arm_window_or_none` uses for a refetched archive).

## Controls

- **KMDW**: GHCND-settled, clean in #289's Diagnostic B (never appeared in
  its per-station table at either TMAX lead). Fixed by the sub-plan, not
  computed.
- **The ASOS control**: chosen by `select_asos_control`, a pre-stated
  deterministic rule -- minimize |hit rate - 0.05| summed over TMAX leads 1
  and 2 among ASOS-settled stations (`STATIONS`, the 11 domestic Polymarket
  stations), tie-break larger pooled n then alphabetical ICAO.
- **KLGA**: a free same-metro paired comparator for the KNYC hypothesis,
  already in the pooled cache (`klga_paired_comparison`, zero extra fetch).
  KLGA is itself mildly flagged at TMAX lead 2 in #289's Diagnostic B, so it
  is a paired comparator, not a clean control.

## Scope guard

TMAX only, leads 1-2. No TMIN fetches (KNYC's TMIN recurrence gets one
context line in the addendum, nothing more). No prod-DB reads: NWS/ensemble
source agreement is out of reach here, noted as a caveat. No live-path change
and no fix implementation; the deliverable is evidence and one recommendation
per station.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path
from tempfile import gettempdir
from typing import Any

import httpx
import numpy as np
from scipy.stats import norm

from rainmaker.backfill import PREVIOUS_RUNS_URL
from rainmaker.config import MIN_CAL_SAMPLES, MIN_SIGMA_F, OPENMETEO_MODELS, STATIONS, Station
from rainmaker.httpclient import build_client
from rainmaker.probability.calibration import CalibrationPair, fit_calibration
from rainmaker.spikes.tail_objective import (
    DEFAULT_CACHE_PATH,
    apply_emos_regime,
    distinct_backfill_stations,
    fetch_or_load_cell_data,
    pit_tail_ratios,
)
from rainmaker.spikes.tmax_tail_diagnosis import (
    ResidualRow,
    StationHitStat,
    baseline_eval_residuals,
    season_of,
    station_concentration,
)

CellData = dict[tuple[str, str, int], list[tuple[date, float, float, float]]]
PerModelData = dict[str, dict[int, dict[date, dict[str, float]]]]

# -----------------------------------------------------------------------------
# Frozen constants (see module docstring for the rules these implement)
# -----------------------------------------------------------------------------

FROZEN_WINDOW_START = date(2026, 3, 18)
FROZEN_WINDOW_END = date(2026, 7, 16)
TARGET_LEADS: tuple[int, ...] = (1, 2)  # TMAX leads 1-2, the diagnosis target

IN_SEASON_START = date(2026, 5, 1)
IN_SEASON_END = FROZEN_WINDOW_END  # 2026-07-16, per the sub-plan's season-scope test
RAW_MISS_THRESHOLD_F = 5.0
Q05 = float(norm.ppf(0.05))

ASOS_ICAOS: frozenset[str] = frozenset(s.icao for s in STATIONS.values())

DEFAULT_PER_MODEL_CACHE_PATH = (
    Path(gettempdir()) / "rainmaker_station_tail_anatomy_per_model_cache.json"
)

EXCLUSION_HIT_RATE_MULTIPLE = 4.0  # observed Lo.05 hit rate >= this * 0.05 -> exclusion-grade
SEASON_SCOPE_RAW_MISS_FRACTION = 0.75
SEASON_SCOPE_MIN_OFF_SEASON_DAYS = 60
REFIT_LO05_LOW, REFIT_LO05_HIGH = 0.5, 1.5
MIXED_PENALTY_DEPTH_F = 3.0
REVISIT_MIN_N = 60  # see module docstring: 2x MIN_CAL_SAMPLES
REVISIT_DATE = date(2026, 11, 15)  # see module docstring: ~75 days into SON
REFIT_EVAL_DAYS = 14  # mirrors tmax_tail_diagnosis.JJA_EVAL_DAYS

# -----------------------------------------------------------------------------
# Data classes
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class BustRow:
    icao: str
    lead: int
    target_date: date
    mu_raw: float
    sigma_raw: float
    actual: float
    z: float
    pit: float
    depth_f: float  # mu_raw - actual; positive = actual below forecast
    required_sigma: float  # depth_f / sigma_raw
    season: str
    kind: str  # "forecast-type" / "spread-type" / "unknown"
    envelope_min: float | None = None  # coldest of the 5 models' own daily extreme, if known
    envelope_max: float | None = None  # warmest of the 5 models' own daily extreme, if known


@dataclass(frozen=True)
class RawMissRow:
    icao: str
    lead: int
    target_date: date
    depth_f: float
    season: str


@dataclass(frozen=True)
class PoolLevelAnatomy:
    icao: str
    lead: int
    n_bust: int
    mean_required_sigma: float
    mean_sigma_bust: float
    mean_sigma_non_bust: float


@dataclass(frozen=True)
class KlgaPairedRow:
    target_date: date
    lead: int
    knyc_actual: float
    klga_actual: float
    actual_delta: float  # knyc_actual - klga_actual
    klga_mu_raw: float
    klga_depth_f: float  # klga_mu_raw - klga_actual, KLGA's own raw miss depth that date
    klga_raw_miss: bool  # KLGA's own depth clears RAW_MISS_THRESHOLD_F that date


@dataclass(frozen=True)
class StationRecommendation:
    icao: str
    classification: str
    n_known: int
    n_forecast_type: int
    n_spread_type: int
    n_unknown: int
    median_depth_f: float | None
    refit_lo05: dict[int, float | None]
    action: str
    rationale: str


# -----------------------------------------------------------------------------
# Trip-wire: the pooled cache must be the frozen window
# -----------------------------------------------------------------------------


def verify_pooled_window(cell_data: CellData) -> None:
    """Abort if the pooled cache's own window has drifted from the frozen
    dates (2026-03-18 to 2026-07-16). Mirrors the trip-wire pattern
    `tmax_tail_diagnosis.mam_arm_window_or_none` uses for a refetched archive,
    stated in the module docstring.
    """
    all_dates = [d for rows in cell_data.values() for d, *_rest in rows]
    if not all_dates:
        raise ValueError("pooled cache is empty")
    data_start, window_end = min(all_dates), max(all_dates)
    if (data_start, window_end) != (FROZEN_WINDOW_START, FROZEN_WINDOW_END):
        raise ValueError(
            f"pooled cache window {data_start.isoformat()}..{window_end.isoformat()} does not "
            f"match the frozen window {FROZEN_WINDOW_START.isoformat()}.."
            f"{FROZEN_WINDOW_END.isoformat()}; refusing to run (see module docstring's trip-wire)"
        )


# -----------------------------------------------------------------------------
# Primary busts (Diagnostic-B hit set) and the raw-miss companion series
# -----------------------------------------------------------------------------


def primary_busts(
    cell_data: CellData,
    residuals: list[ResidualRow],
    icaos: frozenset[str],
    leads: tuple[int, ...] = TARGET_LEADS,
) -> list[BustRow]:
    """The exact Diagnostic-B hit set (baseline eval-window z < Q05), for the
    given stations and leads, joined back to the raw (mu, sigma, actual)
    triple so each bust carries its depth and required-sigma multiple.
    `kind` starts as "unknown"; `attach_envelope_kinds` fills it in.
    """
    raw_lookup: dict[tuple[str, int, date], tuple[float, float, float]] = {}
    for (icao, variable, lead), rows in cell_data.items():
        if variable != "TMAX" or icao not in icaos or lead not in leads:
            continue
        for d, mu, sigma, actual in rows:
            raw_lookup[(icao, lead, d)] = (mu, sigma, actual)

    out: list[BustRow] = []
    for r in residuals:
        if r.variable != "TMAX" or r.icao not in icaos or r.lead not in leads or r.z >= Q05:
            continue
        key = (r.icao, r.lead, r.target_date)
        if key not in raw_lookup:
            continue
        mu, sigma, actual = raw_lookup[key]
        _year, _month, season_name = season_of(r.target_date)
        out.append(
            BustRow(
                icao=r.icao,
                lead=r.lead,
                target_date=r.target_date,
                mu_raw=mu,
                sigma_raw=sigma,
                actual=actual,
                z=r.z,
                pit=float(norm.cdf(r.z)),
                depth_f=mu - actual,
                required_sigma=(mu - actual) / sigma,
                season=season_name,
                kind="unknown",
            )
        )
    return out


def raw_misses(
    rows: list[tuple[date, float, float, float]], icao: str, lead: int
) -> list[RawMissRow]:
    """Companion series (seasonal coverage only, see module docstring): actual
    <= mu_raw - RAW_MISS_THRESHOLD_F, no calibration, no chronological split,
    over whatever window `rows` spans.
    """
    out: list[RawMissRow] = []
    for d, mu, _sigma, actual in rows:
        if actual <= mu - RAW_MISS_THRESHOLD_F:
            _year, _month, season_name = season_of(d)
            out.append(
                RawMissRow(
                    icao=icao, lead=lead, target_date=d, depth_f=mu - actual, season=season_name
                )
            )
    return out


def raw_miss_season_summary(
    rows_by_lead: dict[int, list[tuple[date, float, float, float]]], icao: str
) -> tuple[float | None, int]:
    """(fraction of the station's full-window raw misses that fall in
    [IN_SEASON_START, IN_SEASON_END], count of distinct off-season days
    present in the archive), pooling every lead in `rows_by_lead`. Fraction is
    None when the station has no raw misses at all (nothing to take a
    fraction of).
    """
    misses: list[RawMissRow] = []
    off_season_days: set[date] = set()
    for lead, rows in rows_by_lead.items():
        misses.extend(raw_misses(rows, icao, lead))
        off_season_days.update(d for d, *_rest in rows if d < IN_SEASON_START or d > IN_SEASON_END)
    if not misses:
        return None, len(off_season_days)
    in_season = sum(1 for m in misses if IN_SEASON_START <= m.target_date <= IN_SEASON_END)
    return in_season / len(misses), len(off_season_days)


# -----------------------------------------------------------------------------
# Envelope in/out classification (forecast-type vs spread-type)
# -----------------------------------------------------------------------------


def classify_bust_kind(actual: float, model_extremes: dict[str, float]) -> str:
    """ "forecast-type" if `actual` falls below every model's own daily
    extreme (the pool could not have produced this value at all);
    "spread-type" if `actual` is at or above the envelope's minimum (some
    model reached it, so the pooled reduction, not any single model, is
    overconfident). Raises if no envelope data is available for this bust;
    callers route that case to "unknown" instead (see `attach_envelope_kinds`).
    """
    if not model_extremes:
        raise ValueError("cannot classify a bust with no model envelope data")
    return "forecast-type" if actual < min(model_extremes.values()) else "spread-type"


def attach_envelope_kinds(busts: list[BustRow], per_model_data: PerModelData) -> list[BustRow]:
    """Fill in each bust's `kind` (and envelope_min/max) from the per-model
    envelope, "unknown" (with envelope bounds left None) when no envelope was
    recovered for that (station, lead, date).
    """
    out: list[BustRow] = []
    for b in busts:
        envelope = per_model_data.get(b.icao, {}).get(b.lead, {}).get(b.target_date)
        if envelope:
            kind = classify_bust_kind(b.actual, envelope)
            out.append(
                replace(
                    b,
                    kind=kind,
                    envelope_min=min(envelope.values()),
                    envelope_max=max(envelope.values()),
                )
            )
        else:
            out.append(replace(b, kind="unknown"))
    return out


def classify_station_anatomy(kinds: list[str]) -> str:
    """Station-level classification from its busts' kinds (see module
    docstring): "unknown" entries are excluded from the denominator, not
    counted as either type.
    """
    known = [k for k in kinds if k != "unknown"]
    if not known:
        return "insufficient-data"
    n = len(known)
    spread_frac = known.count("spread-type") / n
    forecast_frac = known.count("forecast-type") / n
    if spread_frac >= 2.0 / 3.0:
        return "spread-dominant"
    if forecast_frac >= 2.0 / 3.0:
        return "forecast-dominant"
    return "mixed"


# -----------------------------------------------------------------------------
# Control-selection rule (deterministic ASOS control)
# -----------------------------------------------------------------------------


def select_asos_control(
    stats_lead1: list[StationHitStat], stats_lead2: list[StationHitStat]
) -> str:
    """Minimize |hit rate - 0.05| summed over TMAX leads 1-2 among ASOS
    stations (`ASOS_ICAOS`), tie-break larger pooled n then alphabetical
    ICAO. See module docstring's Controls section.
    """
    by_1 = {s.icao: s for s in stats_lead1}
    by_2 = {s.icao: s for s in stats_lead2}
    candidates = ASOS_ICAOS & set(by_1) & set(by_2)
    if not candidates:
        raise ValueError("no ASOS station has StationHitStat data at both TMAX leads 1 and 2")
    scored: list[tuple[float, int, str]] = []
    for icao in candidates:
        s1, s2 = by_1[icao], by_2[icao]
        rate1 = s1.hits_05 / s1.n if s1.n else 1.0
        rate2 = s2.hits_05 / s2.n if s2.n else 1.0
        score = abs(rate1 - 0.05) + abs(rate2 - 0.05)
        total_n = s1.n + s2.n
        scored.append((score, -total_n, icao))
    scored.sort()
    return scored[0][2]


# -----------------------------------------------------------------------------
# KLGA paired-date comparison (KNYC hypothesis, zero extra fetch)
# -----------------------------------------------------------------------------


def klga_paired_comparison(cell_data: CellData, knyc_busts: list[BustRow]) -> list[KlgaPairedRow]:
    """For each KNYC primary bust, KLGA's own raw (mu, sigma, actual) on the
    same date/lead, if the pooled cache has it (it always does for the
    frozen window: both are Polymarket-eligible archive stations already
    fetched for the comparison in #284/#289).
    """
    out: list[KlgaPairedRow] = []
    for b in knyc_busts:
        klga_rows = {
            d: (mu, sigma, actual)
            for d, mu, sigma, actual in cell_data.get(("KLGA", "TMAX", b.lead), [])
        }
        if b.target_date not in klga_rows:
            continue
        klga_mu, _klga_sigma, klga_actual = klga_rows[b.target_date]
        klga_depth = klga_mu - klga_actual
        out.append(
            KlgaPairedRow(
                target_date=b.target_date,
                lead=b.lead,
                knyc_actual=b.actual,
                klga_actual=klga_actual,
                actual_delta=b.actual - klga_actual,
                klga_mu_raw=klga_mu,
                klga_depth_f=klga_depth,
                klga_raw_miss=klga_depth >= RAW_MISS_THRESHOLD_F,
            )
        )
    return out


# -----------------------------------------------------------------------------
# Pool-level anatomy (degradation-clause fallback; also useful context always)
# -----------------------------------------------------------------------------


def pool_level_anatomy_for_cell(
    icao: str, lead: int, rows: list[tuple[date, float, float, float]], busts: list[BustRow]
) -> PoolLevelAnatomy:
    """Mean required-sigma multiple among this cell's primary busts, plus mean
    raw sigma (multi-model disagreement) at bust dates vs non-bust dates: the
    degradation-clause fallback when the per-model refetch fails, and useful
    context regardless.
    """
    cell_busts = [b for b in busts if b.icao == icao and b.lead == lead]
    bust_dates = {b.target_date for b in cell_busts}
    bust_required = [b.required_sigma for b in cell_busts]
    sigma_bust = [sigma for d, _mu, sigma, _actual in rows if d in bust_dates]
    sigma_non_bust = [sigma for d, _mu, sigma, _actual in rows if d not in bust_dates]
    return PoolLevelAnatomy(
        icao=icao,
        lead=lead,
        n_bust=len(bust_required),
        mean_required_sigma=float(np.mean(bust_required)) if bust_required else 0.0,
        mean_sigma_bust=float(np.mean(sigma_bust)) if sigma_bust else 0.0,
        mean_sigma_non_bust=float(np.mean(sigma_non_bust)) if sigma_non_bust else 0.0,
    )


# -----------------------------------------------------------------------------
# Season-pure per-station refit check (spread-dominant branch only)
# -----------------------------------------------------------------------------


def season_pure_refit_lower05(
    rows: list[tuple[date, float, float, float]],
    icao: str,
    variable: str,
    lead: int,
    window_end: date = FROZEN_WINDOW_END,
) -> float | None:
    """Season-pure (JJA-only) refit for one station/lead, mirroring
    `tmax_tail_diagnosis`'s own jja_season_pure arm construction (fit on
    JJA-only pairs before the eval window, eval on the newest
    REFIT_EVAL_DAYS days) but for a single station rather than the pooled
    13-station version. Returns the eval-window lower-.05 PIT ratio, or None
    if the station's JJA fit sample falls below MIN_CAL_SAMPLES (gated, same
    floor Diagnostic C itself uses): the refit check is inconclusive in that
    case, and `recommend_for_station` falls back to a confidence penalty per
    the frozen rule.
    """
    eval_start = window_end - timedelta(days=REFIT_EVAL_DAYS - 1)
    fit_end = eval_start - timedelta(days=1)
    season_year, season_month, _name = season_of(window_end)
    season_start = date(season_year, season_month, 1)
    fit_rows = [r for r in rows if season_start <= r[0] <= fit_end]
    eval_rows = [r for r in rows if eval_start <= r[0] <= window_end]
    if len(fit_rows) < MIN_CAL_SAMPLES or not eval_rows:
        return None
    cal_pairs = [
        CalibrationPair(mu=mu, sigma=sigma, ensemble_var=sigma**2, actual=actual)
        for _d, mu, sigma, actual in fit_rows
    ]
    cal = fit_calibration(icao, variable, lead, cal_pairs)
    pits: list[float] = []
    for _d, mu, sigma, actual in eval_rows:
        loc, scale, _df = apply_emos_regime(
            bias=cal.bias,
            var_a=cal.var_a,
            var_b=cal.var_b,
            df=None,
            n_samples=cal.n_samples,
            mu=mu,
            ensemble_var=sigma**2,
            min_sigma=MIN_SIGMA_F,
            fallback_df=None,
        )
        z = (actual - loc) / scale
        pits.append(float(norm.cdf(z)))
    ratios = pit_tail_ratios(pits)
    return float(ratios["lower_05"])


# -----------------------------------------------------------------------------
# Recommendation mapping (the frozen decision tree)
# -----------------------------------------------------------------------------


def recommend_for_station(
    icao: str,
    kinds: list[str],
    depths_f: list[float],
    hit_rate_lead1: float,
    hit_rate_lead2: float,
    raw_miss_in_season_frac: float | None,
    off_season_day_count: int,
    refit_lo05: dict[int, float | None],
) -> StationRecommendation:
    """Map evidence to one recommendation per station via the frozen rules in
    the module docstring. `refit_lo05` is only meaningful (non-empty) for a
    spread-dominant station; it is ignored otherwise.
    """
    known = [k for k in kinds if k != "unknown"]
    n_known = len(known)
    n_forecast = known.count("forecast-type")
    n_spread = known.count("spread-type")
    n_unknown = len(kinds) - n_known
    classification = classify_station_anatomy(kinds)
    median_depth = float(np.median(depths_f)) if depths_f else None

    if classification == "spread-dominant":
        refit_values = list(refit_lo05.values())
        refit_passes = bool(refit_values) and all(
            v is not None and REFIT_LO05_LOW <= v <= REFIT_LO05_HIGH for v in refit_values
        )
        refit_desc = (
            ", ".join(
                f"lead {lead}: {v:.2f}" if v is not None else f"lead {lead}: n/a"
                for lead, v in sorted(refit_lo05.items())
            )
            or "not computed"
        )
        if refit_passes:
            action = "station-specific calibration adjustment"
            rationale = (
                f"spread-dominant ({n_spread}/{n_known} spread-type); season-pure refit "
                f"({refit_desc}) lands inside the evidence bar "
                f"[{REFIT_LO05_LOW}, {REFIT_LO05_HIGH}] at every checked lead"
            )
        else:
            action = "confidence penalty"
            rationale = (
                f"spread-dominant ({n_spread}/{n_known} spread-type); season-pure refit "
                f"({refit_desc}) misses the evidence bar [{REFIT_LO05_LOW}, {REFIT_LO05_HIGH}] "
                "at one or more leads; falling back to a confidence penalty per the frozen rule"
            )
    elif classification == "forecast-dominant":
        exclusion_grade = (
            hit_rate_lead1 >= EXCLUSION_HIT_RATE_MULTIPLE * 0.05
            and hit_rate_lead2 >= EXCLUSION_HIT_RATE_MULTIPLE * 0.05
        )
        base_action = "exclusion" if exclusion_grade else "confidence penalty"
        season_scoped = (
            raw_miss_in_season_frac is not None
            and raw_miss_in_season_frac >= SEASON_SCOPE_RAW_MISS_FRACTION
            and off_season_day_count < SEASON_SCOPE_MIN_OFF_SEASON_DAYS
        )
        action = (
            f"season-scoped {base_action}, revisit after {REVISIT_DATE.isoformat()} (SON accrual)"
            if season_scoped
            else base_action
        )
        raw_desc = (
            f"{raw_miss_in_season_frac:.0%} of raw misses in-season, {off_season_day_count} "
            "off-season days of archive data"
            if raw_miss_in_season_frac is not None
            else "no raw-miss data"
        )
        rationale = (
            f"forecast-dominant ({n_forecast}/{n_known} forecast-type); lead1 hit rate "
            f"{hit_rate_lead1:.0%}, lead2 hit rate {hit_rate_lead2:.0%} vs the "
            f"{EXCLUSION_HIT_RATE_MULTIPLE:.0f}x-nominal exclusion cut "
            f"({EXCLUSION_HIT_RATE_MULTIPLE * 0.05:.0%}); {raw_desc}"
        )
    elif classification == "mixed":
        depth_desc = f"{median_depth:.1f}F" if median_depth is not None else "n/a"
        if median_depth is not None and median_depth >= MIXED_PENALTY_DEPTH_F:
            action = "confidence penalty"
        else:
            revisit_desc = REVISIT_DATE.isoformat()
            action = f"no action, revisit at n >= {REVISIT_MIN_N} busts (or after {revisit_desc})"
        rationale = (
            f"mixed ({n_forecast}/{n_known} forecast-type, {n_spread}/{n_known} spread-type); "
            f"median bust depth {depth_desc} vs the {MIXED_PENALTY_DEPTH_F}F penalty cut"
        )
    else:
        action = "no action, revisit once per-model envelope data is available"
        rationale = "insufficient per-model envelope data to classify any bust"

    return StationRecommendation(
        icao=icao,
        classification=classification,
        n_known=n_known,
        n_forecast_type=n_forecast,
        n_spread_type=n_spread,
        n_unknown=n_unknown,
        median_depth_f=median_depth,
        refit_lo05=refit_lo05,
        action=action,
        rationale=rationale,
    )


# -----------------------------------------------------------------------------
# Per-model fetch variant (new, sibling-only): retains dict[lead][date][model]
# -----------------------------------------------------------------------------


def fetch_per_model_lead_extremes(
    station: Station,
    leads: tuple[int, ...],
    start: date,
    end: date,
    client: httpx.Client,
    variable: str = "TMAX",
) -> dict[int, dict[date, dict[str, float]]]:
    """Per-lead, per-date, per-model daily extreme (TMAX: max of the hourly
    series) from the Previous Runs API, the same endpoint and daily-extreme
    reduction as `backfill.fetch_historical_lead_forecasts`, but keeping each
    model's own extreme rather than reducing across models to a Gaussian.
    Raises on HTTP error, matching the convention every other fetch function
    in these two spikes follows.
    """
    fields = [f"temperature_2m_previous_day{lead}" for lead in leads]
    resp = client.get(
        PREVIOUS_RUNS_URL,
        params={
            "latitude": str(station.lat),
            "longitude": str(station.lon),
            "hourly": ",".join(fields),
            "temperature_unit": "fahrenheit",
            "timezone": station.timezone,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "models": ",".join(OPENMETEO_MODELS),
        },
    )
    resp.raise_for_status()
    body = resp.json()
    if "hourly" not in body:
        raise ValueError(
            f"'hourly' key missing from Open-Meteo response: {body.get('reason', body)!r}"
        )
    hourly: dict[str, Any] = body["hourly"]
    times = hourly["time"]
    reduce = max if variable == "TMAX" else min
    out: dict[int, dict[date, dict[str, float]]] = {}
    for lead in leads:
        suffix = "" if lead == 0 else f"_previous_day{lead}"
        per_day: dict[date, dict[str, float]] = {}
        for model in OPENMETEO_MODELS:
            values = hourly.get(f"temperature_2m{suffix}_{model}")
            if values is None:
                continue
            by_day: dict[date, list[float]] = {}
            for iso, value in zip(times, values, strict=True):
                if value is None:
                    continue
                by_day.setdefault(date.fromisoformat(iso[:10]), []).append(value)
            for day, hours in by_day.items():
                per_day.setdefault(day, {})[model] = reduce(hours)
        out[lead] = per_day
    return out


def _save_per_model_cache(cache_path: Path, data: PerModelData) -> None:
    serializable = {
        icao: {
            str(lead): {d.isoformat(): models for d, models in per_day.items()}
            for lead, per_day in by_lead.items()
        }
        for icao, by_lead in data.items()
    }
    cache_path.write_text(json.dumps(serializable))


def _load_per_model_cache(cache_path: Path) -> PerModelData:
    raw = json.loads(cache_path.read_text())
    return {
        icao: {
            int(lead): {date.fromisoformat(d): models for d, models in per_day.items()}
            for lead, per_day in by_lead.items()
        }
        for icao, by_lead in raw.items()
    }


def fetch_or_load_per_model_data(
    stations: list[Station],
    leads: tuple[int, ...],
    start: date,
    end: date,
    cache_path: Path = DEFAULT_PER_MODEL_CACHE_PATH,
    *,
    force_refetch: bool = False,
) -> PerModelData:
    """Load the cached per-model envelope data if present, else fetch (4
    requests, one per target station) and cache it. Raises on HTTP error;
    `main` catches that and applies the degradation clause.
    """
    if not force_refetch and cache_path.exists():
        print(f"loading cached per-model envelope data from {cache_path}", file=sys.stderr)
        return _load_per_model_cache(cache_path)
    client = build_client(60.0)
    out: PerModelData = {}
    try:
        for station in stations:
            out[station.icao] = fetch_per_model_lead_extremes(
                station, leads, start, end, client, "TMAX"
            )
    finally:
        client.close()
    _save_per_model_cache(cache_path, out)
    n_pairs = sum(len(per_day) for by_lead in out.values() for per_day in by_lead.values())
    print(f"cached {n_pairs} per-model envelope rows to {cache_path}", file=sys.stderr)
    return out


# -----------------------------------------------------------------------------
# Rendering
# -----------------------------------------------------------------------------


def render_bust_table(busts: list[BustRow]) -> str:
    header = (
        "| Station | Lead | Date | Depth (F) | PIT | Required-sigma | Envelope [min, max] | "
        "In/Out | Season |"
    )
    rule = "| --- " * 9 + "|"
    lines = [header, rule]
    for b in sorted(busts, key=lambda x: (x.icao, x.lead, x.target_date)):
        envelope_desc = (
            f"[{b.envelope_min:.1f}, {b.envelope_max:.1f}]"
            if b.envelope_min is not None and b.envelope_max is not None
            else "-"
        )
        lines.append(
            f"| {b.icao} | {b.lead} | {b.target_date.isoformat()} | {b.depth_f:.1f} | "
            f"{b.pit:.3f} | {b.required_sigma:.2f} | {envelope_desc} | {b.kind} | {b.season} |"
        )
    return "\n".join(lines) + "\n"


def render_recommendations(recs: list[StationRecommendation]) -> str:
    header = (
        "| Station | Classification | Known | Forecast-type | Spread-type | Unknown | "
        "Median depth (F) | Action |"
    )
    rule = "| --- " * 8 + "|"
    lines = [header, rule]
    for r in sorted(recs, key=lambda x: x.icao):
        depth_desc = f"{r.median_depth_f:.1f}" if r.median_depth_f is not None else "-"
        lines.append(
            f"| {r.icao} | {r.classification} | {r.n_known} | {r.n_forecast_type} | "
            f"{r.n_spread_type} | {r.n_unknown} | {depth_desc} | {r.action} |"
        )
    lines.append("")
    for r in sorted(recs, key=lambda x: x.icao):
        lines.append(f"- **{r.icao}**: {r.rationale}")
    return "\n".join(lines) + "\n"


def render_klga_paired(rows: list[KlgaPairedRow]) -> str:
    header = "| Date | Lead | KNYC actual | KLGA actual | Delta | KLGA depth (F) | KLGA raw miss |"
    rule = "| --- " * 7 + "|"
    lines = [header, rule]
    for row in sorted(rows, key=lambda x: (x.lead, x.target_date)):
        lines.append(
            f"| {row.target_date.isoformat()} | {row.lead} | {row.knyc_actual:.1f} | "
            f"{row.klga_actual:.1f} | {row.actual_delta:+.1f} | {row.klga_depth_f:.1f} | "
            f"{row.klga_raw_miss} |"
        )
    return "\n".join(lines) + "\n"


def render_pool_level(anatomies: list[PoolLevelAnatomy]) -> str:
    header = (
        "| Station | Lead | n bust | Mean required-sigma | Mean sigma (bust) | "
        "Mean sigma (non-bust) |"
    )
    rule = "| --- " * 6 + "|"
    lines = [header, rule]
    for a in sorted(anatomies, key=lambda x: (x.icao, x.lead)):
        lines.append(
            f"| {a.icao} | {a.lead} | {a.n_bust} | {a.mean_required_sigma:.2f} | "
            f"{a.mean_sigma_bust:.2f} | {a.mean_sigma_non_bust:.2f} |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    cell_data = fetch_or_load_cell_data(DEFAULT_CACHE_PATH)
    verify_pooled_window(cell_data)
    residuals = baseline_eval_residuals(cell_data)

    stats_lead1 = station_concentration(residuals, "TMAX", 1)
    stats_lead2 = station_concentration(residuals, "TMAX", 2)
    asos_control = select_asos_control(stats_lead1, stats_lead2)
    print(f"ASOS control station: {asos_control}", file=sys.stderr)

    target_icaos: tuple[str, ...] = ("KSFO", "KNYC", "KMDW", asos_control)
    stations_by_icao = {s.icao: s for s in distinct_backfill_stations()}
    target_stations = [stations_by_icao[icao] for icao in target_icaos]

    degraded = False
    per_model_data: PerModelData | None
    try:
        per_model_data = fetch_or_load_per_model_data(
            target_stations, TARGET_LEADS, FROZEN_WINDOW_START, FROZEN_WINDOW_END
        )
    except (httpx.HTTPError, ValueError) as exc:
        print(
            f"per-model refetch failed, degrading to pool-level anatomy only: {exc}",
            file=sys.stderr,
        )
        degraded = True
        per_model_data = None

    busts = primary_busts(cell_data, residuals, frozenset(target_icaos))
    if per_model_data is not None:
        busts = attach_envelope_kinds(busts, per_model_data)

    window_desc = f"{FROZEN_WINDOW_START.isoformat()} to {FROZEN_WINDOW_END.isoformat()}"
    print(f"archive window: {window_desc}")
    print(f"\nASOS control: {asos_control}\n")

    print("## Bust table (primary Diagnostic-B hit set, TMAX leads 1-2)\n")
    print(render_bust_table(busts))

    if degraded:
        print("## Degraded: pool-level anatomy only (per-model refetch failed)\n")
        pool_anatomies = [
            pool_level_anatomy_for_cell(icao, lead, cell_data.get((icao, "TMAX", lead), []), busts)
            for icao in target_icaos
            for lead in TARGET_LEADS
        ]
        print(render_pool_level(pool_anatomies))
    else:
        recs = []
        by_icao_1 = {s.icao: s for s in stats_lead1}
        by_icao_2 = {s.icao: s for s in stats_lead2}
        for icao in target_icaos:
            rows_by_lead = {lead: cell_data.get((icao, "TMAX", lead), []) for lead in TARGET_LEADS}
            station_busts = [b for b in busts if b.icao == icao]
            kinds = [b.kind for b in station_busts]
            depths = [b.depth_f for b in station_busts]
            s1, s2 = by_icao_1.get(icao), by_icao_2.get(icao)
            hit_rate1 = (s1.hits_05 / s1.n) if s1 and s1.n else 0.0
            hit_rate2 = (s2.hits_05 / s2.n) if s2 and s2.n else 0.0
            raw_frac, off_days = raw_miss_season_summary(rows_by_lead, icao)

            refit_lo05: dict[int, float | None] = {}
            if classify_station_anatomy(kinds) == "spread-dominant":
                for lead in TARGET_LEADS:
                    refit_lo05[lead] = season_pure_refit_lower05(
                        rows_by_lead[lead], icao, "TMAX", lead
                    )

            recs.append(
                recommend_for_station(
                    icao=icao,
                    kinds=kinds,
                    depths_f=depths,
                    hit_rate_lead1=hit_rate1,
                    hit_rate_lead2=hit_rate2,
                    raw_miss_in_season_frac=raw_frac,
                    off_season_day_count=off_days,
                    refit_lo05=refit_lo05,
                )
            )
        print("## Recommendations\n")
        print(render_recommendations(recs))

    knyc_busts = [b for b in busts if b.icao == "KNYC"]
    print("## KLGA paired-date comparison (KNYC hypothesis)\n")
    print(render_klga_paired(klga_paired_comparison(cell_data, knyc_busts)))


if __name__ == "__main__":
    main()
