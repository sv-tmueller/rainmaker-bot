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
