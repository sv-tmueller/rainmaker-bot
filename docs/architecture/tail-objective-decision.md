# Tail-objective comparison: Student-t vs twCRPS (spike #284)

Decision doc for #284, part of the #280/#283 umbrella. This does not change the
live path: `distribution.py`, `calibration.py`, `outcomes.py` are untouched.
The comparison code lives in `src/rainmaker/spikes/tail_objective.py` (dead to
the live path) with sanity tests in `tests/test_tail_objective_spike.py`.

## Evidence baseline: latest tail-check readout

Latest `daily-diagnostics` run as of this write-up: [29540071375](https://github.com/sv-tmueller/rainmaker-bot/actions/runs/29540071375),
2026-07-16 22:37 UTC (the next scheduled run is 2026-07-17 21:45 UTC, after
this doc). The workflow does not yet pass `tail-check --since` (#282 added the
flag; nothing wires it into the cron), so this is the same full-history,
regime-mixed population #244 diffed against a since cut. No newer run exists
to cite.

PIT tail-occurrence ratios (P(PIT in tail)/q; 1.0 is honest):

| Variable | Lead | n | Up.10 | Lo.10 | Up.05 | Lo.05 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| TMAX | 0 | 668 | 1.77 | 0.82 | 2.16 | 0.75 |
| TMAX | 1 | 675 | 0.98 | 1.07 | 1.16 | 1.30 |
| TMAX | 2 | 95 | 1.89 | 0.63 | 1.47 | 0.63 |
| TMIN | 0 | 214 | 0.79 | 0.98 | 0.93 | 1.31 |
| TMIN | 1 | 282 | 0.99 | 1.81 | 0.85 | 2.27 |
| TMIN | 2 | 56 | 0.54 | 0.71 | 0.36 | 0.71 |

This has not materially moved since the 2026-07-16 readout in #244 (it is
close to the same run): TMAX lead 1's lower tail (1.30) and TMIN lead 1's
lower tail (2.27) are still the clearest misses; TMIN lead 0's lower tail
(1.31) is a smaller but consistent miss. TMAX lead 0's upper tail (2.16) looks
bad in this unfiltered population but #244 showed that number is dominated by
pre-2026-07-06 (pre-backfill-fix) history; the post-fix window reads ~1.2x.
That fix is not re-litigated here; this spike is scoped to the lower tail per
#280.

## Comparison design

**Data.** Backfill archive pairs, not the prod DB: `fetch_historical_lead_forecasts`
+ `venue_actuals` + `build_pairs`-equivalent join (imported/mirrored from
`backfill.py`, not edited), one Previous Runs request per (station, variable),
leads 0-2, all 13 backfill stations (`STATIONS` union `KALSHI_STATIONS`,
deduped by ICAO), 120-day window ending 2026-07-17. 78 (station, variable,
lead) cells, 9,414 forecast/actual pairs total. Two caveats carried into every
number below:

- Sigma here is multi-model disagreement (Open-Meteo's per-model spread), the
  same proxy the live calibration is fitted on, so it is the right population
  for comparing fit objectives, but it is not the live pooled-source spread.
- The lead-0 archive is slightly fresher than what the live morning run sees
  (see `backfill.py`'s lead-0 docstring), so lead-0 numbers here are mildly
  optimistic versions of the live lead-0 numbers.

**Evaluation split.** Single chronological split per cell: fit on the oldest
60% (~72 days), evaluate on the newest 40% (~48 days), pooled per (variable,
lead) across the 13 stations. This is the sub-plan's explicit fallback from a
weekly-anchor walk-forward: the scoring engine, four fitters, and their
sanity tests were the session's first priority per the #283 size trip-wire,
and that budget was spent getting the engine correct (see the numeric-CRPS
truncation bug fixed during TDD, below) rather than on walk-forward indexing.
A single out-of-sample split still answers the question that matters
(in-sample would automatically favor the more flexible t-with-fitted-df), just
with one eval window per cell instead of several weekly ones, so it is more
exposed to the "one correlated week" caveat #244 already flagged.

**Scoring engine.** One generic numeric integral serves as both the CRPS and
twCRPS objective (Allen et al., arXiv:2407.03167):
`integral w(z) * (F(z) - 1{y<=z})^2 dz`, with w=1 for CRPS. Implemented via a
standardized-grid substitution (`numeric_crps` in the harness), validated
against the closed-form Gaussian CRPS already in `backtest.py` to within
3e-3 (grid resolution 20,001 points; the trapezoid rule's error at the
indicator's jump discontinuity is O(step), which is why the grid is that
fine). During TDD, an initial version of this engine had a real bug worth
recording: with a *fixed* integration span, an optimizer minimizing it could
push the fitted sigma toward zero, which pushed the standardized actual
outside the truncated grid and made the (wrong) integral report a near-zero
score for an absurdly overconfident fit -- a numerical trap disguised as a
global optimum. Fixed by widening the grid to always cover the actual value
and flooring the fitted sigma at a physical minimum (1.0 degrees F) during
fitting. Caught by `tests/test_tail_objective_spike.py`'s synthetic-recovery
tests before any live-data numbers were trusted.

**Candidates**, all sharing the same (bias, var_a, var_b) EMOS parametrization
as the live path and the same three-regime n-gating as `apply_calibration`
(`apply_emos_regime` in the harness):

1. `baseline`: the live path's own `fit_calibration` + `apply_calibration`
   (Gaussian, closed-form-CRPS fit). Imported, not reimplemented.
2. `t_df5` / `t_df8`: Student-t, df held fixed (5 primary, 8 sensitivity),
   (bias, var_a, var_b) fit by minimizing mean numeric CRPS.
3. `t_free_df`: Student-t with df as a 4th fit parameter, parametrized as
   log(df - 2) (bounded so df stays in (2, 62]).
4. `twcrps_x5` / `twcrps_x10`: stays Gaussian, (bias, var_a, var_b) fit by
   minimizing mean numeric twCRPS with a two-sided indicator tail weight
   (multiplier 5 primary, 10 sensitivity) beyond the fit window's own
   climatological q10/q90 actuals, 1.0 inside.

**Metrics per (variable, lead) cell**, pooled across stations' eval windows:
PIT tail ratios at q=0.05/0.10, upper and lower separately (generalized
re-implementation of `tracking._pit_tail_ratios`, decoupled from the Gaussian
CDF so it also scores the Student-t candidates); Brier over a `standard_buckets`
ladder centered on the raw (uncalibrated) forecast mean, using a
continuity-corrected bucket-probability function ported from
`outcomes.bucket_probability` and generalized to any CDF (`outcomes.py`
itself is untouched); mean unweighted numeric CRPS as a sharpness guard;
`BodyMaxDev`, the largest |predicted - realized| gap among reliability bins
below 0.90 claimed probability (`backtest.reliability_bins`, imported), as the
"did the body degrade" guard; and claimed-vs-realized for the extreme
[0.95, 1.0] NO/YES bin (mirroring `tracking._tail_bin`), since that is the
bin the live tail-check flags as where the money leaks.

## Results

78 cells x 6 rows; the full table is reproducible offline from the cached
archive pairs (`python -m rainmaker.spikes.tail_objective`, cache at
`$TMPDIR/rainmaker_tail_objective_cache.json`, not committed). All temperature
cells:

| Candidate | Var | Lead | n | Up.10 | Lo.10 | Up.05 | Lo.05 | Brier | CRPS | BodyMaxDev |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| baseline | TMAX | 0 | 635 | 0.94 | 1.34 | 1.13 | 1.67 | 0.712 | 1.205 | 0.298 |
| t_df5 | TMAX | 0 | 635 | 0.63 | 1.37 | 0.72 | 1.39 | 0.727 | 1.237 | 0.327 |
| t_df8 | TMAX | 0 | 635 | 0.68 | 1.53 | 0.91 | 1.57 | 0.728 | 1.237 | 0.336 |
| t_free_df | TMAX | 0 | 635 | 0.66 | 1.35 | 0.82 | 1.29 | 0.727 | 1.237 | 0.337 |
| twcrps_x5 | TMAX | 0 | 635 | 0.82 | 1.73 | 1.13 | 2.14 | 0.729 | 1.242 | 0.367 |
| twcrps_x10 | TMAX | 0 | 635 | 0.83 | 1.87 | 1.17 | 2.39 | 0.731 | 1.245 | 0.367 |
| **baseline** | **TMAX** | **1** | 635 | 0.66 | 2.03 | 0.88 | **2.71** | 0.796 | 1.585 | 0.416 |
| t_df5 | TMAX | 1 | 635 | 0.68 | 2.50 | 0.54 | 2.87 | 0.804 | 1.599 | 0.382 |
| t_df8 | TMAX | 1 | 635 | 0.71 | 2.54 | 0.66 | 3.15 | 0.805 | 1.603 | 0.391 |
| t_free_df | TMAX | 1 | 635 | 0.71 | 2.54 | 0.66 | 3.21 | 0.805 | 1.603 | 0.391 |
| twcrps_x5 | TMAX | 1 | 635 | 0.72 | 2.96 | 0.98 | 4.60 | 0.823 | 1.647 | 0.403 |
| twcrps_x10 | TMAX | 1 | 635 | 0.83 | 3.06 | 1.20 | 4.63 | 0.818 | 1.627 | 0.403 |
| baseline | TMAX | 2 | 635 | 0.55 | 2.03 | 0.54 | 2.17 | 0.808 | 1.729 | 0.438 |
| t_df5 | TMAX | 2 | 635 | 0.50 | 2.57 | 0.50 | 3.15 | 0.824 | 1.759 | 0.348 |
| t_df8 | TMAX | 2 | 635 | 0.50 | 2.49 | 0.50 | 2.87 | 0.817 | 1.740 | 0.330 |
| t_free_df | TMAX | 2 | 635 | 0.52 | 2.49 | 0.50 | 2.87 | 0.817 | 1.740 | 0.330 |
| twcrps_x5 | TMAX | 2 | 635 | 0.65 | 2.91 | 0.69 | 4.00 | 0.823 | 1.750 | 0.325 |
| twcrps_x10 | TMAX | 2 | 635 | 0.72 | 2.99 | 0.76 | 4.22 | 0.824 | 1.754 | 0.326 |
| **baseline** | **TMIN** | **0** | 635 | 0.52 | 1.01 | 0.31 | **1.17** | 0.632 | 0.881 | 0.402 |
| t_df5 | TMIN | 0 | 635 | 0.31 | 0.83 | 0.22 | 0.76 | 0.656 | 0.931 | 0.328 |
| t_df8 | TMIN | 0 | 635 | 0.38 | 0.93 | 0.28 | **0.98** | 0.653 | 0.924 | 0.286 |
| t_free_df | TMIN | 0 | 635 | 0.38 | 0.88 | 0.28 | 0.88 | 0.654 | 0.925 | 0.341 |
| twcrps_x5 | TMIN | 0 | 635 | 0.46 | 1.02 | 0.28 | 1.32 | 0.654 | 0.922 | 0.455 |
| twcrps_x10 | TMIN | 0 | 635 | 0.46 | 1.02 | 0.31 | 1.26 | 0.655 | 0.923 | 0.455 |
| **baseline** | **TMIN** | **1** | 635 | 0.38 | 1.17 | 0.35 | **1.35** | 0.698 | 1.120 | 0.304 |
| t_df5 | TMIN | 1 | 635 | 0.43 | 1.09 | 0.25 | **1.04** | 0.708 | 1.153 | 0.303 |
| t_df8 | TMIN | 1 | 635 | 0.44 | 1.10 | 0.35 | 1.17 | 0.709 | 1.151 | 0.351 |
| t_free_df | TMIN | 1 | 635 | 0.41 | 1.04 | 0.35 | **1.04** | 0.709 | 1.150 | 0.341 |
| twcrps_x5 | TMIN | 1 | 635 | 0.49 | 1.21 | 0.57 | 1.45 | 0.708 | 1.143 | 0.294 |
| twcrps_x10 | TMIN | 1 | 635 | 0.47 | 1.28 | 0.57 | 1.57 | 0.699 | 1.122 | 0.204 |
| baseline | TMIN | 2 | 635 | 0.44 | 1.07 | 0.41 | 1.26 | 0.729 | 1.226 | 0.136 |
| t_df5 | TMIN | 2 | 635 | 0.44 | 1.07 | 0.28 | 1.26 | 0.734 | 1.251 | 0.136 |
| t_df8 | TMIN | 2 | 635 | 0.47 | 1.10 | 0.47 | 1.35 | 0.735 | 1.250 | 0.299 |
| t_free_df | TMIN | 2 | 635 | 0.46 | 1.10 | 0.35 | 1.35 | 0.735 | 1.250 | 0.239 |
| twcrps_x5 | TMIN | 2 | 635 | 0.65 | 1.37 | 0.76 | 1.86 | 0.737 | 1.252 | 0.591 |
| twcrps_x10 | TMIN | 2 | 635 | 0.66 | 1.43 | 0.76 | 1.95 | 0.736 | 1.251 | 0.257 |

(Bold rows: baseline and the closest-to-1.0 candidate for each of the three
flagged broken cells: TMAX lead 1, TMIN lead 0, TMIN lead 1.) The
[0.95, 1.0] claim/realized bin (all candidates, all cells) is omitted from
the table above for width; it moves by 0.2-0.8pp between candidates in every
cell, which is small next to the PIT-ratio swings below and does not change
the reading.

## Reading

**twCRPS is dominated; rule it out.** Across all 12 (variable, lead) cells,
neither `twcrps_x5` nor `twcrps_x10` improves the lower-tail ratio versus the
Gaussian baseline even once. In every cell the twCRPS lower ratio is farther
from 1.0 than baseline, often by a lot (TMAX lead 1: baseline 2.71 -> 4.60/4.63;
TMIN lead 2: baseline 1.26 -> 1.86/1.95). Brier is flat-to-worse in every cell
too. The tail-indicator weight, multiplying already-large squared errors near
the fit window's q10/q90 by 5x or 10x, appears to pull the EMOS fit toward a
*wider* body rather than a genuinely heavier tail (Gaussian shape has no other
way to move mass outward), which is exactly the collateral-damage failure
mode this comparison was built to catch. Not pursued further; per #283 a null
result is a finding, not license to invent a third variant.

**Student-t fixes the TMIN lower tail, not the TMAX one.** At the two flagged
TMIN cells, a Student-t fit clearly improves the lower-tail ratio:

- TMIN lead 0: baseline 1.17 (0.17 off nominal) -> best candidate (`t_df8`)
  0.98 (0.02 off). `t_free_df` (0.88, 0.12 off) is second-best; `t_df5`
  (0.76, 0.24 off) is worse than baseline here.
- TMIN lead 1: baseline 1.35 (0.35 off) -> `t_df5` and `t_free_df` tie at
  1.04 (0.04 off), a large improvement; `t_df8` (1.17, 0.17 off) also
  improves but less.

At the flagged TMAX cell it goes the other way:

- TMAX lead 1: baseline 2.71 (already the worst cell in the table) -> every
  Student-t variant is *worse* (2.87-3.21), and twCRPS is worse still
  (4.60-4.63). Same story at TMAX lead 2 (baseline 2.17, all t variants
  2.87-3.15).

So the #280 framing ("the fix is variable-agnostic, not TMAX-only") is not
borne out by this data: whatever is making TMAX's lower tail thin is not the
same shape problem a heavier-tailed family fixes, or a single per-lead
Student-t fit is fighting some other TMAX-specific effect (season mixing
across the 120-day window is one candidate explanation; see caveats). TMIN's
lower-tail problem, by contrast, looks like a genuine tail-shape issue that
Student-t addresses directly.

**No single fixed df wins everywhere.** `t_df5` is best at TMIN lead 1 but
one of the worst options at TMIN lead 0; `t_df8` is the reverse. `t_free_df`
(the data-driven fit) is tied-for-best or second-best at both TMIN cells and
never the worst option anywhere in the table, which is the strongest argument
for preferring the fitted-df parametrization over guessing a fixed df, if
Student-t is pursued at all.

**Collateral damage is real but bounded.** Where Student-t helps (TMIN lead
0-1), Brier rises by 0.010-0.024 (about 2-4% relative) and `BodyMaxDev`
mostly *improves* (TMIN lead 0: 0.402 -> 0.286-0.341) with one exception
(TMIN lead 1 `t_df8`/`t_free_df`: 0.304 -> 0.341-0.351, a small worsening).
The upper tail (Up.05) is a more consistent casualty: it degrades under every
Student-t variant at TMIN lead 0 (baseline 0.31, already under-populated,
-> 0.22-0.28, further under) and is flat-to-worse at TMIN lead 1. Since these
upper-tail ratios were already well below 1.0 (claims already conservative
relative to what happens), a wider predictive distribution pushes them
further from nominal in the same direction, rather than flipping the sign of
the miscalibration. That is a real, quantified cost, not a null one.

## Verdict

Neither candidate is a clean, variable-agnostic fix. twCRPS-weighted Gaussian
is ruled out outright: it never improves the lower tail and is flat-to-worse
on Brier everywhere tested. Student-t with a fitted df (`t_free_df`) fixes the
lower tail specifically at TMIN lead 0-1, the two TMIN cells #244 flagged,
moving both ratios to within 0.04-0.12 of nominal from 0.17-0.35 off, at a
real but modest Brier cost (2-4% relative) and a consistent (if largely
already-present) softening of the upper tail. It does not fix, and modestly
worsens, the TMAX lead-1 and lead-2 lower tail, the other cell #244 flagged as
broken.

This is a partial-fix outcome, not the null result #283 flagged as acceptable,
but also not the clean win #280 hoped for. The next batch (implementation) is
not a drop-in "switch to Student-t" per #280's original framing; it needs to
either scope Student-t to TMIN only (leaving TMAX on the Gaussian baseline,
which is still the best of the four options there) or investigate why TMAX
lead 1-2's lower tail resists a heavier-tailed family before choosing a
family change for it. That scoping decision, and any refit, is out of this
spike's non-goals (no live-path changes, no refit) and belongs to the next
#280 batch.

## Caveats

- **Single split, not walk-forward.** One ~48-day eval window per cell, not
  several weekly ones; the "one correlated week" noise #244 already flagged
  for the live readout applies here too, just to a somewhat larger window.
  Cold or warm spells that hit many cities in the same eval window would move
  every station's cell together, so the effective independent sample is
  smaller than the pooled n (~635 per variable/lead) suggests.
- **Season mixing.** The 120-day fetch window spans meteorological seasons for
  some stations; the live path's own calibration window (`BACKFILL_DAYS=45`,
  season-clamped in `backfill.season_window`) is deliberately shorter and
  season-aware for exactly this reason. This spike's 120-day window trades
  that discipline for more history to split and evaluate; a production refit
  would not use a 120-day, season-mixed window as-is.
- **Archive-proxy sigma.** Per `backfill.py`'s own docstring, the archive's
  predictive spread is multi-model disagreement, not the live pooled-source
  spread (NWS + Open-Meteo + ensemble). It is the same proxy the live
  calibration is fitted on, so the comparison is apples-to-apples between
  candidates, but the absolute PIT ratios here should not be read as
  predictions of the live numbers.
- **Lead-0 freshness.** The lead-0 archive is mildly optimistic versus what
  the live morning run sees (`fetch_historical_lead_forecasts` docstring), so
  lead-0 numbers in this table are a mild best case.
- **Synthetic bucket ladder.** Brier and BodyMaxDev are computed over
  `standard_buckets` (2-degree ranges, +/-10 degrees), not real market
  buckets, mirroring `backtest.py`'s existing synthetic-ladder methodology,
  not real Polymarket/Kalshi ladders.
