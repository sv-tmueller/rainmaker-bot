# MVP 3.0: paper-first automated trading

Date: 2026-11-01 (owner decisions), written 2026-11-02. Issue: #475. Batch:
#482. Status: approved decisions, pre-implementation. Nothing in this spec is
built yet; siblings #476 to #479 build the foundation in parallel.

## Goal

Start MVP 3.0 with a dry-run trader. Each 3h run, the trader takes the bets
the advisory gate already recommends, prices each one against the live CLOB
order book, and records what the order would have filled at. No order is
posted. Live money comes in a later batch, only after the paper evidence
meets the go-live criterion below and the owner signs off separately.

## Roadmap context

- MVP 1.0 advisory and MVP 2.0 tracking are live. The bot never trades today.
- MVP 3.0 phase A (this spec): paper trading. The trade command exists, the
  mode is `paper`, no credentials and no funded wallet exist anywhere.
- MVP 3.0 phase B (outlined only, last section): live orders through
  `py-clob-client`. It needs its own spec update and its own sign-off.

Polymarket access stays read-only through phase A. The "read-only in 1.0" rule
in CLAUDE.md is reworded to say so.

## Why paper first

The edge is unproven at execution-realistic prices. The facts, all from
`docs/architecture/recommendation-gate.md` (the 2026-10-19 sweep, leads 0-3):

- The live gate value `MIN_EDGE = 0.12` (#440, live since 2026-10-19) shows
  +0.6% ROI over 347 bets in the pure-policy window (since 2026-08-14) and
  +1.6% over 488 bets in the regime window (since 2026-07-18). These are the
  0.12 rows. The rows that were live when the sweep ran (0.07) showed -3.5%
  and -0.2%.
- Both numbers are inside the noise. A hit rate near 68% on a few hundred
  bets has an interval wide enough to hold a break-even strategy.
- 0.12 was chosen on the same data that now scores it, so the ROI is
  optimistic by construction.
- Every bet in the sweep was predicted under the calibration that existed
  then. The regime refit v3 (#462), the canonical lead (#460) and the 0.12
  gate all took effect on 2026-10-19, so the current policy era starts there.
- The ROI is graded at the ask stored with the run. That ask is the best
  ask, with no depth, no slippage and no taker fee. On Polymarket the NO ask
  is derived as `1 - yes_bid` (2026-10-07 update), not read from a NO book.

Paper trading against the real book removes three of those gaps (depth,
slippage, fees) and adds a clean sample under one policy era, at the cost of
some weeks of waiting and no money at risk.

## Decisions

Each decision was taken with the owner on 2026-11-01 unless marked otherwise.

### Paper first

Live money waits for paper evidence. Reason: see the previous section. The
realized edge is about 1% and cannot be told apart from zero yet.

### Flat stake, not Kelly

Every order stakes `TRADE_STAKE_USDC = 5`. Reason: the claimed edge is 0.12 to
0.25 while the realized ROI is about 1%. Kelly sizes on the claimed edge. At
the gate floor (edge 0.12) and a typical ask of 0.67, the Kelly fraction is
0.12 / 0.33, about 36% of bankroll on one bet. Calibration is still being
refit, so a sizing rule that trusts the claimed edge is the wrong tool. Flat
stake with caps bounds the loss from a bad model.

### Hosting on a VPS in Denmark

The whole 3h job (run, trade, settle, prune) moves to a VPS, deployed by
#480 in batch 2. GitHub Actions keeps CI, daily diagnostics, backups and
keepalive. Supabase and the Vercel dashboard do not change.

Reasons:

- A trade must use the prices of the run that produced it. Splitting the job
  across two hosts adds a stale-price window, so the job moves as a unit.
- GitHub-hosted runners give no control over the egress country, and
  Polymarket enforces geographic restrictions by IP.
- The wallet key (live phase) must never sit in GitHub secrets.

Denmark is conditional. #479 records whether Denmark is on Polymarket's
current restricted-region list. If it is restricted, the owner picks another
allowed country before #480 provisions anything. This spec does not assume
the answer.

### Dedicated wallet (live phase only)

A new Polymarket account used only by the bot, a small bankroll, the key only
on the VPS. Reasons: it limits the damage a bug or a leaked key can do,
reconciliation is clean because every fill belongs to the bot, and the wallet
balance becomes a second hard limit next to the caps. Phase A has no wallet.

### Polymarket only

Kalshi is not traded. Monthly precipitation markets are included under the
same gate and reported per variable. Reason: MVP 3.0 is scoped to the venue
whose CLOB API the roadmap names, and the gate evidence already separates the
two venues.

### Paper mirrors live

Paper and live differ in exactly one place: the order is recorded, not
posted. The same gate, the same sizing, the same book walk, the same limits,
the same geoblock check. Reason: paper evidence only transfers to live if the
code path is the same. A separate paper-only shortcut would invalidate the
go-live test.

## The 3h cycle

On the VPS, one timer runs in order: `run`, `trade`, `settle`, `prune`. The
trade step reads the run that just finished, so its prices are minutes old.

`rainmaker trade` (batch 2, built from the foundation packages) does this:

1. Read the mode. `off` exits. `live` is rejected in phase A. (kill switch)
2. Run the geoblock guard (#479). Blocked, unreachable or unparseable aborts.
3. Read the halt flag once. Halted aborts with a message.
4. Require a run with status `ok` that started within
   `TRADE_MAX_RUN_AGE_MINUTES = 60`. Otherwise no trade.
5. Load that run's recommended Polymarket outcomes with their `p_win`, `edge`
   and quoted ask.
6. Read current exposure and open positions from the store (#477).
7. Size (#478): drop outcomes that already hold a position, order by edge,
   apply the caps, return orders with a stake.
8. For each order, fetch the book of the right token (#476), cut the asks at
   the limit price, and walk the book with a budget sized so that
   `spent_usdc + fee_usdc <= TRADE_STAKE_USDC`. Apply the minimum order size and
   record the result, spend and fee included, in `paper_orders` (#477).
9. Print a summary: orders by status, USDC staked, and how many orders each
   cap blocked.

Never wire `trade` into `daily-run.yml`. The overlap lock is per host, so two
hosts running the job would place the same order twice.

## Sizing and risk limits

Four constants in `config.py` (#478), all tunable:

| Constant | Default | Meaning |
| --- | ---: | --- |
| `TRADE_STAKE_USDC` | 5 | cap on total USDC outlay per order (fill plus taker fee) |
| `TRADE_MAX_NEW_EXPOSURE_PER_DAY_USDC` | 50 | new USDC spent per UTC day |
| `TRADE_MAX_OPEN_EXPOSURE_USDC` | 150 | USDC spent on unsettled positions |
| `TRADE_MAX_POSITIONS_PER_OUTCOME` | 1 | open positions per (market, outcome) |

Rules:

- **Position key:** (market_id, outcome). The side is ignored. A YES and a NO
  on the same outcome count as one position, so the bot cannot hedge against
  itself. It may hold several outcomes of one market.
- **Stake:** `TRADE_STAKE_USDC` caps the total outlay per order, fill plus
  taker fee. The trader walks the book with a budget sized so that
  `spent_usdc + fee_usdc <= TRADE_STAKE_USDC`.
- **Exposure:** USDC actually spent, fees included (`spent_usdc + fee_usdc`). A
  position stays open until its market has an `outcomes` row. A partial fill
  counts what was spent. A no-fill counts zero.
- **Order of orders:** highest edge first, with a stable tie-break (market id,
  then outcome). Caps bind in that order, so the best bets win scarce room.
- **Within one run,** caps are charged at the planned stake. The planned stake
  is an upper bound on actual outlay, because the stake caps spend plus fee, so
  charging the caps at the planned stake within a run can never let exposure
  exceed them. After the run, the store holds the actual spend and the next run
  sees it.
- **Kill switch input:** #478 takes a boolean. A true value yields zero
  orders. Where the boolean comes from is defined in the kill switch section.

Monthly precipitation positions stay open for weeks and can crowd out the
150 USDC cap. The trade summary counts how often each cap blocks an order, so
the owner can see it happen and tune the constants.

Constants change only through a PR. A database write can never change a cap.

## Fill pricing

The advisory run prices bets at the Gamma best ask. The trader prices at what
the book would fill.

Book facts, confirmed against the live endpoint for #476 (read-only):

- `GET https://clob.polymarket.com/book?token_id=<id>` needs no auth. Prices,
  sizes and the timestamp (epoch milliseconds) are strings.
- Asks arrive in descending price order and bids in ascending order. The best
  price is the last element on both sides. Code that reads `asks[0]` gets the
  worst ask. The client sorts both sides best first.
- The NO token's book mirrors the YES book: NO asks are `1 - YES bids` at the
  same sizes. NO is priced from the NO token's own book, which gives depth. Its
  top price equals the `1 - yes_bid` the advisory uses.
- `last_trade_price` on the NO book returned the YES price. It is not parsed
  and not trusted.
- An unknown or closed token returns HTTP 404. That differs from an empty book
  (HTTP 200 with empty arrays). A 404 means no order and a log line. An empty
  book records a `no_fill` order.

Rules:

- **Limit price** = `p_win - (MIN_EDGE + STATION_EDGE_DELTA)`, using the
  station delta for the order's (station, variable), zero if none. It is the
  highest price at which the edge still clears the gate's own floor. The asks
  above it are dropped before the walk.
- **Order type:** fill what the book offers, up to the stake budget (spend plus
  fee), at or under the limit, then cancel the rest. This mirrors the planned live order type, a
  marketable limit order that may fill partially.
- **Outcome:** `filled` (the stake budget was fully used), `partial` (some depth,
  less than the budget) or `no_fill` (nothing at or under the limit, an empty book,
  or the order is below the minimum order size).
- **Minimum order size:** the book reports `min_order_size` (5 shares in the
  sample). A 5 USDC stake buys more than 5 shares at any price under 1, so it
  cannot bind a full fill, but a small partial fill can fall under it. Such a
  partial is recorded as `no_fill`.
- **Taker fee:** `docs.polymarket.com/trading/fees` (fetched 2026-11-02) sets
  the Weather taker rate at 0.05. The formula is
  `fee = shares x feeRate x p x (1 - p)`, charged in USDC. The docs list no
  maker fee and a maker rebate. At a 0.67 price the fee is about 1.65% of the
  spend. That is larger than the +0.6% row of the 0.12 sweep and about equal to
  the +1.6% row, so the fee is the same size as the whole measured edge. The fee
  is computed per filled level and stored in `fee_usdc`. It sits inside the
  stake: the walk spends only what leaves room for the fee, so
  `spent_usdc + fee_usdc <= TRADE_STAKE_USDC`. Batch 2 re-checks the rate
  against the market's page before relying on it.
- No rounding to the tick inside the walk. Tick size and share rounding belong
  to the live order batch.

## Kill switch and trading mode

Two independent controls. Both fail closed.

**Mode.** Env var `RAINMAKER_TRADE_MODE` is `off`, `paper` or `live`. Unset or
unknown means `off`. It is set on the host only, so a database write can never
turn trading on. Batch 2 rejects `live`. Only the live batch may accept it.

**Halt.** A database flag in an append-only table `trading_control`:

| Column | Notes |
| --- | --- |
| `id` | primary key |
| `halted` | 0 or 1 |
| `reason` | required text |
| `actor` | who or what wrote the row |
| `created_at` | UTC timestamp |

The latest row wins. No row, a read error, or `halted = 1` all mean halted.
So the first `resume` row is the deliberate start of trading, and a fresh
store never trades. The flag is set by `rainmaker halt --reason ...` and
`rainmaker resume --reason ...`, and by a `workflow_dispatch` halt workflow
that the owner can trigger from the GitHub mobile app. The trade step reads it
once and passes the boolean to #478. RLS is required on this table.

Why a database flag and not an env var or a sentinel file:

- It can be reached without SSH access to the host that will hold the key.
- It keeps an audit trail (who, why, when).
- It fails closed on every error path.
- A live stop rule (phase B) can set it automatically.
- An env var means editing a mode-600 secrets file over SSH. A sentinel file
  also needs SSH and fails open if the path is wrong.

**Hard stop.** Disabling the systemd timer on the VPS stops everything. This
lives in the runbook (#480), not in code.

## Geoblock guard

#479 adds a read-only check against Polymarket's geoblock endpoint (believed
to be `https://polymarket.com/api/geoblock`; #479 confirms it before coding).
The trade command runs it first. A blocked host, a network error and an
unparseable response all abort the trade step.

It is enforced in paper mode too. Paper runs are only valid from the host that
live will use, otherwise the evidence tests a path live cannot take. So paper
runs only on the VPS. An allowed VPS country does not make a restricted
account holder eligible. Batch 3's sign-off confirms the holder's own
jurisdiction under Polymarket's terms.

## Store additions

New tables go in the base schema (`store/db.py`), per repo rule. A table
created on prod after migration `0013_enable_rls` has RLS off, which lets the
public anon key read and change it through PostgREST. So every new trading
table needs a Postgres-only migration that enables RLS (for example
`0014_paper_orders_rls`). SQLite records it as a no-op, as 0013 does.

`paper_orders` (#477):

- Order columns: `run_id`, `market_id`, `outcome`, `side` (YES or NO),
  `stake_usdc`, `fill_price`, `shares`, `book_snapshot` (JSON text), `status`,
  `created_at`.
- Decision-context columns: `token_id` (not null), `p_win`, `edge` (at the
  quoted ask), `quoted_ask`, `limit_price`, `fee_usdc` (all nullable).
- Status values: `filled`, `partial`, `no_fill`.

Why the context columns: `store/prune.py` drops all but the latest same-day
prices and predictions rows once a market settles. An order placed on an
earlier run would lose its `p_win`, edge and quoted ask, and the go-live
execution-gap metric needs the quoted ask. The columns cost little on a new
table and a migration on a live one.

Other rules:

- Paper orders are never pruned.
- Queries that score orders must not select `book_snapshot`. It is large and
  free-tier egress is limited (#469).
- Batch 2 adds `trading_control` (above) and grading (`won`, from the canonical
  grader at settle).

## Go-live criterion

All three conditions must hold, checked once, when the sample is reached.

1. **Sample.** At least 400 settled paper orders with a fill (partial fills
   count, no-fills do not), spread over at least 60 distinct settlement dates,
   all in one policy era.
2. **ROI.** At least +1.0% net of fees at book-walked fills, computed as the sum
   of P&L after taker fees over the sum of USDC outlay (fill plus fee).
3. **Execution gap.** The mean of (average fill price minus the run's stored
   ask) is at most 0.5 cents per share.

Why these numbers:

- **400.** 0.12 was chosen on 347 and 488 replayed bets. The paper evidence
  should be at least as large as the evidence that set the policy it tests.
  The sweep windows ran at about 5 bets a day across both venues, and the
  trader sees fewer on Polymarket alone, so expect 70 to 130 days. At the 5
  USDC stake, 400 orders spend up to 2,000 USDC, so +1.0% is about 20 USDC.
- **60 dates.** Outcomes of one market move together and one day's weather is
  shared across cities. Sixty dates keep a single cold snap from deciding the
  result.
- **+1.0% net.** The go-live ROI bar is net of fees: realized P&L after taker
  fees, divided by total USDC outlay (fill plus fee). The bar stays at +1.0%
  net. It is a margin above break-even, not a reproduction of the sweep. The
  2026-10-19 sweep measured +0.6% to +1.6% gross at stored asks. At a 0.67 ask
  the taker fee costs about 1.65% of spend, so the measured edge implies a net
  ROI of about -1.0% to 0.0%. At the current gate the criterion is therefore
  expected to fail unless paper fills beat the sweep's edge. That is the purpose
  of paper first: to find this out before money is at risk.
- **0.5 cents.** The typical ask is about 0.67, so one cent per share costs about
  1.5 ROI points. That wipes out the measured edge.

### What the test can and cannot show

The spec states the limits plainly.

- The per-order return SD is about 0.7 per dollar staked. One standard error on
  ROI is about 3.5 points at 400 orders, and 4 to 5 points after correlation.
- So the ROI test can rule out a clearly losing strategy. It cannot confirm a 1%
  edge. A true +1.5% edge passes only about half the time.
- The execution gap is a paired price difference, so its error is a small
  fraction of a cent. It is the precise part of the test, and the one thing
  the stored-ask sweeps cannot measure.

### Noise handling

- Confidence intervals come from a bootstrap that resamples settlement dates,
  not orders. They are reported in the verdict. They are not a pass or fail
  condition.
- The sample size is fixed in advance and checked once. No early go-live on a
  hot streak. The only early exit is a no-go.
- On a fail, extend once by 200 orders and check once more. A second fail means
  re-planning the model, not a third try.
- One era only. Any change to which orders get placed, or to the `p_win` behind
  them, starts a new era and resets the count. That covers gate constants,
  station policies, calibration refits and caps. Grading fixes do not. Group such
  changes during the paper phase. The 2026-11-15 KSFO revisit
  (`STATION_EDGE_DELTA`) falls inside the likely window.
- The grader uses one best bet per market and run (`_best_per_market_run` in
  `src/rainmaker/tracking.py`), but the trader may hold several outcomes of one
  market. That is why the noise handling clusters by settlement date. The
  go-live report (batch 2, package c) shows a best-per-market view next to the
  order view, so it can be compared with `track`.

## Hosting and cutover

DK hosting depends on #479's restricted-region check (see Decisions).

Cutover steps, all operator steps, none in code:

1. Provision the VPS (#480). Move the 3h job to it with the mode `off`. Disable
   the Actions cron the same day and keep `workflow_dispatch`.
2. After one clean day, set the mode to `paper` and write the first `resume`
   row.
3. Never run `trade` from `daily-run.yml`.

Deploy kit contents (`deploy/` systemd units, an overlap lock, the Postgres-DSN
guard, an `OnFailure=` hook reusing `scripts/notify_failure.sh`, a dead-man
ping, and a runbook in `docs/operations/`) are specified in #480.

## Error handling

Every path fails closed.

| Condition | Result |
| --- | --- |
| Mode unset or unknown | `off`, no trade |
| Halt flag unreadable, absent or set | halted, no trade |
| Geoblock error, blocked or unparseable | blocked, no trade |
| No run with status `ok` in the last 60 minutes | no trade |
| Book fetch fails (including 404) for an outcome | no order for that outcome |
| Empty asks, or no ask at or under the limit | `no_fill` order recorded |
| Store write fails | the step fails and the failure alert fires |
| `asset_id` in the book differs from the requested token | the fetch raises |

The step never retries a failed order on its own. The next cycle re-evaluates
from the store.

## Testing

TDD for the math: the book walk, sizing and caps, and the go-live criterion
(the bootstrap and the three checks). Synthetic inputs with known answers.
API clients are tested against saved fixtures (`tests/fixtures/`), never live
endpoints. The golden end-to-end test is untouched: the trade command is a
separate entry point and does not change forecasting, calibration or ranking.

## Live phase outline

Out of scope until its own sign-off. Outline only.

- Entry conditions: the go-live criterion passes, the kill switch has been
  tested on the VPS (halt, zero orders, resume), and the geoblock guard passes
  on the VPS. The sign-off also confirms that the account holder's own
  jurisdiction is allowed under Polymarket's terms.
- Add `py-clob-client`. This is a new dependency and its PR says why.
- A wallet runbook: a dedicated account, a small bankroll, the key on the VPS
  only.
- Fill-or-partial marketable limit orders at the limit price, respecting
  `neg_risk` and tick size.
- Reconcile fills from the CLOB. Do not assume them.
- A live stop rule that writes the halt flag.
- Update the "bot never trades" lines in `docs/operations/README.md`.

## Batch plan

**Batch 1 (#482), foundation.**

- #475 this spec.
- #476 book client and stake fill-price walk.
- #477 paper-trade store tables and exposure queries.
- #478 flat-stake sizing and risk-limit module.
- #479 geoblock guard.
- #481 is an unrelated fix in the same batch.

**Batch 2, paper trade and cutover.**

- (a) Kill switch, mode and the halt workflow (size S).
- (b) `rainmaker trade` in paper mode (size M). Blocked by (a) and #476 to #479.
- (c) Paper settlement and the go-live report (size M). It shows per-era counts,
  the fill rate and a best-per-market view.
- (d) #480, the VPS deploy kit (size M). It does nothing to trade while the mode
  is off.
- (e) If needed, a migration for `paper_orders` columns and RLS (size S).

**Batch 3, live.** A separate sign-off. See the live outline above.

#476 to #479 implement decisions already written in their issue bodies, plus
the RLS requirement and the context columns above. If review of this spec
changes a cap or the kill switch shape, a later batch updates the code.

## Open items

- **Fee mitigations.** Fee mitigations are open items for the owner before
  batch 2. (1) A fee-aware gate: the edge is computed net of the taker fee at
  the quoted ask, so only bets that clear MIN_EDGE after fees are placed. (2)
  Resting limit (maker) orders, which pay no maker fee and earn a rebate per
  Polymarket's fee docs, at the cost of fill uncertainty. Either one changes
  which orders are placed and starts a new policy era.
- **Fee rate source.** The 0.05 weather taker rate comes from Polymarket's docs
  page. The page names no API endpoint. Batch 2 confirms the rate for each
  market and stores it as a constant (proposed name `TRADE_TAKER_FEE_RATE`,
  not yet in code).
- **Denmark.** The #479 result decides the hosting country.
- **Polymarket volume share.** The bets per day on Polymarket alone sets the
  evaluation date. The first weeks of paper data give the real rate.
- **Fill-and-kill orders.** Confirm that `py-clob-client` supports a
  fill-or-partial marketable limit order, or find the equivalent, before the
  live batch.
- **Book fields.** If the book parse keeps `min_order_size`, `tick_size` and
  `neg_risk`, batch 2 uses the first and batch 3 the others.
