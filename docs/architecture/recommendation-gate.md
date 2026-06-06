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
