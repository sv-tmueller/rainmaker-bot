# Recommendation gate and the confidence floor

The advisory recommends a bet only when all three gates hold
(`ranking/edge.py`, constants in `config.py`):

- `p_win >= CONFIDENCE_FLOOR` (0.80, relaxed from 0.90; see the resolution below)
- `n_sources >= MIN_SOURCES` (2)
- `edge >= MIN_EDGE` (0.05)

The same three gates apply to each side. A YES bet is priced off the YES ask; a
NO bet off the NO ask (`1 - yes_bid`) with `p_win = 1 - p_yes`.

## The question (issue #58)

A live run on 2026-06-05 scored 242 buckets and recommended none: the big-edge
buckets were low-confidence (blocked by the 0.90 floor), and the only buckets
over 0.90 were priced near 1.00, so they carried no edge. That looked like the
floor contradicting the project's "rank by edge, not confidence" principle and
suppressing high-EV early bets.

## Evidence (2026-06-06)

Two checks, both with tools already in the repo.

**Forecast backtest** (`rainmaker backtest`, #59 Part 1) over 7,789 city-days
plus 193 real closed markets, scoring uncalibrated raw fits at the archive
horizon (~lead 1):

- Reliability: claimed probabilities are honest in the low (longshot) regime and
  overconfident in the high regime. At a claimed 0-30% the outcome happens about
  as often; at a claimed 70-95% it happens only ~42-48%.
- Central-interval coverage is 39/67/78% against the nominal 50/80/90%: the raw
  Gaussian is too narrow.
- On real market buckets (wider than the synthetic 2F ladder) coverage is near
  nominal (46/76/88) with only mild overconfidence, so the severe reliability
  numbers are partly a narrow-bucket artifact.

**Live re-run** on 2026-06-06 (post-#60 NO betting, uncalibrated, throwaway DB),
22 markets:

- 3 recommended bets, all NO, all clearing 0.90 on their own: LA 68-69 NO
  (edge +0.22), Miami 88-89 NO (+0.16), Denver 94-95 NO (+0.07). The empty board
  on 2026-06-05 was a YES-only, pre-#60 artifact.
- The floor still blocks some positive-edge bets just under it, for example
  SF 64-65 NO at p 0.89, edge +0.17. That bet sits at p_yes ~0.11, in the
  well-calibrated regime, so the 0.89 is trustworthy and the block is a real
  miss.

## Initial lean (superseded by the P/L evidence below)

The first read of the reliability evidence argued to keep the flat 0.90 floor:

1. The forecast is overconfident, so lowering the floor would bet on inflated
   probabilities (a claimed 70% that lands ~42%). Lowering it is negative-EV on
   current calibration.
2. NO-side betting (#60) already clears the empty-board symptom in the low-p_yes
   regime, where calibration is good. No floor change is needed to surface clean
   bets.
3. Tuning the floor for profit needs a betting P/L backtest against historical
   odds (#59 Part 2), which is blocked on a historical-odds source. Until then
   any floor number is intuition, not evidence.

## Deferred option

The data shows an asymmetry the flat floor ignores: a given floor value is
trustworthy for NO-on-longshots (low p_yes, well-calibrated) but not for
YES-on-favorites (high p_yes, overconfident). A regime-aware floor (a lower bar
for NO bets, or for the low-p_yes regime) would capture bets like SF 64-65 NO
without admitting overconfident YES favorites. Hold this until #59 Part 2 can
score it against real P/L.

## Update 2026-06-06: P/L evidence reopens this (#58)

#59 Part 2 shipped (`rainmaker backtest-pnl`), so the floor can now be scored
against historical P/L. A sweep over a 45-day universe, replaying the gates at
leads 0-2, points the other way from the decision above:

| Floor | Bets | Win% | Total P/L | ROI |
| ---: | ---: | ---: | ---: | ---: |
| 0.90 | 231 | 90% | +25.97u | +14.3% |
| 0.80 | 313 | 89% | +50.17u | +21.8% |
| 0.70 | 344 | 85% | +54.97u | +23.0% |
| 0.60 | 363 | 80% | +49.02u | +20.2% |

Relaxing to ~0.75-0.80 nearly doubles total P/L and lifts ROI from +14% to ~+22%
with win rate holding ~89%, the original #58 hypothesis backed by P/L rather than
the reliability curve.

The one caveat was that the backtest priced at the token mid, not the ask, and
the bets a lower floor adds are longshot markets, so mid-vs-ask optimism might
inflate exactly those. So we checked it.

## Resolution 2026-06-06: relax the floor to 0.80

`pnl_backtest` gained a spread haircut (`ask = mid + spread/2`). Live weather-market
spreads measured first: median 0.8c overall (p90 5c), and the longshot buckets
where the added bets live are tighter still (median 0.4c, p90 3c). So the
mid-vs-ask gap is small. Re-running the sweep with a conservative flat 5c spread
(well above those):

| Floor | Spread | Bets | Win% | Total P/L | ROI |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 0.90 | 0c | 213 | 91% | +28.6u | +17.4% |
| 0.90 | 5c | 186 | 90% | +23.1u | +15.9% |
| 0.80 | 5c | 262 | 87% | +35.5u | +18.5% |
| 0.70 | 5c | 290 | 84% | +38.9u | +18.9% |

Even charging the stress-test 5c spread, relaxing beats 0.90 on both ROI
(+18.5% vs +15.9%) and total P/L (+35.5u vs +23.1u), win rate holding 84-87%. The
mid-vs-ask concern is real but small and does not reverse the ranking.

Decision: set `CONFIDENCE_FLOOR = 0.80`. It captures most of the gain while
keeping the highest win rate of the relaxed options (87% vs 0.70's 84%) - a
measured step, not an over-extension into the low-confidence tail.

Caveats carried forward: `min_sources` was relaxed to 1 in the backtest (the
archive is one source), so this is a superset of the live two-source gate; the
forecast is at the archive horizon (~lead 1); the window is a recent 45 days; the
flat 5c spread is conservative, so the true edge is likely closer to the 0c
column. The regime-aware floor above remains a future refinement; 0.80 is a flat
step toward it. Revisit as more settled history accrues.

## Update 2026-06-16: per-side regime floor adopted (#85)

The deferred "regime-aware floor" option above is now implemented. A sweep over
190 closed Polymarket TMAX markets (730-day window, leads 0-3, real fill prices
from the CLOB trades endpoint) scores each scheme. The analysis used the
yes=0.80 family to isolate the pure NO-floor effect (YES floor unchanged):

| Scheme | Bets | W-L | Win% | Total P/L | ROI |
| --- | ---: | ---: | ---: | ---: | ---: |
| flat 0.80 (baseline) | 279 | 222-57 | 79.6% | +19.06u | +9.4% |
| flat 0.85 | 247 | 199-48 | 80.6% | +12.30u | +6.6% |
| no=0.75, yes=0.80 | 309 | 240-69 | 77.7% | +23.58u | +10.9% |
| no=0.70, yes=0.80 | 325 | 240-85 | 73.8% | +17.44u | +7.8% |
| no=0.65, yes=0.80 | 344 | 244-100 | 70.9% | +13.77u | +6.0% |
| no=0.75, yes=0.85 | 309 | 240-69 | 77.7% | +22.63u | +10.4% |
| no=0.70, yes=0.85 | 325 | 240-85 | 73.8% | +16.49u | +7.4% |
| no=0.65, yes=0.85 | 344 | 244-100 | 70.9% | +12.82u | +5.5% |

The decision rule is the marginal cohort, not total P/L (the totals share a
common base). Reading the 0.05-step increments in the yes=0.80 column:

| Added cohort | Added bets | Added P/L | Added P/L per bet |
| --- | ---: | ---: | ---: |
| 0.80 -> 0.75 (p_no in [0.75, 0.80)) | +30 | +4.52u | +0.15u |
| 0.75 -> 0.70 (p_no in [0.70, 0.75)) | +16 | -6.14u | -0.38u |
| 0.70 -> 0.65 (p_no in [0.65, 0.70)) | +19 | -3.67u | -0.19u |

The 0.75 threshold is exactly where marginal value turns negative. The 30 added
NO bets at p_no in [0.75, 0.80) deliver +4.52u at +15% P/L per bet. The next
cohort destroys value. This aligns with the calibration evidence: the NO
(longshot) regime is well-calibrated at p_no > ~0.75; below that the forecast
becomes less reliable.

Decision: adopt `CONFIDENCE_FLOOR_NO = 0.75`, keep `CONFIDENCE_FLOOR = 0.80`
(YES floor unchanged). The YES floor does not change because the yes=0.80 and
yes=0.85 columns confirm NO improvement from raising it.

Caveats: this sweep is TMAX-only (no precip P/L evidence). The precip path
accepts `floor_no` in its API but is not relaxed here - no evidence to do so.
`min_sources` was 1 in the backtest (archive is one source); the live gate uses
2. Fill coverage was partial for some low-volume buckets, which may fall back to
the mid price; this slightly optimistic pricing is the same caveat as the
original 0.80 decision.

## Update 2026-06-27: upper edge / confidence cap (#205)

### What was built

Two optional upper-bound parameters added to `backtest-pnl`: `--max-edge` and
`--max-p-win` (both `float | None`, default None = no cap). The cap is applied
inside `replay_market` after `evaluate_market` returns the `recommended` list,
but before the best-edge `max(...)` pick. Any recommended outcome with
`edge > max_edge` or `p_win > max_p_win` is dropped; the replay then picks the
best of what remains. If no recommended bet survives the cap, the lead is
skipped (no bet). A capped lead falls through to the next-best recommended bet
rather than being deleted entirely. The live ranking path (`edge.py`) is
untouched (seam B): the golden e2e is unaffected by construction.

The filter is side-agnostic: `RankedOutcome.p_win` and `.edge` already encode
the chosen side (a NO outcome stores `p_no` as `p_win`).

`PnlBacktestResult` carries `max_edge` and `max_p_win`; `render_pnl_report`
discloses them when set.

### Sweep tables (numbers pending a data-access run)

[These tables were never filled in and are superseded by the 2026-07-06
re-run below.]

Each row is a full alternative policy replayed over the 730-day closed-market
universe (190 TMAX markets, leads 0-3, floor 0.80 flat - no asymmetric NO
floor). Read totals directly (unlike the lower-floor sweeps, the upper cap rows
are not nested supersets - each row is a standalone policy over the same
universe, so totals are directly comparable without a marginal-cohort
decomposition).

Note: `backtest-pnl` has no `--floor-no` flag, so the backtest runs at the flat
0.80 floor on both sides. This is looser than the live NO gate
(`CONFIDENCE_FLOOR_NO=0.75`), meaning the sweep is a superset on that axis too
(same spirit as the `min_sources=1` superset caveat above).
[Correction 2026-07-05 (#226): the flag exists now, and the direction was
inverted. 0.80 is the stricter NO threshold, so those sweeps were a subset on
this axis, not a superset.]

Preferred pricing mode: `--asks trades` (real CLOB fills; no spread added).
Fall back to `--spread 0.05` only if trades coverage is too thin at the extremes
and produces anomalous results; disclose which was used.

**Upper edge cap sweep** (`max_p_win` left unset):

| max_edge | Bets | W-L | Win% | Total P/L | ROI |
| ---: | ---: | ---: | ---: | ---: | ---: |
| none (baseline) | _pending_ | _pending_ | _pending_ | _pending_ | _pending_ |
| 0.50 | _pending_ | _pending_ | _pending_ | _pending_ | _pending_ |
| 0.30 | _pending_ | _pending_ | _pending_ | _pending_ | _pending_ |
| 0.20 | _pending_ | _pending_ | _pending_ | _pending_ | _pending_ |
| 0.10 | _pending_ | _pending_ | _pending_ | _pending_ | _pending_ |

Commands to reproduce (ALL row from each run):

```
# Baseline (no cap)
uv run rainmaker backtest-pnl --days 730 --leads 0,1,2,3 --asks trades

# max_edge caps
uv run rainmaker backtest-pnl --days 730 --leads 0,1,2,3 --asks trades --max-edge 0.50
uv run rainmaker backtest-pnl --days 730 --leads 0,1,2,3 --asks trades --max-edge 0.30
uv run rainmaker backtest-pnl --days 730 --leads 0,1,2,3 --asks trades --max-edge 0.20
uv run rainmaker backtest-pnl --days 730 --leads 0,1,2,3 --asks trades --max-edge 0.10
```

**Upper confidence cap sweep** (`max_edge` left unset):

| max_p_win | Bets | W-L | Win% | Total P/L | ROI |
| ---: | ---: | ---: | ---: | ---: | ---: |
| none (baseline) | _pending_ | _pending_ | _pending_ | _pending_ | _pending_ |
| 0.99 | _pending_ | _pending_ | _pending_ | _pending_ | _pending_ |
| 0.97 | _pending_ | _pending_ | _pending_ | _pending_ | _pending_ |
| 0.95 | _pending_ | _pending_ | _pending_ | _pending_ | _pending_ |
| 0.90 | _pending_ | _pending_ | _pending_ | _pending_ | _pending_ |

Commands to reproduce:

```
# max_p_win caps (same baseline as above)
uv run rainmaker backtest-pnl --days 730 --leads 0,1,2,3 --asks trades --max-p-win 0.99
uv run rainmaker backtest-pnl --days 730 --leads 0,1,2,3 --asks trades --max-p-win 0.97
uv run rainmaker backtest-pnl --days 730 --leads 0,1,2,3 --asks trades --max-p-win 0.95
uv run rainmaker backtest-pnl --days 730 --leads 0,1,2,3 --asks trades --max-p-win 0.90
```

### Caveats

- **Numbers pending**: the CLOB history endpoint was not reachable from the
  sandbox where this code was built. Fill in the table by running the commands
  above where network access to `data-api.polymarket.com` is available. Do not
  fabricate numbers.
- **In-sample risk**: the 730-day backtest universe is the same archive used for
  the #85 floor decision. It is the OOS check for the live tail signal (26 live
  days) but is itself an archive-horizon, single-source, `min_sources=1`
  superset. Real live performance will differ.
- **Non-monotonicity**: prior sweeps showed the edge >0.50 tail ran +218% ROI
  on very thin live stakes (26 markets). That is almost certainly noise from a
  small sample. Compare the backtest rows against the live tail result to see
  whether the backtest reproduces or contradicts it; thin-stake tail rows will
  be noisy in the backtest too (few bets, wide confidence interval).
- **Pricing mode**: use `--asks trades` for comparability with the #85 floor
  table. If trades coverage is thin at the extreme cap values (very few bets
  have fills), note it and compare with `--spread 0.05`.

### Ship / no-ship recommendation

Pending the sweep numbers. Once the table is filled in, evaluate:

1. If capping at some `max_edge` value raises ROI without unacceptable total
   P/L loss vs the baseline, adopt that cap as a live gate (edit `edge.py` in a
   follow-on issue; do not edit it here - this is the backtest-only seam).
2. If capping at some `max_p_win` value raises ROI, same path.
3. If neither sweep shows a stable improvement over the baseline (i.e., ROI
   fluctuates with cap value and no clear optimum), the recommendation is
   no-ship: leave the gates uncapped and revisit once more live history accrues.

Decision authority: operator, after reviewing the filled-in tables.

## Update 2026-06-28: edge/confidence cap rejected (#205/#218)

[Void declared by the 2026-07-05 #226 update below; resolved by the
2026-07-06 section at the end of this doc, which confirms the no-cap
direction on honest evidence.]

The cap sweep was run over the 730-day closed-market universe with `--asks trades`,
leads 0,1 (leads 2-3 produce no bets; closed-market discovery is mildly
non-deterministic, ~166-185 markets/run).

| Cap | Bets | Win rate | ROI |
| --- | ---: | ---: | ---: |
| uncapped | 221 | 75% | +8.2% |
| edge <= 0.30 | 231 | 77% | +2.9% |
| edge <= 0.20 | 182 | 80% | +0.7% |
| p_win <= 0.90 | 120 | 61% | -6.1% |

Decision: do not ship a cap. Both cap types monotonically reduce ROI. The
high-edge / high-confidence bets are the profitable ones in the backtest - the
opposite of the recent live signal (#201), where those bets underperformed. The
live underperformance is likely the overconfidence now addressed by bias
calibration (#201); the backtest uses calibrated forecasts. Revisit once
calibration has re-measured live performance.

Caveats: the 730-day universe is the same archive used for the #85 floor
decision; it is in-sample for the floor but out-of-sample for the cap question.
`min_sources` is relaxed to 1 in the backtest (archive is one source), so
recommended is a superset of the live two-source gate. Closed-market discovery
is mildly non-deterministic (~166-185 markets/run), so the sign of the ROI gap
holds but magnitudes are noisy. Run with `--asks trades`, leads 0,1 (leads 2-3 produce
no bets).

## Update 2026-07-05: the gate now requires an applied full calibration (#225)

A live finding showed dispersion, not just bias, driving the gap between
claimed and realized win rates: buckets claimed at 85-100% confidence realized
only about 45-56%. `bias_only` calibration shifts the mean but keeps the
widened raw sigma (`UNCALIBRATED_WIDEN = 1.25`), which is exactly the
underdispersed object producing that gap. Only full EMOS calibration corrects
sigma (`sqrt(var_a + var_b * ensemble_var)`).

Decision: `evaluate_market`'s `recommended` gate now also requires
`calibrated == "full"`, on top of the confidence floor, min-sources, and
min-edge gates already in place, and the existing `not uncalibratable` (ghcnd)
guard. The required tier is `full`, not `bias_only`: `bias_only` still carries
the widened-raw sigma this change is meant to stop betting on. #224's backfill
gives live cells the sample count (`n >= MIN_CAL_SAMPLES = 30`) to reach
`full`; relaxing to `bias_only` later is a one-line change, backed by #229's
diagnostic if the evidence supports it.

Scope: the temperature path (`evaluate_market`) only. `evaluate_precip_market`
hardcodes `calibrated="uncalibrated"` as an honest label; no precip
calibration exists, and #224 cannot restore precip coverage. Gating precip
recommendations off entirely would kill the monthly-precip book, a product
decision out of scope here.

Interim opt-out: `evaluate_market` gained `require_calibration: bool = True`.
The live callers (`cli.py`) pass no explicit value and get the fail-safe by
default. `pnl_backtest.py`'s replay never has a fitted calibration to pass
(the archive backtest has no calibration cell), so it opts out explicitly
with `require_calibration=False` and a comment naming #226, which is expected
to remove that opt-out once it replays against a real calibration cell.

Until #224's backfill runs in production, this suppresses nearly all live
recommendations: only cells with an existing full fit (currently lead-1 TMAX)
survive. That is the intended fail-safe, not a regression.

## Update 2026-07-05: backtest-pnl now replays the production gate (#226)

The 2026-06-28 cap rejection above stated "the backtest uses calibrated
forecasts." That premise was false: `pnl_backtest.py` opted out of the #225
full-calibration gate (`require_calibration=False`) because it had no
calibration cell to pass. Every sweep table in this document, including the
2026-06-28 cap rejection numbers, was produced against raw, uncalibrated
forecasts at a flat 0.80 floor, not the calibrated production policy.

Decision: void the 2026-06-28 cap decision pending a re-run. Do not treat its
ROI numbers as evidence for or against a cap until the sweep is repeated
under the fixed replay.

What changed: `backtest_pnl` now takes a `calibration_lookup(icao, lead)`
callable, resolved once per station group and threaded into `replay_market`,
which passes the resulting cell to `evaluate_market` and no longer opts out
of the full-calibration gate. Three points carried over from the sub-plan:

- **Calibration provenance**: cells are loaded from the current calibration
  table (the same `load_calibration` the live run uses), not fit walk-forward
  from data strictly before each replayed date. The fit can therefore include
  some of the replayed dates, a mild look-ahead. Walk-forward was rejected:
  the archive is shallow (early 2024 onward, thinned by `season_window`), so a
  strict "before this date" fit would starve most of a 730-day replay window.
  The look-ahead is disclosed in the report and accepted as second-order (a
  3-parameter EMOS fit over dozens to hundreds of pairs has small per-point
  leverage, and any shared in-sample optimism largely cancels across a
  comparative sweep).
- **Which cell per lead**: the lead-L cell at lead L, mirroring the live load.
  Known mismatch, disclosed not fixed: the replay forecast is the archive
  multi-model at roughly lead 1 for every lead, so a lead-3 cell corrects a
  lead-1-horizon forecast. A per-lead replay forecast is follow-up material.
- **Missing cells suppress bets**: a (station, lead) slot with no full-tier
  cell on file recommends nothing, matching the live gate. The report now
  discloses full-tier cell coverage (cells found vs slots checked) and warns
  on stderr when zero cells load.

The re-run itself is an operator step, not part of this change: it needs
`DATABASE_URL` set to the production store and #233's backfill to have
populated cells there first. Once run, correct the sweep tables above in a
dated follow-up.

## Update 2026-07-05: the historical sweeps also graded look-ahead prices and the wrong actual (#227)

Two more sources of optimism affected every sweep table above, on top of the
look-ahead calibration fit and the opted-out gate already noted. `backtest.py`
and `pnl_backtest.py` graded every station against the bare NCEI actual, the
same source settle.py and backfill.py stopped using for Polymarket cities once
NCEI was measured to bucket-flip about 32% of markets against settlement
(ASOS about 15%). And `replay_market` snapped each replayed lead's price to
the nearest point in either direction within 12h, so a lead-N bet could be
priced on information up to 12h after the simulated decision time.

Decision: both are fixed here, not just disclosed. Actuals now route through
`venue_actuals` (ASOS for Polymarket stations, NCEI for Kalshi-only KNYC/KMDW),
the same routing backfill and settle already use. Price and fill snapping is
now look-back-only (`last_before` with a bounded max age), so no replayed
price timestamp lands after its simulated decision time; a lead whose only
nearby price is in the future is honestly skipped rather than priced ahead of
itself. No new sweep numbers are produced in this change: it is superseded by
the same pending re-run #226 already flagged.

## Update 2026-07-06: cap sweep re-run under the honest replay, no cap ships (#230)

This is the dated follow-up promised by the 2026-07-05 #226 update. The
2026-06-27 sweep is re-run under the #226 production-faithful replay,
re-deciding the voided 2026-06-28 call.

Method: `backtest-pnl --days 730 --leads 0,1,2,3 --asks trades`, production
policy replay (full-calibration gate on, floor 0.80, NO floor 0.75, min edge
0.05, min_sources relaxed to 1 as always). Calibration coverage 44 of 44
(station, lead) slots. Trades fill coverage about 368 of 736 lead-market
slots, remainder falls back to mid. Universe ~184-217 closed markets per run
(discovery is mildly non-deterministic, so magnitudes are noisy, signs and
ordering are the signal). Leads 2-3 produce no bets, all results are leads
0-1. Each row is one standalone run.

Upper edge cap (max_p_win unset):

| max_edge | Bets | W-L | Win% | Total P/L | ROI |
| ---: | ---: | ---: | ---: | ---: | ---: |
| none (baseline) | 210 | 190-20 | 90% | +28.71u | +17.8% |
| 0.50 | 210 | 191-19 | 91% | +29.81u | +18.5% |
| 0.30 | 217 | 197-20 | 91% | +26.52u | +15.6% |
| 0.20 | 202 | 185-17 | 92% | +21.21u | +12.9% |
| 0.10 | 123 | 116-7 | 94% | +10.78u | +10.2% |

Upper confidence cap (max_edge unset):

| max_p_win | Bets | W-L | Win% | Total P/L | ROI |
| ---: | ---: | ---: | ---: | ---: | ---: |
| none (baseline) | 210 | 190-20 | 90% | +28.71u | +17.8% |
| 0.99 | 203 | 183-20 | 90% | +28.12u | +18.2% |
| 0.97 | 163 | 144-19 | 88% | +22.86u | +18.9% |
| 0.95 | 132 | 111-21 | 84% | +15.55u | +16.3% |
| 0.90 | 78 | 63-15 | 81% | +9.91u | +18.7% |

Decision: no cap ships, both gates stay uncapped, per rule 3 of the
2026-06-27 ship/no-ship recommendation. Tighter edge caps degrade ROI
monotonically from the 0.50 row (18.5 -> 15.6 -> 12.9 -> 10.2 percent), and
the max_edge 0.50 and max_p_win rows sit within run-to-run noise of the
baseline while every binding cap cuts total P/L. The high-edge, high-confidence
bets remain the profitable ones under the calibrated replay, confirming the
direction of the voided 2026-06-28 decision on honest evidence.

## Update 2026-07-09: same-ruler replay after per-model source weighting, within noise (#263)

Method: a re-run of the 2026-07-06 baseline replay on post-#248 code (the
per-model source weighting from #239/#248, merged 2026-07-06 22:16). Same
command as the 2026-07-06 baseline. Produced on main at 404694b, with
calibration cells freshly fitted by `backfill --city all` into a local SQLite.

```
uv run rainmaker backtest-pnl --days 730 --leads 0,1,2,3 --asks trades
```

| | 2026-07-06 baseline | 2026-07-09 (post-#248) |
| --- | ---: | ---: |
| Closed markets in universe | ~184-217 | 219 |
| Bets | 210 | 242 |
| W-L | 190-20 | 215-27 |
| Win rate | 90% | 89% |
| Total P/L | +28.71u | +31.27u |
| ROI | +17.8% | +17.0% |
| Calibration coverage | 44/44 | 44/44 |
| Trades fill coverage | 368/736 | 438/876 |

Per-lead detail for the 2026-07-09 run: lead 0 produced 97 bets at 84-13,
+12.21u, +17.0% ROI, mean edge 18%. Lead 1 produced 145 bets at 131-14,
+19.07u, +17.0% ROI, mean edge 15%. Leads 2-3 produced no bets; the archive
forecast is roughly lead 1 for every lead, same as the baseline runs.

The per-model source weighting left replay economics within run-to-run noise.
Discovery is non-deterministic, so the universe moved from 184-219 markets
across runs and the window end shifted by 3 days. No regression, no material
gain. Total P/L is higher on a slightly larger universe, but per-bet
economics stay flat.

## Update 2026-07-18: station-policy gate terms - KNYC excluded (#302), KSFO TMAX edge-floor delta (#303)

The gate gains a new per-station term alongside the confidence floor,
min-sources, and min-edge gates already documented above: `station_policy`, a
`STATION_POLICIES` map in `config.py` keyed by ICAO and consumed inside
`evaluate_market`. It follows the same shape as the existing uncalibratable
gate (intl markets, ghcnd_id is None): recommended is forced off on every
outcome, both sides, while the forecast and advisory display stay intact
(`outcomes` non-empty, `mu`/`sigma` set). `MarketReport.policy_exclusion`
carries the reason string verbatim into the rendered report. Un-excluding a
station is a one-line change: delete its `STATION_POLICIES` entry.

`STATION_POLICIES["KNYC"]` implements the #296 addendum's verdict
(`docs/architecture/tail-objective-decision.md`, "Addendum (#296):
KSFO/KNYC per-station tail anatomy"): KNYC is a genuine forecast-skill
problem, not a calibration one. 72% of its TMAX busts fall below every one
of the five Open-Meteo models' own daily extreme (no model, individually,
forecast the actual), and both leads clear the 4x-nominal severity cut by a
wide margin (50%/25% observed lower-.05 hit rates vs a 20% cut, i.e. 10x and
5x nominal). A forecast-skill gap this severe is not fixable by widening or
shifting a Gaussian: no amount of scale/bias correction recovers information
no input model ever had. The addendum's own verdict sentence scoped the
exclusion to "the live path's TMAX ladder"; issue #302 resolved that in favor
of a station-wide exclusion (also covering TMIN), since Diagnostic B flags
KNYC in both TMIN cells too (13-14% hit rates vs the 5% nominal claim) and
the addendum did not run a TMIN-specific anatomy to justify a narrower scope.

Scope: KNYC is Kalshi-only (Polymarket has no Central Park market), so this
only ever binds on Kalshi-discovered markets. Tracking is untouched by
design: discovery, forecasting, settlement, tail-check, and P&L all keep
recording KNYC exactly as before; only the `recommended` flag changes, the
same continuity the uncalibratable gate already gives intl markets.

Path back in: the station-metadata audit the addendum's own caveats name
(Central Park's coordinates, canopy, and sensor siting were not checked
against what the forecast models and settlement pipeline actually query).
That audit is out of #302's scope; STATION_POLICIES is the one place to
revisit once it lands.

### KSFO TMAX: a raised edge floor, not an exclusion (#303)

The #296 addendum's other flagged station gets a different mechanism.
`STATION_EDGE_DELTA`, a `dict[tuple[str, str], float]` in `config.py` keyed
by `(icao, variable)`, adds a per-market delta to `MIN_EDGE`: the recommended
gate on both YES and NO sides becomes `edge >= MIN_EDGE + delta` instead of
`edge >= MIN_EDGE`. `STATION_EDGE_DELTA[("KSFO", "TMAX")] = 0.05` doubles
KSFO TMAX's bar to 0.10; every other gate (confidence floor, min sources,
the full-calibration requirement) and every other station or variable is
untouched. `evaluate_market` takes the resolved delta as `min_edge_delta`
(default 0.0, so every existing call site and the golden e2e fixture, which
has no KSFO market, stay byte-identical); the caller looks it up from
`STATION_EDGE_DELTA` by `(market.target.station.icao, market.target.variable)`,
the same pure-function shape `station_policy` already has. `cli.py`'s two
temperature call sites and `pnl_backtest.py`'s `replay_market` all make the
same lookup, so the P/L backtest replays the identical live gate (#226).
`MarketReport.edge_floor_delta` and `.edge_floor_delta_reason` carry the
applied delta and a human-readable reason into both renderers, mirroring
`policy_exclusion`'s placement.

**Why KSFO is a penalty and KNYC is an exclusion.** The addendum classified
KSFO spread-dominant (39/50 busts, 78%, land inside the 5-model envelope: a
pooled-spread problem, not a forecast-skill one) and prescribed the frozen
rule's evidence bar for that branch: a season-pure per-station refit (JJA-only
fit, newest-14-day eval, one fit per lead) must move the station's lower-.05
PIT ratio into [0.5, 1.5] at both TMAX leads to justify a calibration fix.
KSFO's refit measured 4.29 (lead 1) and 2.86 (lead 2), both far outside the
bar and both worse than the already-broken pooled baseline (2.71 at lead 1).
The frozen rule's fallback for a failed refit in the spread-dominant branch is
a confidence penalty, not exclusion (KSFO's severity was never checked
against the exclusion cut; that cut belongs to the forecast-dominant branch
KNYC took). The two stations are on record with opposite mechanisms and,
correctly, opposite actions.

**Mechanism: an edge-floor delta, not a probability haircut or a widened
sigma.** Two alternatives were considered and rejected:

- *Probability haircut* (shrink p_win toward the ask before ranking): this is
  a raised edge floor at fixed `MIN_EDGE` by another name, except it also
  corrupts the displayed and recorded p_win and leaks into the confidence-floor
  gate, a second gate the penalty was never meant to touch. Its effect cannot
  be isolated to the one gate the addendum's evidence actually supports.
- *Widened sigma* (hand-pick a per-station scale multiplier): this is exactly
  the sigma-scaling the season-pure refit already tried and measured, and it
  failed the [0.5, 1.5] evidence bar decisively (4.29/2.86). The frozen rule's
  fallback for a failed refit is a penalty, not a substitute calibration fix;
  hand-picking a widening the fitted refit already showed does not work would
  be strictly worse, and it would poison the recorded predictive parameters
  the 2026-11-15 revisit needs to measure cleanly.

An edge-floor delta stays entirely inside the ranking gate: p_win, sigma, and
every recorded predictive parameter are untouched, so the 2026-11-15 revisit
(the same accrual horizon the addendum's KMDW/KSEA rows use) can still ask
"did the underlying calibration improve" without the penalty itself
contaminating the answer.

**Parameter: +0.05, double the plain bar.** Edge inherits p_win's
overstatement one-for-one, so edge is the right unit for a penalty sized to
that overstatement. At a claimed p_no near 0.95 (the confidence-floor
neighborhood these bets live in), a 2x understatement of the lower tail's
true spread corresponds to roughly 5 points of overstated probability at the
claim level; +0.05 is sized to absorb that 2x case. The archive's ratios
(4.29/2.86) do not translate directly into a live-run magnitude (the archive
caveat above: archive sigma is multi-model disagreement, not the live pooled
spread), so no finer-grained fit is possible from this evidence; 0.05 is the
recorded judgment value, with the 2026-11-15 revisit as the scheduled
re-check, the same date the addendum's own KMDW/KSEA season-scoped rows use.

**Rejected refinements**, narrower than the delta chosen:

- *Raise the confidence floor instead*, for KSFO only: rejected because the
  live P/L sweep behind `CONFIDENCE_FLOOR` (see the 2026-06-06 update above)
  found the opposite direction profitable - a higher floor suppresses the
  highest-edge bets, which is backwards for a station whose problem is
  overstated probability, not overstated confidence band width.
- *A direction-aware penalty* (only the lower-tail side, since KSFO's busts
  are all lower-tail): more surgical, but the frozen rule calls for a
  penalty, not a bespoke per-tail rule, and the addendum did not test whether
  KSFO's upper tail is clean enough to leave unpenalized. Out of scope for a
  v1 the frozen rule wants simple; a candidate for the 2026-11-15 revisit if
  the flat delta proves too blunt.

Un-penalizing KSFO TMAX, or retuning the delta, is a one-line change:
edit or delete its `STATION_EDGE_DELTA` entry.
