# MVP 3.0: paper-first automated trading

Date: 2026-11-01 (owner decisions), written 2026-11-02. Issue: #475. Batch:
#482. Amended 2026-11-02 with the owner decisions of batch #489 (issue #494).
Status: approved decisions. The batch 1 foundation (#476 to #479) is merged.
Batch 2 (#489) builds the paper pricing, the fee-aware gate, paper settlement
and the deploy kit. The `trade` command is batch 3.

## Goal

Start MVP 3.0 with a dry-run trader. Each 3h run, the trader takes the bets
the advisory gate already recommends, prices each one against the live CLOB
order book, and records what the order would have filled at. No order is
posted. Live money comes in a later batch, only after the paper evidence
meets the go-live criterion below and the owner signs off separately.

## Roadmap context

- MVP 1.0 advisory and MVP 2.0 tracking are live. The bot never trades today.
- MVP 3.0 phase A (this spec): paper trading. The trade command exists (batch
  3), the mode is `paper`, no credentials and no funded wallet exist anywhere.
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

Each decision was taken with the owner on 2026-11-01, except the hosting
decision (2026-11-02) and two that the architect proposed in the #475
sub-plan: "Polymarket only" and "Paper mirrors live". The two proposals are
marked below and are open for owner review at this PR.

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

The 0.67 is derived, not measured per bet. In the 0.12 rows of the sweep,
staked divided by n (`gate_sweep.py` adds each bet's ask to `staked`, and
staked is PnL over ROI) is about 0.66 in the regime window and 0.73 in the
pure-policy window. 0.67 is a round figure near the regime value. The same
0.67 drives the fee, gap and noise (SD) numbers below.

### Hosting on the current host

Owner decision, 2026-11-02: paper trading runs on the current host. The whole
3h job (run, trade, settle, prune) moves there as a unit, deployed by #480 in
batch 2. GitHub Actions keeps CI, daily diagnostics, backups and keepalive.
Supabase and the Vercel dashboard do not change. The operator procedure is the
runbook `docs/operations/host-deploy.md` (#480).

Reasons:

- Paper places no orders, holds no key and reads only public data, as the
  advisory already does. So no host in a chosen country is needed yet.
- A trade must use the prices of the run that produced it. Splitting the job
  across two hosts adds a stale-price window, so the job moves as a unit.
- The wallet key (live phase) must never sit in GitHub secrets.

Superseded: this spec first planned a VPS in Denmark, conditional on #479's
restricted-region check. The 2026-11-02 decision replaces that plan. Where the
live phase runs is decided in the live batch, and the geoblock guard then
applies to it (see the geoblock section).

### Dedicated wallet (live phase only)

A new Polymarket account used only by the bot, a small bankroll, the key only
on the live host. Reasons: it limits the damage a bug or a leaked key can do,
reconciliation is clean because every fill belongs to the bot, and the wallet
balance becomes a second hard limit next to the caps. Phase A has no wallet.

### Polymarket only

Architect proposal (#475 sub-plan), for owner review at this PR.

Kalshi is not traded. Monthly precipitation markets are included under the
same gate and reported per variable. Reasons: the roadmap names Polymarket's
CLOB API for MVP 3.0, and the dedicated account is a Polymarket account.

### Paper mirrors live

Architect proposal (#475 sub-plan), for owner review at this PR.

Paper and live share the gate, the sizing, the limit price and the caps. They
differ in the order step: paper walks the book it fetched and records the
result, while live posts an order and reconciles the real CLOB fills (tick and
share rounding apply only to live). They also differ in the geoblock guard,
which only live enforces (see the geoblock section). Reason: paper evidence
only transfers to live if everything before the order step is the same code
path. A separate paper-only shortcut would invalidate the go-live test.

## The 3h cycle

On the host, one timer runs in order: `run`, `trade`, `settle`, `prune`. The
trade step reads the run that just finished, so its prices are minutes old.

`rainmaker trade` (batch 3, built from the foundation and batch 2 packages)
does this:

1. Read the mode (`RAINMAKER_TRADE_MODE`). `off` or unset exits. `live` is
   rejected until the live batch. (kill switch)
2. Run the geoblock guard (#479). In paper mode, record the result and go on,
   even when it reports blocked, unreachable or unparseable. Live mode aborts.
3. Require a run with status `ok` that started within
   `TRADE_MAX_RUN_AGE_MINUTES = 60`. Otherwise no trade.
4. Load that run's recommended Polymarket outcomes with their `p_win`, gross
   `edge` and quoted ask.
5. Read current exposure and open positions from the store (#477).
6. Compute each outcome's net edge and its limit price (see the net edge rule
   below and the fill pricing section).
7. Size (#478): drop outcomes that already hold a position, order by net edge,
   apply the caps, return orders with a stake.
8. For each order, fetch the book of the right token (#476), cut the asks at
   the limit price, and price the order with `price_paper_order`
   (`trading/pricing.py`, #490). It takes the fee rate as an argument and walks
   the book with a budget sized so that `spent_usdc + fee_usdc <=
   TRADE_STAKE_USDC`. It applies the minimum order size. The trader records the
   result, spend and fee included, in `paper_orders` (#477).
9. Print a summary: orders by status, USDC staked, how many orders each cap
   blocked, and the geoblock result.

Net edge rule (batch 3 trader contract). The `predictions` table keeps gross
`edge` (`p_win - ask`, per share). The trader loads it and orders and sizes by
net edge:

    net_edge = edge - taker_fee_per_share(quoted_ask, POLYMARKET_TAKER_FEE_RATE)

It records net edge in `paper_orders.edge`. This matches the fee-aware gate
(#491), whose floor and ranking use `net_edge`. The `p_win` column and the
quoted ask are stored as they are.

Never wire `trade` into `daily-run.yml`. The overlap lock is per host, so two
hosts running the job would place the same order twice.

## Sizing and risk limits

Four constants in `config.py` (#478). Three are tunable. The fourth,
`TRADE_MAX_POSITIONS_PER_OUTCOME`, is fixed at 1.

| Constant | Default | Meaning |
| --- | ---: | --- |
| `TRADE_STAKE_USDC` | 5 | cap on USDC outlay per order (fill plus fee) |
| `TRADE_MAX_NEW_EXPOSURE_PER_DAY_USDC` | 50 | new USDC outlay per UTC day |
| `TRADE_MAX_OPEN_EXPOSURE_USDC` | 150 | USDC outlay on unsettled positions |
| `TRADE_MAX_POSITIONS_PER_OUTCOME` | 1 | open per (market, outcome), fixed |

Rules:

- **Position:** a position is a filled or partial order. A `no_fill` row is
  not a position, so the trader may try the same outcome again on the next
  cycle (see the open item on `no_fill` rows).
- **Position key:** (market_id, outcome). The side is ignored. A YES and a NO
  on the same outcome count as one position, so the bot cannot hedge against
  itself. It may hold several outcomes of one market.
- **Stake:** `TRADE_STAKE_USDC` caps the total outlay per order, fill plus
  taker fee. The trader walks the book with a budget sized so that
  `spent_usdc + fee_usdc <= TRADE_STAKE_USDC`.
- **Exposure:** USDC outlay actually made, fees included
  (`spent_usdc + fee_usdc`). Spend is `shares x fill_price`. It is not a
  column in `paper_orders`. A position stays open until its market has an
  `outcomes` row. A partial fill counts what was spent. A no-fill counts zero.
- **Order of orders:** highest net edge first, with a stable tie-break (market
  id, then outcome, then side). Caps bind in that order, so the best bets win
  scarce room.
- **Within one run,** caps are charged at the planned stake. The planned stake
  is an upper bound on actual outlay, because the stake caps spend plus fee, so
  charging the caps at the planned stake within a run can never let exposure
  exceed them. After the run, the store holds the actual spend and the next run
  sees it.
- **Kill switch input:** #478 takes a boolean. A true value yields zero
  orders. In paper, the trade command passes `halted=False` after the mode
  check passes. In live, the halt flag supplies it (see the kill switch
  section).
- **Fixed positions cap:** a value of `TRADE_MAX_POSITIONS_PER_OUTCOME` above 1
  needs a spec decision and a count query first. #493 enforces the fixed 1.

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

- **Limit price:** the highest ask at which the fee-adjusted edge, as the gate
  computes it (#491), still meets the gate's floor. The floor is `MIN_EDGE +
  STATION_EDGE_DELTA`, using the station delta for the order's (station,
  variable), zero if none. The asks above the limit are dropped before the
  walk. Decision 2026-11-02, from the #490 review. The earlier rule was the
  gross `p_win - (MIN_EDGE + delta)`. Once the gate nets the taker fee out of
  edge, that gross limit would admit fills the gate rejects. "Paper mirrors
  live" needs the limit to use the same edge definition as the gate. The net
  edge falls as the ask rises, so the limit is well defined. The closed-form
  `trading.pricing.limit_price` (#502) computes it with the gate's net-edge
  definition. #490 takes the limit as an input.
- **Order type:** fill what the book offers, up to the stake budget (spend plus
  fee), at or under the limit, then cancel the rest. This mirrors the planned
  live order type, a marketable limit order that may fill partially.
- **Outcome:** `filled` (the stake budget was fully used), `partial` (some
  depth, less than the budget) or `no_fill` (nothing at or under the limit, an
  empty book, or the order is below the minimum order size).
- **Minimum order size:** the book reports `min_order_size` (5 shares in the
  sample). #490 parses it, with `tick_size`. A 5 USDC stake buys more than 5
  shares at any price under 1, so it cannot bind a full fill, but a small
  partial fill can fall under it. Such a partial is recorded as `no_fill`. It
  keeps its walk numbers, so it carries a non-null `fill_price` and `shares`.
  Reports must therefore filter on `status IN ('filled', 'partial')`, never on
  `fill_price IS NOT NULL`.
- **Taker fee:** `docs.polymarket.com/trading/fees` (fetched 2026-11-02) sets
  the Weather taker rate at 0.05. The formula is
  `fee = shares x feeRate x p x (1 - p)`, charged in USDC. The docs list no
  maker fee and a maker rebate. The rate is the constant
  `POLYMARKET_TAKER_FEE_RATE = 0.05` in `config.py`, owned by #491. It
  replaces the name `TRADE_TAKER_FEE_RATE` that earlier drafts proposed. A
  live Gamma check on 2026-11-02 showed `feesEnabled: true`, rate 0.05 and
  `takerOnly` on both temperature and precipitation markets. #490's pricing
  takes `fee_rate` as a required argument, so the trade command passes
  `POLYMARKET_TAKER_FEE_RATE` in.
- **Fee units.** The two units differ by a factor of the price. Edge and the
  limit price are per share. The fee per share is
  `rate x p x (1 - p)`, about 1.1 points at a 0.67 ask. ROI is per USDC of
  outlay. The fee per USDC is `rate x (1 - p)`, about 1.65% at a 0.67 ask. The
  1.65% figure is an ROI-unit number. Subtracting it from a per-share edge
  would overcharge by a factor of `1 / p`.
- **Fee against the measured edge.** At a 0.67 price the fee is about 1.65%
  per USDC of spend (an ROI-unit figure). That is larger than the +0.6% row of
  the 0.12 sweep and about equal to the +1.6% row, so the fee is the same size
  as the whole measured edge. The fee is computed per filled level and stored
  in `fee_usdc`. It sits inside the stake: the walk spends only what leaves
  room for the fee, so `spent_usdc + fee_usdc <= TRADE_STAKE_USDC`.
- **Fee-aware gate (owner decision, 2026-11-02).** The owner chose the
  fee-aware gate as the first fee mitigation. #491 computes `net_edge = edge -
  fee per share at the ask`. The gate floor and the ranking use `net_edge`. The
  `MAX_EDGE` cap stays on gross edge. `predictions.edge` keeps its gross
  meaning, and only `recommended` changes meaning, from the merge date. The new
  gate is a strict tightening. It covers all Polymarket markets, temperature
  and precipitation. Kalshi edges stay gross, because its fee coefficient is
  not read yet. Reason: a gross gate recommends bets that lose after fees.
  Maker orders are a later experiment, not part of this batch. Paper cannot
  simulate resting fills honestly, since it has no queue position and cannot
  know whether a resting order would ever trade.
- No rounding to the tick inside the walk. Tick size and share rounding belong
  to the live order batch.

## Kill switch and trading mode

Both controls fail closed.

**Mode (paper kill switch).** Env var `RAINMAKER_TRADE_MODE` is `off` or
`paper`. Unset or unknown means `off`. It is set on the host only, so a
database write can never turn trading on. `live` is rejected until the live
batch. Only the live batch may accept it.

Owner decision, 2026-11-02: in paper mode, the mode env is the kill switch.
The `trading_control` halt flag below moves to the live batch. Reason: paper
places no orders and risks no money, so setting the mode to `off` on the host
is enough, and the database flag adds a table, two commands and a workflow
for no gain yet. It starts to pay off once a key exists, because it can be
reached without access to the host that holds the key.

**Halt (live batch).** A database flag in an append-only table
`trading_control`:

| Column | Notes |
| --- | --- |
| `id` | primary key |
| `halted` | 0 or 1 |
| `reason` | required text |
| `actor` | who or what wrote the row |
| `created_at` | UTC timestamp |

The latest row wins. No row, a read error, or `halted = 1` all mean halted.
So the first `resume` row is the deliberate start of live trading, and a fresh
store never trades. The flag is set by `rainmaker halt --reason ...` and
`rainmaker resume --reason ...`, and by a `workflow_dispatch` halt workflow
that the owner can trigger from the GitHub mobile app. The trade step reads it
once and passes the boolean to #478. RLS is required on this table.

Why a database flag and not an env var or a sentinel file, once live:

- It can be reached without SSH access to the host that will hold the key.
- It keeps an audit trail (who, why, when).
- It fails closed on every error path.
- A live stop rule can set it automatically.
- An env var means editing a mode-600 secrets file over SSH. A sentinel file
  also needs SSH and fails open if the path is wrong.

**Hard stop.** Disabling the systemd timer on the host stops everything. This
lives in the runbook (#480), not in code.

## Geoblock guard

#479 adds a read-only check against Polymarket's geoblock endpoint (believed
to be `https://polymarket.com/api/geoblock`; #479 confirms it before coding).
The trade command runs it right after the mode check. #493 makes the request
refuse redirects. Before that fix, a caller whose HTTP client followed a 302
could read an allowed answer from the redirect target and fail open.

Owner decision, 2026-11-02: the guard blocks live mode only. A blocked host, a
network error and an unparseable response abort the live trade step. In paper
mode, the trader calls the guard and records the result, but does not abort.
Reason: paper places no orders and reads only public data, as the advisory
already does. The geoblock limits who may place orders, so stopping a
read-only job on it gains nothing. Recording the result still shows whether
the host would be blocked when live starts.

**Eligibility.** Owner decision, 2026-11-02: the account-holder eligibility
question is parked until the live deployment. Reason: paper has no account,
no wallet and no order. It stays a precondition for any wallet or live order.
An allowed host country does not make a restricted account holder eligible.
The live sign-off confirms the holder's own jurisdiction under Polymarket's
terms before a wallet is funded or an order is placed.

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
- Decision-context columns: `token_id` (not null), `p_win`, `edge` (net edge at
  the quoted ask, see the net edge rule), `quoted_ask`, `limit_price`,
  `fee_usdc` (all nullable).
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
- #492 adds the `won` and `pnl_usdc` columns in migration
  `0015_paper_orders_grading`. Grading runs inside `settle` with the advisory
  grader. `trading_control` (above) moves to the live batch with its RLS
  migration.

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

- **400.** 0.12 was chosen on 347 and 488 replayed bets. The paper sample sits
  between the two windows, so it is the same order of size as the evidence
  that set the policy it tests.
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
  the taker fee costs about 1.65% per USDC of outlay (an ROI-unit figure, see
  the fee units rule), so the measured edge implies a net ROI of about -1.0%
  to 0.0%. At the old gate the criterion is therefore expected to fail unless
  paper fills beat the sweep's edge. That is the purpose of paper first: to
  find this out before money is at risk. The fee-aware gate (#491) is a strict
  tightening, so fewer bets pass it, and the go-live numbers do not change.
- **0.5 cents.** The typical ask is about 0.67, so one cent per share costs
  about 1.5 ROI points. That wipes out the measured edge.

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
  station policies, calibration refits and caps. Grading fixes do not. Group
  such changes during the paper phase. The 2026-11-15 KSFO revisit
  (`STATION_EDGE_DELTA`) falls inside the likely window.
- Eras are recorded, not passed on the command line. #492 keeps them in a
  `PAPER_ERAS` constant in `paper_tracking.py`, one entry per era. Per-era
  counts and a fixed era boundary need a recorded list, and a `--since` date
  would not give that. An entry starts when the new policy takes effect: the
  merge time of the change, or the UTC time of the refit run. The first entry
  is "fee-aware gate (#491)", starting 2026-11-03T07:36:10Z, the merge time of
  PR #497. Operator rule for the runbook: a prod calibration refit through
  `backfill.yml` has no PR, but it is an era change, so the operator also adds
  a `PAPER_ERAS` entry that starts at the refit run's UTC time.
- The grader uses one best bet per market and run (`_best_per_market_run` in
  `src/rainmaker/tracking.py`), but the trader may hold several outcomes of one
  market. That is why the noise handling clusters by settlement date. The
  go-live report `rainmaker paper-report` (#492) shows a best-per-market view
  next to the order view, so it can be compared with `track`. The view is
  informational and sits outside the verdict.
- Grading. #492 grades paper orders inside `settle` with the advisory grader
  (`tracking._bet_won`), so a paper order and an advisory bet on the same
  outcome agree. When `regrade.yml` rewrites an outcome, the paper orders are
  re-graded. ROI, fill rate and the execution gap count only rows with status
  `filled` or `partial`.
- Bootstrap confidence intervals are deferred out of #492. The go-live check is
  70 or more days out, so a follow-up issue should land before then.

## Hosting and cutover

Paper runs on the current host (see Decisions). The operator procedure is the
runbook `docs/operations/host-deploy.md` (#480). The Denmark plan is
superseded.

Cutover steps, all operator steps, none in code:

1. Install the deploy kit on the host (#480) and move the 3h job to it with
   the mode `off`. Disable the Actions cron the same day and keep
   `workflow_dispatch`.
2. After one clean day, set the mode to `paper`, once the batch 3 trade
   command exists.
3. Never run `trade` from `daily-run.yml`.

Deploy kit contents (`deploy/` systemd units, a service user, a `flock` lock,
the Postgres-DSN guard, an `OnFailure=` hook reusing
`scripts/notify_failure.sh`, a dead-man ping, and the runbook) are specified in
#480. The lock skips an overlapping run instead of cancelling the one in
flight. That differs from the Actions concurrency group on purpose: killing a
run mid-write is worse than losing one 3h refresh. The failure alert carries
the unit result and a journal pointer, never journal text and never the
hostname, because the repo is public.

## Error handling

Every path fails closed.

| Condition | Result |
| --- | --- |
| Mode unset or unknown | `off`, no trade |
| Halt flag unreadable, absent or set (live) | halted, no trade |
| Geoblock blocked, error or unparseable | live: no trade. Paper: recorded |
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
endpoints. The trade command is a separate entry point and leaves the golden
end-to-end test untouched. The fee-aware gate (#491) did update that test, to
assert ranking by net edge.

## Live phase outline

Out of scope until its own sign-off. Outline only.

- Entry conditions: the go-live criterion passes, the halt flag and its
  commands exist and have been tested on the live host (halt, zero orders,
  resume), and the geoblock guard passes on the live host. The sign-off also
  confirms that the account holder's own jurisdiction is allowed under
  Polymarket's terms. This eligibility check was parked on 2026-11-02 and is
  a precondition for any wallet or live order.
- Add `trading_control`, the halt and resume commands and the halt workflow
  (see the kill switch section). Make the geoblock guard abort live mode.
- Add `py-clob-client`. This is a new dependency and its PR says why.
- A wallet runbook: a dedicated account, a small bankroll, the key on the live
  host only.
- Fill-or-partial marketable limit orders at the limit price, respecting
  `neg_risk` and tick size.
- Reconcile fills from the CLOB. Do not assume them.
- A live stop rule that writes the halt flag.
- Resting limit (maker) orders as a separate experiment, once live fills can
  be observed.
- Update the "bot never trades" lines in `docs/operations/README.md`.

## Batch plan

**Batch 1 (#482), foundation. Done.**

- #475 this spec.
- #476 book client and stake fill-price walk.
- #477 paper-trade store tables and exposure queries.
- #478 flat-stake sizing and risk-limit module.
- #479 geoblock guard.
- #481 is an unrelated fix in the same batch.

**Batch 2 (#489), paper foundations.** No trade command yet.

- #490 fee-aware fill pricing: `price_paper_order` in `trading/pricing.py`,
  with the book fields `min_order_size` and `tick_size`. It takes `fee_rate`
  as an argument and sizes the walk so spend plus fee stays within the stake.
  Resolves the old open items on the `walk_asks` docstring and the book
  fields.
- #491 fee-aware gate: `POLYMARKET_TAKER_FEE_RATE`, `taker_fee_per_share` and
  `net_edge`. It starts the fee-aware policy era.
- #492 paper settlement and the go-live report: migration
  `0015_paper_orders_grading`, grading inside `settle`, `PAPER_ERAS`, and
  `rainmaker paper-report` with per-era counts, the fill rate and a
  best-per-market view. This is old items (c) and (e) of the earlier plan.
- #493 hardening: the geoblock redirect fix, value rules on
  `record_paper_order` for every status, and the fixed positions cap.
- #480 the deploy kit and runbook `docs/operations/host-deploy.md`. It does
  nothing to trade while the mode is off.
- #494 this amendment.

**Batch 3, trade command and cutover on this host.**

- `rainmaker trade` in paper mode (size M), as in the 3h cycle section. It
  orders and sizes by net edge, computes the limit with the closed-form
  `trading.pricing.limit_price` (#502, the gate's net-edge definition), and
  passes `POLYMARKET_TAKER_FEE_RATE` to `price_paper_order`.
- The mode check from `RAINMAKER_TRADE_MODE` (`off` or `paper`).
- The operator cutover on the current host, per the runbook.

**Later batch, live.** A separate sign-off. See the live outline above.

If review of this spec changes a cap or the kill switch shape, a later batch
updates the code.

## Open items

- **Fee mitigations.** Resolved for the first one: the fee-aware gate is built
  in #491 (owner decision, 2026-11-02). The second, resting limit (maker)
  orders, is a later experiment. The docs list no maker fee and a maker
  rebate, but paper cannot simulate resting fills honestly. Any such change
  starts a new policy era.
- **Fee rate source.** Resolved by #491: `POLYMARKET_TAKER_FEE_RATE = 0.05` in
  `config.py`, checked against Gamma on 2026-11-02 (see fill pricing). Still
  open: the Kalshi fee coefficient, so Kalshi edges stay gross. Kalshi is not
  traded.
- **Polymarket volume share.** The bets per day on Polymarket alone sets the
  evaluation date. The first weeks of paper data give the real rate.
- **`no_fill` rows.** A `no_fill` is not a position, so the trader may retry
  an outcome every 3h, and each try writes a row that is never pruned. Decide
  whether such rows keep the full `book_snapshot` or a trimmed one, or
  whether repeated `no_fill` rows for one outcome are collapsed, given the
  egress limit (#469).
- **`walk_asks` docstring.** Resolved by #490. The docstring no longer says
  live market orders are fill-or-kill. This spec records partials as `partial`
  and plans a live order type that fills partially.
- **Fill-and-kill orders.** Confirm that `py-clob-client` supports a
  fill-or-partial marketable limit order, or find the equivalent, before the
  live batch.
- **Book fields.** Resolved by #490 (package 1 of batch #489): the book parse
  keeps `min_order_size` and `tick_size`, and the walk uses `min_order_size`.
  `neg_risk` is for the live batch.
- **Denmark.** Closed. The hosting decision of 2026-11-02 supersedes it.
- **Optional guard.** Resolved by #504. A market whose Gamma fee schedule
  disagrees with `POLYMARKET_TAKER_FEE_RATE` is barred from recommendations
  and the reason printed, so a drifted schedule cannot pass a wrong net edge
  through the gate. See `docs/architecture/recommendation-gate.md`
  ("Polymarket fee-schedule drift guard").
