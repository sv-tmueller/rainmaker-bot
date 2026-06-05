# NO-side Betting Implementation Plan (#60)

**Goal:** Let the bot recommend NO bets (sell an overpriced bucket), not just YES.
A NO bet on bucket B wins when the settled temperature is not in B.

**Why:** NO on an overpriced low-probability bucket is naturally high-confidence
and positive-edge at once, which the YES-only view almost never produces. It is
also the class of bet the 0.90 floor can actually admit (relates to #58).

## Key facts (from exploration)

- NO ask is not in Gamma; derive `no_ask = 1 - best_bid` (the YES bid), and
  `no_bid = 1 - best_ask`. Confirmed complementary in the fixtures.
- `best_bid` is null on illiquid longshot buckets, so a NO bet there cannot be
  priced or filled; skip it (treat like a missing ask).
- A bet side is cross-cutting. Use an explicit `side` column on `predictions`
  and `prices` (migrations), not a label suffix; settlement P&L must join price
  by side and score NO as the complement.
- `compute_live_accuracy` collapses per-bucket rows with DISTINCT on identical
  `dist_params`; adding NO rows (same dist_params) keeps that correct, no change.

## Tasks (TDD)

### Task 1: NO pricing on the bucket
**Files:** `src/rainmaker/polymarket/markets.py`, `tests/test_polymarket_markets.py`
- Add `no_token_id: str`, `no_ask: float | None`, `no_bid: float | None` to
  `Bucket`. In `parse_bucket`: `no_token_id = token_ids[1]`;
  `no_ask = 1 - best_bid` if best_bid is not None else None;
  `no_bid = 1 - best_ask` if best_ask is not None else None.
- Tests: NO fields derived correctly; null best_bid -> no_ask None.

### Task 2: Emit NO outcomes
**Files:** `src/rainmaker/ranking/edge.py`, `tests/test_edge.py`
- `RankedOutcome.side: Literal["YES", "NO"]`.
- In `evaluate_market`, per bucket: keep the YES outcome (side="YES"); when
  `no_ask` is not None and `0 < no_ask < 1`, also emit a NO outcome with
  `p_win = 1 - p_win_yes`, `best_ask = no_ask`, `edge = p_win_no - no_ask`,
  same gates (floor, min_sources, min_edge). Sort all by edge.
- Tests: a NO outcome appears with the right p_win/edge; a NO bet on an
  overpriced unlikely bucket is recommended while its YES is not.

### Task 3: Storage with a side column
**Files:** `src/rainmaker/store/migrate.py`, `src/rainmaker/store/record.py`,
`tests/test_store.py` / `tests/test_record.py`
- Migrations: `ALTER TABLE predictions ADD COLUMN side TEXT` and same for
  `prices` (existing rows default to YES on read).
- `_record_predictions`: write `o.side`.
- `_record_prices`: write a YES row (side="YES", price=best_ask) and, when
  `no_ask` is available, a NO row (side="NO", price=no_ask). Keep `outcome` = the
  bucket label for both.
- Tests: YES and NO rows persisted with the right side and price.

### Task 4: Side-aware settlement
**Files:** `src/rainmaker/tracking.py`, `tests/test_tracking.py`
- `_settled_rows`: select `p.side`, and join prices on
  `pr.outcome = p.bucket AND pr.side = p.side`. Treat a missing `side` as YES.
- Add `_bet_won(row)`: `settles` for YES, `not settles` for NO.
- `compute_pnl` and the hit-rate use `_bet_won`. Keep the Brier over YES rows
  only (forecast calibration is unchanged; NO Brier is identical and would just
  double n).
- Tests: a winning NO bet pays `1 - no_ask`; a losing NO bet pays `-no_ask`;
  hit-rate counts NO wins.

### Task 5: Report shows the side
**Files:** `src/rainmaker/report/render.py`, `tests/test_render.py`
- Show side per bet: "BUY <bucket>" for YES, "SELL <bucket>" for NO (or a Side
  column). Header note explains NO = sell.
- Tests: a NO bet renders with its side in terminal and markdown.

### Task 6: Dashboard
**Files:** `dashboard/lib/data.ts`, `dashboard/components/BetsTable.tsx`
- `Bet` gains `side`. Read `side` from predictions; build the ask lookup keyed by
  `market_id|bucket|side`; read prices `side` too. Show the side in BetsTable
  (a Side column or BUY/SELL). Verify `npm run build`.

### Task 7: Golden e2e
**Files:** `tests/test_golden_e2e.py` (+ fixtures if needed)
- Update the expected report to include the NO outcomes the fixtures now produce.

## Verification

- `uv run pytest` green (golden e2e updated), `ruff check`, `ruff format --check`,
  `mypy src` clean.
- `npm run build` in `dashboard/`.
- One real `rainmaker run` (or against fixtures) shows NO bets where the market
  overprices an unlikely bucket.

## Out of scope / noted

- De-duplicating correlated bets (NO-on-B vs YES-on-other-buckets): the report
  lists all gate-passers ranked by edge; the human reviewer decides. Documented,
  not solved here.
- Order placement stays manual (advisory only).
