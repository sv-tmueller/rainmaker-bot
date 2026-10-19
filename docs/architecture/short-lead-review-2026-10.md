# Fresh-eyes short-lead accuracy review (2026-10)

Second-opinion pass over the 0-3 day lead path, per #425 (batch #424).
The repo was last reviewed wholesale on 2026-10-07 (issues #396-#407, all
merged); those findings are treated as adjudicated and are cited as closed,
not re-reported.

Reviewed in the order a datum flows: source fetch/stamping ->
freshness gating -> pooling/sigma fit -> calibration apply -> bucket
integration -> edge gate. Each module gets an explicit verdict. Everything
below was read against the current tree (main at 9209bec); behavioral claims
were verified by simulation against the real parsers, one live api.weather.gov
probe, and the saved fixtures.

| # | Module | Verdict |
|---|--------|---------|
| 1 | forecasts/nws.py | FINDING (1 investigation, medium) -> #434 |
| 2 | forecasts/openmeteo.py | CLEAN |
| 3 | forecasts/aggregate.py | FINDING (1 improvement, low) -> #437 |
| 4 | probability/distribution.py | FINDING (1 suspicion, medium) -> #435 |
| 5 | probability/calibration.py | CLEAN |
| 6 | probability/outcomes.py | CLEAN |
| 7 | ranking/edge.py | FINDING (1 improvement, low) -> #436 |

Cross-cutting (modules 1-2): one low improvement on lead-tag semantics -> #438.

## Module 1: forecasts/nws.py - FINDING

Checked: TMIN night-period selection (start date = target - 1,
isDaytime=false) and the #404 caveat comment; lead_time_days from the
issued-local date; period-match returning the first hit; the F-unit
assertion.

Clean parts:

- The #404 caveat comment (nws.py:20-31) accurately carries the measured
  wedge (mean 1.0-1.7F, peaks 7F, night-period lows systematically lower)
  and correctly frames a window change as a calibration-regime decision.
- The F-unit assertion (nws.py:37-38) fails loud. Good.
- lead_time_days from the issued-local date is internally coherent (see the
  cross-cutting note below for the cross-source inconsistency).

Finding (medium, filed as investigate): the period matcher (nws.py:34-36)
selects the FIRST period with isDaytime == want_daytime and
start_local.date() == match_date. For TMIN this predicate does not identify
a unique period: ANY night period that BEGINS on the match date qualifies,
including a post-midnight fragment ("Overnight", "Rest of Tonight") that
covers the END of the previous night rather than the target night. Two
corruption modes:

1. Shadowing: a night fragment starting at 00:00-06:00 on date T-1 plus the
   wanted evening period starting 18:00 on T-1 both match a TMIN target T;
   first-hit-wins returns the fragment's residual low of the ENDING night as
   the forecast for the NEXT night. Verified by simulation against the real
   `parse()` (fixture-format payload with a leading "Overnight" fragment:
   TMIN target T returned the fragment's 48F instead of "Tonight"'s 62F).
2. Silent loss: if the WFO replaces the ongoing night period with a
   post-midnight fragment, lead-0 TMIN (match date T-1) finds no qualifying
   period at all and the source contributes zero samples with ok=True -
   the failure is only visible as a smaller n_samples.

Frequency is unverified: a live api.weather.gov probe for KLGA at 01:18 EDT
(2026-10-19 05:18 UTC) showed the baseline generator RETAINING the full
"Tonight" period (started 19:00 the previous evening) as period 1, i.e.
the safe shape. Whether any of the 11 stations' WFOs emit post-midnight
night fragments in the 00:00-06:00 UTC cron window is exactly what the
filed issue measures (a scan of the forecasts table: NWS TMIN values vs the
Open-Meteo consensus for the same target, flagging disagreements larger
than the known night-period wedge). Suggested fix regardless: match on the
period that COVERS the wanted window (for TMIN, prefer the qualifying night
period whose start hour is in the afternoon/evening, or equivalently whose
end date is the target date), keeping first-hit semantics only as a
fallback.

## Module 2: forecasts/openmeteo.py - CLEAN

Checked: fetch-wallclock issued_at stamping (#401); lead_time_days=idx;
the ensemble member regex; per-model skip on None values.

- The #401 stamping (openmeteo.py:143-149) is correct: parsing stays pure
  (issued_at=None), only the source stamps with one `datetime.now(UTC)`
  applied to every sample via model_copy, so the freshness gate can witness
  staleness. Aggregate's stale-drop then works (test at
  tests/test_openmeteo.py:171 pins it).
- lead_time_days=idx: `_common_params` sends timezone=station.timezone and
  no past_days, so daily["time"][0] is the station-local today and idx is
  days-from-local-today. Sound (see the cross-cutting note for the
  UTC-vs-local edge).
- The member regex `{field}_member(\d+)` with fullmatch excludes the bare
  control run, intentionally and commented (openmeteo.py:76-78); the
  fixture confirms the control key coexists with memberNN keys and is
  correctly ignored.
- Per-model skip on None values (multimodel and ensemble) silently shrinks
  the sample set, but n_samples reflects it in coverage, mu reweights per
  remaining group, and the min-sources gate sees the honest count. This is
  the intended graceful degradation, not a swallow.

## Module 3: forecasts/aggregate.py - FINDING (low)

Checked: the blanket per-source except; the issued_at=None freshness
bypass; the flat 24h freshness limit.

- The blanket `except Exception` (aggregate.py:27) is intended resilience
  (one source down must not abort the run), and the error string is not
  swallowed silently - it rides into SourceCoverage.error and is rendered
  in both report forms (render.py:27). But it also catches programming
  errors (TypeError, NameError, AttributeError) inside a parser, which
  then present as "source down" indefinitely with no traceback anywhere.
  Filed as a small improvement: keep the resilience, but write the
  traceback to stderr (the run is unattended; stderr goes to the Actions
  log) or narrow the catch to the transport/parse error types.
- The issued_at=None freshness bypass (aggregate.py:8-9) is moot for both
  current sources post-#401, but remains a silent revert-to-fresh-forever
  for any future source that forgets to stamp. Nit-level (see nits); the
  comment already documents the stance.
- FRESHNESS_LIMIT_HOURS = 24 flat across leads is #427's subject (sample
  recency for short leads, already filed). Noted, not re-reported.

## Module 4: probability/distribution.py - FINDING (medium suspicion)

Checked: the ensemble-arm sigma (between-group mixture only), mu's
equal-weight per (source, model) group, the MIN_SIGMA_F floor interaction,
inter_family_gap as the P2 counterfactual.

Clean parts:

- mu equal-weight per group (#239) is implemented as documented: a 30-strong
  ensemble and a single NWS run each contribute one group mean.
- The K>1 mixture variance, the K=1 reduction, and the no-ensemble fallback
  all match the docstring and the tests.
- inter_family_gap's extended_sigma IS the exact counterfactual of the
  natural #400 extension: every group (deterministic singletons included,
  v_k = within-group sample variance, 0 for singletons) joins one
  equal-weight mixture, sigma^2 = mean(av + (am - am.mean())^2). If #426's
  fit extension implements that mixture, the diagnostic already computes
  precisely what the fit will produce. Details in the deferred-threads
  section, including one scope gap found there.

Finding (medium suspicion, filed as investigate): the sigma the calibration
CELLS are fitted against is a different statistic from the sigma the live
path feeds to apply_calibration. The live raw sigma is fit_gaussian's
ensemble-mixture sigma (within+between ensemble-group dispersion); the
cells are fitted by backfill from `fetch_historical_lead_forecasts`
(backfill.py:244-250), whose historical sigma is the plain stdev across the
five deterministic OPENMETEO_MODELS (Previous Runs API, no ensemble members
exist in that archive). The same mismatch applies to mu: fit-time mu is the
mean of the five OM deterministic models; live mu averages nine groups
(NWS + 5 OM deterministic + 3 ensemble groups). EMOS's var_b *
ensemble_var term is only meaningful if the apply-time ensemble_var is drawn
from the same distribution the fit saw; a systematic scale difference
between deterministic-inter-model spread and live ensemble-mixture spread
biases every full-regime predictive sigma in one direction. The recorded
docs acknowledge this exact divergence for the SPIKE and the P/L backtest
archive ("the predictive spread is multi-model disagreement, not the live
pooled spread", tail-objective-decision.md), but the production cells loaded
by the live run are fitted on the same archive statistic, so the mismatch
is load-bearing in the live path, not just in offline replays. Whether it
is materially harmful is measurable: reconstruct live raw sigmas from the
forecasts table per (station, variable, lead) the way #417's measurement
did, and compare against the fit-time sigma distribution for the same
cells. Filed as investigate with that measurement as the first step.

## Module 5: probability/calibration.py - CLEAN

Checked: apply_calibration's three regimes and UNCALIBRATED_WIDEN on
uncalibrated/bias_only; the full-regime sigma sqrt(var_a + var_b * sigma^2);
df passthrough; fit_calibration vs fit_student_t_free_df warm starts.

- The three regimes gate exactly on n_samples against MIN_CAL_BIAS_SAMPLES
  (10) and MIN_CAL_SAMPLES (30); uncalibrated and bias_only both return the
  widened raw sigma (max(g.sigma * 1.25, min_sigma)), full returns
  max(sqrt(var_a + var_b * g.sigma^2), min_sigma). Matches the module
  docstring and config.
- df passes through only in the full regime (calibration.py:377);
  uncalibrated/bias_only stay Gaussian regardless of cal.df, as documented.
- Both fitters use the IDENTICAL warm start (bias0 = mean signed error,
  var_b0 = 1, var_a0 = max(0, resid_var - mean_ev)); the only difference
  is the predictive family and the fourth log(df-2) parameter with its
  bounds. No drift between them.
- The numeric CRPS engine is the ported spike code with the truncation-trap
  fix and the #407 grid-shrink warning; per the #291 record it is not
  re-derived here, and its tests pin the traps.
- The MIN_SIGMA_F floor saturating on low-spread pools (multiple days at
  exactly 1.5F) was flagged and recorded by the #373 audit; not re-reported
  (nit).

One behavioral consequence of the regime design is NOT a calibration.py
defect but shows up in edge.py; see module 7.

## Module 6: probability/outcomes.py - CLEAN

Checked: the continuity correction against bucket edges; settles()'s
banker's rounding (#399/#416, adjudicated); the Student-t cdf branch.

- The +-0.5 continuity correction is consistent with settles()'s
  whole-degree rounding on every bucket kind: below = cdf(X+0.5) (round(v)
  <= X iff v < X+0.5 up to the measure-zero half atom), above =
  1 - cdf(Y-0.5), range = cdf(hi+0.5) - cdf(lo-0.5). The exhaustive-partition
  test (test_outcomes.py:107) pins the no-gap/no-overlap property.
- The Student-t branch (t.cdf((x - mu) / sigma, df)) is the correct
  standardized form at the same (mu, sigma); the df=None branch is the
  byte-identical Gaussian path.
- Rounding at halves (#399/#416): CONFIRMED only, per the sub-plan.
  settles() still grades with Python round() (half-to-even);
  tests/test_outcomes.py:127 (`test_exact_half_cases_in_prod_history_are_
  rule_compatible`) still pins the three observed prod cases (81.5 -> 82
  matching the Gamma resolution, 72.5/54.5 inside 2-degree buckets) and the
  never-observed discriminator. No code drift since the adjudication: the
  only commit touching outcomes.py afterward is the Student-t
  generalization (302351a, 2026-07-17), which preserved round() and the
  settles() body. No new work needed.

## Module 7: ranking/edge.py - FINDING (low)

Checked: the gate-stack assembly; n_sources from coverage; no_ask
derivation per venue (#402, adjudicated); the C-market F/C guard.

Clean parts:

- The gate stack matches config and the decision record exactly: floor
  0.80 (YES) / floor_no 0.75 (NO, falling back to floor when None),
  min_edge 0.07 + STATION_EDGE_DELTA on both sides, max_edge 0.25 inclusive
  on the RAW edge, require_calibration -> calibrated == "full",
  uncalibratable (ghcnd_id None), and the STATION_POLICIES exclusions
  (KNYC, KHOU). All present at edge.py:190-197 / 216-223.
- n_sources counts coverage entries with ok and n_samples > 0, so a source
  that fetched but yielded nothing fresh honestly counts as absent against
  MIN_SOURCES = 2.
- The #402 no_ask asymmetry is adjudicated and documented
  (recommendation-gate.md, 2026-10-07 update); the C-market guard
  (edge.py:169-174) fails loud if a Fahrenheit calibration cell ever meets
  a Celsius market. Both stand.

Finding (low, filed as improve): the no-cell path bypasses
UNCALIBRATED_WIDEN. When `calibration is None` (no cell on file for the
(station, variable, lead)), edge.py:178 builds the predictive straight from
the raw Gaussian: `Predictive(mu=gaussian.mu, sigma=gaussian.sigma)`. The
equivalent ignorance case one notch over - a cell that exists but has
n_samples < 10 - goes through apply_calibration and gets sigma widened by
1.25x. Both paths are labeled calibrated="uncalibrated" and both suppress
recommendations (require_calibration), so no BET is affected; but the
DISPLAYED and RECORDED p_win/edge for cell-less markets are computed from
the narrower sigma, and those recorded dist_params feed
compute_live_accuracy / tail-check analytics, where rows from the two paths
carry the same "uncalibrated" label with different sigma semantics. A
one-line unification (route the None case through the same widening, or
document why not) closes it; note the golden e2e may pin current numbers,
so the change must update fixtures deliberately.

## Cross-cutting: lead-time tagging (modules 1-2, low, filed as improve)

Three different rulers coexist for "lead":

- The gate and calibration LOAD use cli.py's `(target.local_date -
  datetime.now(UTC).date()).days` (cli.py:162,197).
- NWS samples are tagged `(target.local_date - issued_local).days`
  (nws.py:47).
- Open-Meteo samples are tagged the array index from station-local today
  (openmeteo.py:65,99).

They agree on morning runs but diverge in the 00:00-06:00 UTC cron window
(two of the eight daily slots): e.g. a 00:00 UTC run (20:00 EDT) has
UTC-today one day AHEAD of station-local today, so a target one local day
out is gate-lead 0 while both sources tag it 1. The sample tags feed the
forecasts table (per-group persistence, record.py:172) and every analysis
built on it (#417's reconstruction, recover_gap), so the tags should use
one canonical definition - station-local date difference, matching the
targets - rather than three. Filed as a small improve issue.

## Deferred threads

### Sigma blind spot #400/#417 -> #426

Does the planned fix address the measured problem? Mostly yes, with one
scope gap to flag.

- The counterfactual is exact: inter_family_gap's extended_sigma
  (distribution.py:140-148) computes the equal-weight mixture variance
  over ALL groups (deterministic singletons joining with v_k=0), which is
  precisely what "deterministic groups join the mixture" produces in
  fit_gaussian. If #426(a) implements that mixture, the diagnostic already
  predicted its output, and the #417 appendix's measured ratios (mean 0.71
  sigma, 45% of groups above 0.5 sigma) quantify the expected sigma growth.
  mu needs no change: deterministic groups already participate in mu
  equally.
- Regime bundling is respected: #426(c) refits all cells and bumps
  REGIME_SINCE in the same change, which is the documented procedure the
  #400 appendix mandates, so no mixed-generation cells linger in the
  calibration table.
- FLAG (comment posted on #426): the refit as specified cannot actually
  re-base var_b on the NEW sigma semantics. #426's local refit draws its
  historical sigmas from the Previous Runs deterministic multi-model
  archive (backfill.py:244-250), which contains no ensemble members; after
  the extension, the LIVE raw sigma grows (det-ens gap enters the
  mixture) while the REFIT sigma statistic stays the old deterministic
  inter-model stdev. Feeding enlarged live sigmas into var_b coefficients
  fitted on the narrower statistic systematically over-disperses every
  full-regime predictive - the same generation-mixing hazard the #400
  appendix warned about, relocated from inside the table to the
  fit/apply boundary. The remedy exists in-repo: reconstruct historical
  per-group sample compositions from the forecasts table (the #417
  measurement already did exactly this) and fit cells on
  fit_gaussian-under-the-new-definition sigmas. #426 should either add
  that reconstruction to its refit path or record why the mismatch is
  accepted. This compounds the module-4 suspicion (the fit/apply sigma
  statistic mismatch that exists TODAY); both share the same remedy.

### TMIN wedge #404/#418 -> #426(b)

- The retarget eliminates the measured wedge: the wedge exists because the
  NWS night period (18:00 T-1 -> 06:00 T) includes pre-midnight hours that
  belong to calendar day T-1. Deriving the low from the hourly gridpoint
  forecast restricted to the target LOCAL day reproduces the settlement
  quantity's window by construction, so the mean 1.0-1.7F component and
  its direction disappear. Residual, unavoidably: hourly gridpoint vs 5-
  minute station observations (the #362 resolution-frequency effect, milder
  for TMIN than TMAX) and gridpoint-vs-station offset; both are stationary
  and are what the accompanying cell refit absorbs.
- Hourly fetch addition confirmed needed: openmeteo.py's `_common_params`
  requests daily fields only (daily=temperature_2m_max/min), and nws.py
  consumes the 12-hour period product only - neither source has an hourly
  fetch today. #426(b) needs a new hourly fetch on whichever source it
  retargets (NWS /forecast/hourly or Open-Meteo hourly=temperature_2m),
  plus a local-day reduction. The issue text ("hourly gridpoint forecast")
  implies this but does not spell out the fetch addition; called out here
  so the implementer sizes it.
- Calibration-absorption claim confirmed: the bias term absorbs the
  STATIONARY mean wedge (largely compensated today), but the 5-7F
  cold-front episodes are conditionally non-stationary and cannot be
  represented by a constant bias; they surface as residual tail error the
  spread/df terms only partially absorb. This is exactly why the retarget
  is worth the refit cost despite today's partial compensation.

### Rounding at halves #399/#416

Adjudicated; confirm-only, and confirmed: settles() still pins the evidence
test (tests/test_outcomes.py:127-150, the three prod .5 cases plus the
never-observed discriminator), and no code drift since #416 - the only
later commit touching outcomes.py (302351a) preserved round() semantics.
Truncation remains ruled out by the 81.5 -> 82 resolution. No new work.

## Nits (deliberately unfiled)

- outcomes.sort sorts by edge only; ties (YES vs NO on the same bucket)
  keep insertion order. Cosmetic.
- inter_family_gap's family classification
  (`any(s.member is not None for s in samples if _group_key(s) == key)`,
  distribution.py:106) is O(n^2) over samples; n is tens, so harmless, but
  a precomputed set would read cleaner.
- aggregate's issued_at=None bypass is a silent fresh-forever for any
  future source that forgets to stamp; a one-line warning when a None-
  stamped sample is admitted would make the stance visible.
- nws.parse returns on the FIRST F-unit violation only; a mixed-unit period
  list fails on the matching period, not earlier junk. Fine as-is.
- MIN_SIGMA_F=1.5 floor saturation on low-spread pools: recorded by the
  #373 audit; revisiting belongs to that thread, not this review.

## Filed issues

Every substantive finding above is filed and linked back to #425; nits are
listed here only, per house convention.

- #434 investigate: NWS TMIN period matcher can return a post-midnight
  night fragment instead of the target night (module 1, medium).
- #435 investigate: calibration cells are fitted on a different sigma
  statistic than the live path applies (module 4, medium suspicion).
- #436 improve: unify the no-cell uncalibrated path with
  apply_calibration's widening (module 7, low).
- #437 improve: log a traceback when a forecast source crashes inside
  aggregate() (module 3, low).
- #438 improve: one canonical lead-time definition across gate, NWS, and
  Open-Meteo tagging (cross-cutting, low).

Plus one scope flag posted on #426 itself (the refit's historical sigma
cannot re-base var_b on the new definition without a forecasts-table
reconstruction): see the deferred-threads section above.

