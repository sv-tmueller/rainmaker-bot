# Recommendation gate and the confidence floor

The advisory recommends a bet only when all three gates hold
(`ranking/edge.py`, constants in `config.py`):

- `p_win >= CONFIDENCE_FLOOR` (0.90)
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

## Decision

Keep the flat 0.90 floor.

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
