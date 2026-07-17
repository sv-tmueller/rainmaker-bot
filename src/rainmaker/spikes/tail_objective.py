"""Spike #284: does a heavier-tailed family or a tail-weighted objective fix the
broken lower tail (TMAX lead 1, TMIN leads 0-1; see #244) better than the live
Gaussian-CRPS EMOS baseline, without degrading the upper tail or the body?

Dead to the live path: nothing here is imported by cli.py or any live-path
module. Run it directly:

    uv run python -m rainmaker.spikes.tail_objective

Findings are written up in docs/architecture/tail-objective-decision.md, not
read back by any other code. distribution.py, calibration.py, outcomes.py,
backfill.py, backtest.py, tracking.py and cli.py are imported here read-only
(reusing the baseline fit/apply path and a few pure helpers) and are never
edited or monkeypatched.

## Scoring engine

One generic numeric integral serves as both the CRPS and the twCRPS objective
(Allen et al., arXiv:2407.03167):

    CRPS(F, y) = integral over x of w(x) * (F(x) - 1{y <= x})^2 dx,  w = 1 for CRPS

`numeric_crps` computes this via the substitution x = loc + scale*u (affine,
exact under the trapezoid rule up to the same truncation as evaluating the
untransformed integral over loc +/- span*scale): the integral becomes

    scale * integral over u of w(loc + scale*u) * (Fstd(u) - 1{u >= (y-loc)/scale})^2 du

with Fstd the *standardized* CDF (mean 0, scale 1). That lets every candidate
share one u-grid across an entire batch of (loc, scale, actual) triples, which
is what makes fitting by numeric-CRPS minimization fast enough to run over
dozens of (station, variable, lead) cells in one process.

## Candidates

1. baseline: the live path's own `fit_calibration` + `apply_calibration`
   (Gaussian EMOS, closed-form-CRPS fit). Imported, not reimplemented.
2. t-fixed-df: Student-t EMOS, same (bias, var_a, var_b) parametrization as the
   baseline, df held fixed (primary df=5, sensitivity df=8), fit by minimizing
   mean numeric CRPS.
3. t-free-df: Student-t EMOS with df as a 4th fit parameter, parametrized as
   log(df - 2) (bounded so df stays in (2, 62], keeping the predictive variance
   finite and the numeric integral well-behaved).
4. twcrps-gaussian: stays in the Gaussian family, fit by minimizing mean numeric
   *twCRPS* with a two-sided indicator tail weight: `multiplier` beyond the
   fit-window actuals' climatological q10/q90, 1.0 inside (primary
   multiplier=5.0, sensitivity multiplier=10.0). The weight multiplies rather
   than zeroes the body so the fit still sees body information.

## n-gating

`apply_emos_regime` mirrors calibration.apply_calibration's three regimes
(uncalibrated / bias_only / full) so every candidate is judged from the same
population size per cell, not just the ones with enough history for a full fit.

## Evaluation design

Single chronological split (fit oldest 60%, eval newest 40%) per (station,
variable, lead) cell, then eval-window scores pooled per (variable, lead)
across stations. This is the sub-plan's documented fallback from the full
weekly-anchor walk-forward: with the scoring engine, four fitters, and their
sanity tests as the session's first priority, the walk-forward's extra
indexing complexity was cut on purpose to leave room for a real live-data run
and the decision doc, per the #283 size trip-wire. A single out-of-sample
split still answers the out-of-sample question (in-sample would automatically
favor the more flexible t-with-fitted-df); it just has fewer, larger eval
windows than a weekly walk-forward would.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from math import sqrt
from pathlib import Path
from tempfile import gettempdir
from typing import Any

import httpx
import numpy as np
from numpy.typing import NDArray
from scipy.optimize import minimize
from scipy.stats import norm, t

from rainmaker.backfill import fetch_historical_lead_forecasts, venue_actuals
from rainmaker.backtest import reliability_bins, standard_buckets
from rainmaker.config import (
    KALSHI_STATIONS,
    MIN_CAL_BIAS_SAMPLES,
    MIN_CAL_SAMPLES,
    MIN_SIGMA_F,
    STATIONS,
    UNCALIBRATED_WIDEN,
    Station,
)
from rainmaker.domain import Bucket
from rainmaker.httpclient import build_client
from rainmaker.probability.calibration import Calibration, CalibrationPair, fit_calibration
from rainmaker.probability.outcomes import settles

LEADS: tuple[int, ...] = (0, 1, 2)
VARIABLES: tuple[str, ...] = ("TMAX", "TMIN")
FETCH_DAYS = 120  # ~120-day span per the sub-plan
FIT_FRACTION = 0.60  # single chronological split: fit oldest 60%, eval newest 40%
MIN_CELL_PAIRS = 10  # below this, a (station, variable, lead) cell is skipped entirely
FIT_MIN_SIGMA = 1.0  # floor on the *fitted* predictive sigma (degrees F); see fit_* docstrings

DEFAULT_CACHE_PATH = Path(gettempdir()) / "rainmaker_tail_objective_cache.json"

FitPair = tuple[float, float, float]  # (raw mu, ensemble_var, actual)

_MAX_GRID = 400_001  # hard cap on numeric_crps's widened grid size; see numeric_crps docstring
_MAX_MATRIX_ELEMENTS = 20_000_000  # cap on (batch size * grid size); bounds memory/time per call

# -----------------------------------------------------------------------------
# Data fetch + cache
# -----------------------------------------------------------------------------


def distinct_backfill_stations() -> list[Station]:
    """Every settlement station across venues, deduped by icao.

    Mirrors cli._distinct_stations (not imported: cli.py stays untouched by
    construction, and this is 4 lines).
    """
    out: dict[str, Station] = {}
    for station in (*STATIONS.values(), *KALSHI_STATIONS.values()):
        out.setdefault(station.icao, station)
    return sorted(out.values(), key=lambda s: s.icao)


def fetch_cell_data(
    stations: list[Station],
    variables: tuple[str, ...],
    leads: tuple[int, ...],
    end: date,
    days: int,
    client: httpx.Client,
) -> dict[tuple[str, str, int], list[tuple[date, float, float, float]]]:
    """(icao, variable, lead) -> sorted [(date, mu, sigma, actual), ...].

    One Previous Runs request per (station, variable) covers every lead (mirrors
    backfill.run_backfill); one venue_actuals request per (station, variable).
    A station/variable that fails to fetch is skipped with a stderr note rather
    than aborting the whole run.
    """
    start = end - timedelta(days=days)
    out: dict[tuple[str, str, int], list[tuple[date, float, float, float]]] = {}
    for station in stations:
        for variable in variables:
            try:
                by_lead = fetch_historical_lead_forecasts(
                    station, leads, start, end, client, variable
                )
                actuals = venue_actuals(station, start, end, client, variable)
            except (httpx.HTTPError, ValueError) as exc:
                print(f"skip {station.icao}/{variable}: {exc}", file=sys.stderr)
                continue
            for lead in leads:
                pairs = [
                    (d, g.mu, g.sigma, actuals[d])
                    for d, g in sorted(by_lead.get(lead, {}).items())
                    if d in actuals
                ]
                if pairs:
                    out[(station.icao, variable, lead)] = pairs
    return out


def save_cache(
    cache_path: Path, data: dict[tuple[str, str, int], list[tuple[date, float, float, float]]]
) -> None:
    serializable = {
        f"{icao}|{variable}|{lead}": [
            [d.isoformat(), mu, sigma, actual] for d, mu, sigma, actual in rows
        ]
        for (icao, variable, lead), rows in data.items()
    }
    cache_path.write_text(json.dumps(serializable))


def load_cache(
    cache_path: Path,
) -> dict[tuple[str, str, int], list[tuple[date, float, float, float]]]:
    raw = json.loads(cache_path.read_text())
    out: dict[tuple[str, str, int], list[tuple[date, float, float, float]]] = {}
    for key, rows in raw.items():
        icao, variable, lead_s = key.split("|")
        out[(icao, variable, int(lead_s))] = [
            (date.fromisoformat(d), mu, sigma, actual) for d, mu, sigma, actual in rows
        ]
    return out


def fetch_or_load_cell_data(
    cache_path: Path = DEFAULT_CACHE_PATH,
    *,
    end: date | None = None,
    days: int = FETCH_DAYS,
    force_refetch: bool = False,
) -> dict[tuple[str, str, int], list[tuple[date, float, float, float]]]:
    """Load the cached archive pairs if present, else fetch and cache them.

    The cache lives outside the repo (default: the OS temp dir) so a rerun is
    reproducible offline without ever risking a commit of fetched data.
    """
    if not force_refetch and cache_path.exists():
        print(f"loading cached archive pairs from {cache_path}", file=sys.stderr)
        return load_cache(cache_path)
    stations = distinct_backfill_stations()
    fetch_end = end or (date.today() - timedelta(days=1))
    client = build_client(60.0)
    try:
        data = fetch_cell_data(stations, VARIABLES, LEADS, fetch_end, days, client)
    finally:
        client.close()
    save_cache(cache_path, data)
    n_pairs = sum(len(v) for v in data.values())
    print(f"cached {n_pairs} pairs across {len(data)} cells to {cache_path}", file=sys.stderr)
    return data


# -----------------------------------------------------------------------------
# Scoring engine: one generic numeric (tw)CRPS integral
# -----------------------------------------------------------------------------


def numeric_crps(
    std_cdf: Callable[[NDArray[np.float64]], NDArray[np.float64]],
    mu: NDArray[np.float64] | float,
    sigma: NDArray[np.float64] | float,
    actual: NDArray[np.float64] | float,
    *,
    weight: Callable[[NDArray[np.float64]], NDArray[np.float64]] | None = None,
    n_grid: int = 20001,
    span: float = 8.0,
) -> NDArray[np.float64]:
    """Vectorized (tw)CRPS via a shared standardized grid (see module docstring).

    `std_cdf` is the *standardized* (loc=0, scale=1) predictive CDF (e.g.
    scipy.stats.norm.cdf, or scipy.stats.t.cdf at some df). `weight`, if given,
    is evaluated on the raw (unstandardized) value line; with weight=None this
    is plain CRPS. Returns one score per (mu, sigma, actual) triple.

    n_grid defaults high (20001, step 0.0008 over span 16) because the
    indicator term has a jump discontinuity at the standardized actual: the
    trapezoid rule's error there is O(step), not O(step^2), so a coarse grid
    (e.g. 1601) is visibly off against the closed-form Gaussian CRPS. Verified
    in tests/test_tail_objective_spike.py.

    `span` is a floor, not a hard cutoff: if some (actual - mu)/sigma falls
    outside +/- span (an overconfident candidate scale, or a genuine outlier),
    the grid widens to cover it (resolution held roughly constant by scaling
    n_grid with the widened span). Without this, an optimizer minimizing this
    objective could shrink sigma toward zero: with a *fixed* span, that pushes
    the standardized actual off the truncated grid entirely, so the integral
    silently drops the true (large) penalty for being that overconfident, and
    the whole thing is multiplied by a near-zero sigma on top -- a numerical
    trap disguised as a global minimum. The true integral (span=infinity) does
    not have this problem; this is what makes the finite grid behave like it.
    """
    mu_arr = np.atleast_1d(np.asarray(mu, dtype=np.float64))
    sigma_arr = np.atleast_1d(np.asarray(sigma, dtype=np.float64))
    actual_arr = np.atleast_1d(np.asarray(actual, dtype=np.float64))
    actual_std = (actual_arr - mu_arr) / sigma_arr  # (N,)
    span_needed = float(np.max(np.abs(actual_std))) + 1.0 if actual_std.size else span
    span_eff = max(span, span_needed)
    n_grid_eff = n_grid if span_eff == span else int(round(n_grid * span_eff / span))
    # Cap both the absolute grid size and the batch x grid matrix size: a large
    # fit batch combined with a wide span (many outliers, or an optimizer probing
    # a tiny sigma before the FIT_MIN_SIGMA floor engages) must not allocate an
    # unbounded (N, G) array. Extreme cases lose resolution rather than memory.
    n_grid_eff = min(
        n_grid_eff, _MAX_GRID, max(1001, _MAX_MATRIX_ELEMENTS // max(len(actual_std), 1))
    )
    u = np.linspace(-span_eff, span_eff, n_grid_eff)
    cdf_u = std_cdf(u)  # (G,)
    indicator = (u[None, :] >= actual_std[:, None]).astype(np.float64)  # (N, G)
    integrand = (cdf_u[None, :] - indicator) ** 2  # (N, G)
    if weight is not None:
        x = mu_arr[:, None] + sigma_arr[:, None] * u[None, :]
        integrand = integrand * weight(x)
    result: NDArray[np.float64] = sigma_arr * np.trapezoid(integrand, u, axis=1)
    return result


def std_cdf_for(df: float | None) -> Callable[[NDArray[np.float64]], NDArray[np.float64]]:
    """Standardized predictive CDF: Gaussian if df is None, else Student-t(df)."""
    if df is None:
        return lambda u: np.asarray(norm.cdf(u), dtype=np.float64)
    return lambda u: np.asarray(t.cdf(u, df), dtype=np.float64)


def tail_weight_from_climatology(
    fit_actuals: list[float], *, multiplier: float
) -> Callable[[NDArray[np.float64]], NDArray[np.float64]]:
    """Two-sided indicator tail weight: `multiplier` beyond the fit window's
    q10/q90 actuals, 1.0 inside. Computed once from the fit-window actuals only
    (no lookahead into the eval window).
    """
    q10, q90 = np.quantile(np.asarray(fit_actuals, dtype=np.float64), [0.10, 0.90])

    def weight(x: NDArray[np.float64]) -> NDArray[np.float64]:
        result: NDArray[np.float64] = np.where((x <= q10) | (x >= q90), multiplier, 1.0)
        return result

    return weight


# -----------------------------------------------------------------------------
# Fitters
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class EmosFit:
    bias: float
    var_a: float
    var_b: float
    df: float | None  # None = Gaussian family; else Student-t degrees of freedom
    n_samples: int


def _arrays(
    pairs: list[FitPair],
) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64]]:
    mu = np.array([p[0] for p in pairs], dtype=np.float64)
    ev = np.array([p[1] for p in pairs], dtype=np.float64)
    actual = np.array([p[2] for p in pairs], dtype=np.float64)
    return mu, ev, actual


def _warm_start(
    mu: NDArray[np.float64], ev: NDArray[np.float64], actual: NDArray[np.float64]
) -> tuple[float, float, float]:
    """Same warm start as calibration.fit_calibration: bias = mean signed error,
    var_b = 1 (unit amplification), var_a = max(0, residual variance - mean(ev)).
    """
    bias0 = float(np.mean(mu - actual))
    residuals0 = actual - (mu - bias0)
    resid_var = float(np.mean(residuals0**2))
    mean_ev = float(np.mean(ev))
    var_b0 = 1.0
    var_a0 = max(0.0, resid_var - var_b0 * mean_ev)
    return bias0, var_a0, var_b0


def _mean_numeric_score(
    std_cdf: Callable[[NDArray[np.float64]], NDArray[np.float64]],
    bias: float,
    var_a: float,
    var_b: float,
    mu: NDArray[np.float64],
    ev: NDArray[np.float64],
    actual: NDArray[np.float64],
    *,
    weight: Callable[[NDArray[np.float64]], NDArray[np.float64]] | None,
) -> float:
    # Floor at FIT_MIN_SIGMA, not an epsilon: numeric_crps's grid is finite, so an
    # optimizer minimizing this objective could otherwise walk sigma toward zero
    # to exploit truncation (see numeric_crps's docstring) rather than genuinely
    # fitting the data. A physical floor forecloses that regardless of grid width.
    sigma = np.sqrt(np.maximum(var_a + var_b * ev, FIT_MIN_SIGMA**2))
    loc = mu - bias
    scores = numeric_crps(std_cdf, loc, sigma, actual, weight=weight)
    return float(np.mean(scores))


def fit_student_t_fixed_df(pairs: list[FitPair], *, df: float) -> EmosFit:
    """Student-t EMOS, df held fixed; (bias, var_a, var_b) fit by minimizing
    mean numeric CRPS.
    """
    mu, ev, actual = _arrays(pairs)
    bias0, var_a0, var_b0 = _warm_start(mu, ev, actual)
    std_cdf = std_cdf_for(df)

    def objective(params: NDArray[np.float64]) -> float:
        b, va, vb = params
        return _mean_numeric_score(std_cdf, b, va, vb, mu, ev, actual, weight=None)

    result = minimize(
        objective,
        x0=np.array([bias0, var_a0, var_b0]),
        method="L-BFGS-B",
        bounds=[(None, None), (0.0, None), (0.0, None)],
    )
    b, va, vb = result.x
    return EmosFit(bias=float(b), var_a=float(va), var_b=float(vb), df=df, n_samples=len(pairs))


def fit_student_t_free_df(pairs: list[FitPair], *, df0: float = 8.0) -> EmosFit:
    """Student-t EMOS with df as a 4th parameter, parametrized as log(df - 2)
    (bounded so df stays in (2, 62]) and fit jointly with (bias, var_a, var_b)
    by minimizing mean numeric CRPS.
    """
    mu, ev, actual = _arrays(pairs)
    bias0, var_a0, var_b0 = _warm_start(mu, ev, actual)
    log_df0 = np.log(df0 - 2.0)

    def objective(params: NDArray[np.float64]) -> float:
        b, va, vb, log_df = params
        df = 2.0 + np.exp(log_df)
        std_cdf = std_cdf_for(float(df))
        return _mean_numeric_score(std_cdf, b, va, vb, mu, ev, actual, weight=None)

    result = minimize(
        objective,
        x0=np.array([bias0, var_a0, var_b0, log_df0]),
        method="L-BFGS-B",
        bounds=[(None, None), (0.0, None), (0.0, None), (np.log(0.5), np.log(60.0))],
    )
    b, va, vb, log_df = result.x
    df_fit = 2.0 + float(np.exp(log_df))
    return EmosFit(bias=float(b), var_a=float(va), var_b=float(vb), df=df_fit, n_samples=len(pairs))


def fit_twcrps_gaussian(pairs: list[FitPair], *, multiplier: float) -> EmosFit:
    """Gaussian EMOS, (bias, var_a, var_b) fit by minimizing mean numeric twCRPS
    with a two-sided tail-indicator weight from the fit window's own q10/q90.
    """
    mu, ev, actual = _arrays(pairs)
    bias0, var_a0, var_b0 = _warm_start(mu, ev, actual)
    weight = tail_weight_from_climatology(actual.tolist(), multiplier=multiplier)
    std_cdf = std_cdf_for(None)

    def objective(params: NDArray[np.float64]) -> float:
        b, va, vb = params
        return _mean_numeric_score(std_cdf, b, va, vb, mu, ev, actual, weight=weight)

    result = minimize(
        objective,
        x0=np.array([bias0, var_a0, var_b0]),
        method="L-BFGS-B",
        bounds=[(None, None), (0.0, None), (0.0, None)],
    )
    b, va, vb = result.x
    return EmosFit(bias=float(b), var_a=float(va), var_b=float(vb), df=None, n_samples=len(pairs))


def apply_emos_regime(
    *,
    bias: float,
    var_a: float,
    var_b: float,
    df: float | None,
    n_samples: int,
    mu: float,
    ensemble_var: float,
    min_sigma: float,
    fallback_df: float | None,
    min_bias_samples: int = MIN_CAL_BIAS_SAMPLES,
    min_samples: int = MIN_CAL_SAMPLES,
) -> tuple[float, float, float | None]:
    """Same three n-gating regimes as calibration.apply_calibration, generalized
    to an optional Student-t family, so every candidate is judged on the same
    population-size floor as the live path.

    - n < min_bias_samples ("uncalibrated"): mu unchanged, sigma widened-raw,
      Gaussian (df=None) -- matches apply_calibration's uncalibrated branch
      exactly regardless of candidate, since there isn't enough data to trust
      even a family choice.
    - min_bias_samples <= n < min_samples ("bias_only"): mu shifted by bias,
      sigma still widened-raw. Family shape falls back to `fallback_df` (a
      fixed hyperparameter, not fit) since var_a/var_b -- and a fitted df --
      would overfit below min_samples.
    - n >= min_samples ("full"): mu - bias, sigma from the fitted var_a/var_b,
      family is the candidate's own df.
    """
    widened = max(sqrt(ensemble_var) * UNCALIBRATED_WIDEN, min_sigma)
    if n_samples < min_bias_samples:
        return mu, widened, None
    if n_samples < min_samples:
        return mu - bias, widened, fallback_df
    scale = max(sqrt(max(var_a + var_b * ensemble_var, 0.0)), min_sigma)
    return mu - bias, scale, df


# -----------------------------------------------------------------------------
# Generic PIT tail ratios (re-implementation of tracking._pit_tail_ratios,
# decoupled from the Gaussian PIT so it also scores the Student-t candidates)
# -----------------------------------------------------------------------------


def pit_tail_ratios(pits: list[float]) -> dict[str, float | int]:
    """Tail-occurrence ratios P(PIT > 1-q)/q and P(PIT < q)/q at q = 0.10, 0.05.

    A ratio of 1.0 means the tail is honest; > 1 means the tail is fatter than
    claimed (busts happen more than the claimed probability implies).
    """
    n = len(pits)

    def ratio(q: float, upper: bool) -> float:
        if upper:
            count = sum(1 for p in pits if p > 1 - q)
        else:
            count = sum(1 for p in pits if p < q)
        return (count / n) / q

    return {
        "n": n,
        "upper_10": ratio(0.10, True),
        "lower_10": ratio(0.10, False),
        "upper_05": ratio(0.05, True),
        "lower_05": ratio(0.05, False),
    }


# -----------------------------------------------------------------------------
# Generic bucket probability (ported continuity correction from
# probability.outcomes.bucket_probability, generalized to any CDF: that
# function is typed for a Gaussian and must not change)
# -----------------------------------------------------------------------------


def bucket_probability_generic(cdf: Callable[[float], float], bucket: Bucket) -> float:
    """P(settled value falls in this bucket), continuity-corrected.

    Copy of outcomes.bucket_probability's geometry (settlement rounds to whole
    degrees F, so "A-B" covers [A-0.5, B+0.5); "X or below" is (-inf, X+0.5];
    "Y or higher" is [Y-0.5, +inf)), generalized to any CDF callable so this
    harness can score Student-t predictives the same way.
    """
    if bucket.kind == "below":
        assert bucket.threshold is not None
        return cdf(bucket.threshold + 0.5)
    if bucket.kind == "above":
        assert bucket.threshold is not None
        return 1.0 - cdf(bucket.threshold - 0.5)
    assert bucket.lo is not None and bucket.hi is not None
    return cdf(bucket.hi + 0.5) - cdf(bucket.lo - 0.5)


def _tail_bin(value: float) -> str:
    """Port of tracking._tail_bin: map a claimed probability to a tail bin."""
    if value < 0.75:
        return "<0.75"
    if value < 0.85:
        return "[0.75,0.85)"
    if value < 0.90:
        return "[0.85,0.90)"
    if value < 0.95:
        return "[0.90,0.95)"
    return "[0.95,1.0]"


# -----------------------------------------------------------------------------
# Per-cell evaluation
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class TopBinStat:
    n: int
    claimed_mean: float
    realized_freq: float


@dataclass(frozen=True)
class CellEval:
    candidate: str
    variable: str
    lead: int
    n: int
    upper_10: float
    lower_10: float
    upper_05: float
    lower_05: float
    brier: float
    mean_crps: float
    body_max_dev: float  # max |predicted_mean - observed_freq| among bins with lo < 0.90
    top_bin_yes: TopBinStat | None
    top_bin_no: TopBinStat | None


PredictFn = Callable[[float, float], tuple[float, float, float | None]]
"""(mu, ensemble_var) -> (loc, scale, df) for one candidate; n-gating is baked
into the closure the caller builds (see run_comparison)."""


def _bind_cdf(
    std_cdf: Callable[[NDArray[np.float64]], NDArray[np.float64]], loc: float, scale: float
) -> Callable[[float], float]:
    """A scalar cdf(x) bound to this (loc, scale). A plain closure defined inside
    a loop would capture loc/scale by late-binding reference (ruff B023); binding
    them as arguments here instead gives each call its own frame.
    """

    def cdf(x: float) -> float:
        return float(std_cdf(np.array([(x - loc) / scale]))[0])

    return cdf


def score_candidate_cell(
    candidate: str,
    variable: str,
    lead: int,
    eval_pairs: list[tuple[date, float, float, float]],
    predict: PredictFn,
) -> CellEval:
    """Score one candidate's fitted predictive over one cell's eval window.

    `predict` maps a raw (mu, ensemble_var) pair to the candidate's (loc,
    scale, df) for that pair.
    """
    pits: list[float] = []
    crps_scores: list[float] = []
    all_pairs: list[tuple[float, bool]] = []  # (bucket prob, won), for Brier + reliability
    top_bin: dict[str, list[tuple[float, bool]]] = {"YES": [], "NO": []}

    for _d, mu, sigma_raw, actual in eval_pairs:
        ensemble_var = sigma_raw**2
        loc, scale, df = predict(mu, ensemble_var)
        std_cdf = std_cdf_for(df)
        z = (actual - loc) / scale
        pits.append(float(std_cdf(np.array([z]))[0]))
        crps_scores.append(float(numeric_crps(std_cdf, loc, scale, actual, weight=None)[0]))

        cdf = _bind_cdf(std_cdf, loc, scale)
        center = round(mu)
        buckets = standard_buckets(float(center))
        for b in buckets:
            p = bucket_probability_generic(cdf, b)
            won = settles(b.kind, b.lo, b.hi, b.threshold, actual)
            all_pairs.append((p, won))
            claim, event_won = (p, won) if p >= 0.5 else (1 - p, not won)
            side = "YES" if p >= 0.5 else "NO"
            if _tail_bin(claim) == "[0.95,1.0]":
                top_bin[side].append((claim, event_won))

    ratios = pit_tail_ratios(pits)
    brier = sum((p - (1.0 if w else 0.0)) ** 2 for p, w in all_pairs) / len(eval_pairs)
    bins = reliability_bins(all_pairs)
    body_bins = [b for b in bins if b.lo < 0.90]
    body_max_dev = max((abs(b.predicted_mean - b.observed_freq) for b in body_bins), default=0.0)

    def top_bin_stat(side: str) -> TopBinStat | None:
        items = top_bin[side]
        if not items:
            return None
        n = len(items)
        return TopBinStat(
            n=n,
            claimed_mean=sum(c for c, _ in items) / n,
            realized_freq=sum(1 for _, w in items if w) / n,
        )

    return CellEval(
        candidate=candidate,
        variable=variable,
        lead=lead,
        n=int(ratios["n"]),
        upper_10=float(ratios["upper_10"]),
        lower_10=float(ratios["lower_10"]),
        upper_05=float(ratios["upper_05"]),
        lower_05=float(ratios["lower_05"]),
        brier=brier,
        mean_crps=sum(crps_scores) / len(crps_scores),
        body_max_dev=body_max_dev,
        top_bin_yes=top_bin_stat("YES"),
        top_bin_no=top_bin_stat("NO"),
    )


# -----------------------------------------------------------------------------
# Walk-forward driver (single chronological split; see module docstring)
# -----------------------------------------------------------------------------

CANDIDATES: tuple[str, ...] = (
    "baseline",
    "t_df5",
    "t_df8",  # sensitivity row
    "t_free_df",
    "twcrps_x5",
    "twcrps_x10",  # sensitivity row
)


def _split(
    rows: list[tuple[date, float, float, float]],
) -> tuple[list[tuple[date, float, float, float]], list[tuple[date, float, float, float]]]:
    split_i = int(len(rows) * FIT_FRACTION)
    return rows[:split_i], rows[split_i:]


def _fit_pairs(rows: list[tuple[date, float, float, float]]) -> list[FitPair]:
    return [(mu, sigma**2, actual) for _d, mu, sigma, actual in rows]


def run_comparison(
    cell_data: dict[tuple[str, str, int], list[tuple[date, float, float, float]]],
) -> dict[tuple[str, str, int], CellEval]:
    """Fit all candidates per (station, variable, lead) cell, pool eval scores
    per (variable, lead) across stations, return one CellEval per
    (candidate, variable, lead).
    """
    fits: dict[tuple[str, str, str, int], EmosFit | Calibration] = {}

    for (icao, variable, lead), rows in cell_data.items():
        if len(rows) < MIN_CELL_PAIRS:
            continue
        fit_rows, eval_rows = _split(rows)
        if not fit_rows or not eval_rows:
            continue
        fit_pairs = _fit_pairs(fit_rows)
        cal_pairs = [
            CalibrationPair(mu=m, sigma=sqrt(e), ensemble_var=e, actual=a) for m, e, a in fit_pairs
        ]

        fits["baseline", icao, variable, lead] = fit_calibration(icao, variable, lead, cal_pairs)
        fits["t_df5", icao, variable, lead] = fit_student_t_fixed_df(fit_pairs, df=5.0)
        fits["t_df8", icao, variable, lead] = fit_student_t_fixed_df(fit_pairs, df=8.0)
        fits["t_free_df", icao, variable, lead] = fit_student_t_free_df(fit_pairs)
        fits["twcrps_x5", icao, variable, lead] = fit_twcrps_gaussian(fit_pairs, multiplier=5.0)
        fits["twcrps_x10", icao, variable, lead] = fit_twcrps_gaussian(fit_pairs, multiplier=10.0)

    results: dict[tuple[str, str, int], CellEval] = {}
    for candidate in CANDIDATES:
        by_vl: dict[tuple[str, int], list[CellEval]] = {}
        for (icao, variable, lead), rows in cell_data.items():
            if len(rows) < MIN_CELL_PAIRS:
                continue
            fit = fits.get((candidate, icao, variable, lead))
            if fit is None:
                continue
            _fit_rows, eval_rows = _split(rows)
            if not eval_rows:
                continue

            def predict(
                mu: float, ensemble_var: float, *, fit: Any = fit
            ) -> tuple[float, float, float | None]:
                if isinstance(fit, Calibration):
                    return apply_emos_regime(
                        bias=fit.bias,
                        var_a=fit.var_a,
                        var_b=fit.var_b,
                        df=None,
                        n_samples=fit.n_samples,
                        mu=mu,
                        ensemble_var=ensemble_var,
                        min_sigma=MIN_SIGMA_F,
                        fallback_df=None,
                    )
                return apply_emos_regime(
                    bias=fit.bias,
                    var_a=fit.var_a,
                    var_b=fit.var_b,
                    df=fit.df,
                    n_samples=fit.n_samples,
                    mu=mu,
                    ensemble_var=ensemble_var,
                    min_sigma=MIN_SIGMA_F,
                    fallback_df=5.0,
                )

            cell_eval = score_candidate_cell(candidate, variable, lead, eval_rows, predict)
            by_vl.setdefault((variable, lead), []).append(cell_eval)

        for (variable, lead), cell_evals in by_vl.items():
            results[(candidate, variable, lead)] = _pool_cell_evals(
                candidate, variable, lead, cell_evals
            )

    return results


def _pool_cell_evals(
    candidate: str, variable: str, lead: int, cell_evals: list[CellEval]
) -> CellEval:
    """n-weighted pool of per-station CellEvals into one (variable, lead) row."""
    n = sum(c.n for c in cell_evals)
    if n == 0:
        raise ValueError("cannot pool zero-sample cell evals")

    def wavg(attr: str) -> float:
        return float(sum(getattr(c, attr) * c.n for c in cell_evals)) / n

    def pool_top_bin(side: str) -> TopBinStat | None:
        stats = [getattr(c, f"top_bin_{side.lower()}") for c in cell_evals]
        stats = [s for s in stats if s is not None]
        if not stats:
            return None
        tn = sum(s.n for s in stats)
        return TopBinStat(
            n=tn,
            claimed_mean=sum(s.claimed_mean * s.n for s in stats) / tn,
            realized_freq=sum(s.realized_freq * s.n for s in stats) / tn,
        )

    return CellEval(
        candidate=candidate,
        variable=variable,
        lead=lead,
        n=n,
        upper_10=wavg("upper_10"),
        lower_10=wavg("lower_10"),
        upper_05=wavg("upper_05"),
        lower_05=wavg("lower_05"),
        brier=wavg("brier"),
        mean_crps=wavg("mean_crps"),
        body_max_dev=max(c.body_max_dev for c in cell_evals),
        top_bin_yes=pool_top_bin("YES"),
        top_bin_no=pool_top_bin("NO"),
    )


# -----------------------------------------------------------------------------
# Rendering
# -----------------------------------------------------------------------------


def render_table(results: dict[tuple[str, str, int], CellEval]) -> str:
    header = (
        "| Candidate | Var | Lead | n | Up.10 | Lo.10 | Up.05 | Lo.05 | Brier | "
        "CRPS | BodyMaxDev | NO[.95,1] claim/real | YES[.95,1] claim/real |"
    )
    rule = "| --- " * 13 + "|"
    lines = [header, rule]

    def fmt_top(stat: TopBinStat | None) -> str:
        return f"{stat.claimed_mean:.3f}/{stat.realized_freq:.3f} (n={stat.n})" if stat else "-"

    for key in sorted(
        results, key=lambda k: (k[1], k[2], CANDIDATES.index(k[0]) if k[0] in CANDIDATES else 99)
    ):
        c = results[key]
        row = (
            f"| {c.candidate} | {c.variable} | {c.lead} | {c.n} | {c.upper_10:.2f} | "
            f"{c.lower_10:.2f} | {c.upper_05:.2f} | {c.lower_05:.2f} | {c.brier:.3f} | "
            f"{c.mean_crps:.3f} | {c.body_max_dev:.3f} | "
        )
        row += f"{fmt_top(c.top_bin_no)} | {fmt_top(c.top_bin_yes)} |"
        lines.append(row)
    return "\n".join(lines) + "\n"


def main() -> None:
    cell_data = fetch_or_load_cell_data()
    results = run_comparison(cell_data)
    print(render_table(results))


if __name__ == "__main__":
    main()
