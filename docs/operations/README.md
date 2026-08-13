# Operations

How to run and operate the bot. MVP 1.0/2.0 are advisory and tracking: the bot
tells you what to bet and scores itself afterwards, but never trades. You place
bets on Polymarket yourself.

## The scheduled cloud run

GitHub Actions runs `.github/workflows/daily-run.yml` every 3 hours, on the hour
(and on manual `workflow_dispatch`): `rainmaker run`, then `rainmaker settle`,
then `rainmaker prune`, all against Supabase Postgres via the `DATABASE_URL`
repository secret (the Supabase session-pooler connection string). Each step
refuses to run unless that secret is a Postgres DSN, so a misconfigured secret
fails loud instead of silently writing to a throwaway SQLite file in the runner.
The dated md/json report is attached to each run as an artifact.

`.github/workflows/daily-diagnostics.yml` runs once a day, after the 21:00 run's
settle: `rainmaker snapshot`, then `rainmaker track`, `rainmaker tail-check`,
and `rainmaker attribution`. These are read-only diagnostics; running them
every 3 hours instead of daily was most of the Supabase egress this bot
generates (#277), since `snapshot` upserts one row per day regardless of
cadence.

`track`, `tail-check`, and `attribution` each print a full-history view and a
since-filtered view, restricted to the active calibration regime's start date
(the value passed to `--since` in the `diagnostics` job of
`daily-diagnostics.yml`). The full-history view is the cross-regime baseline;
the since-filtered view isolates calibration under the current fit, since a
prior refit can make older runs a poor guide to current performance.
`track --since` prints the same pooled P&L/Brier aggregate as a plain
`track`, plus a per-(variable, lead) Brier/hit-rate breakdown restricted to
that population, so a per-cell cost prediction (for example one recorded in a
refit's decision doc) is directly comparable to live numbers. `track` and
`tail-check` run twice in the workflow (once plain, once with `--since`) to
get both views; `attribution --since` prints both views from a single
invocation instead, sharing one settled-rows read internally so the workflow
never issues a second full-history query for it (#334). When a refit lands (a
prod `backfill` re-run), update the `--since` value passed in the
`diagnostics` job of `daily-diagnostics.yml` in the same change that records
the refit.

Every CLI command targets local SQLite (default `rainmaker.db`) unless
`DATABASE_URL` is set to a postgres DSN. Export the prod DSN locally only when
you mean to touch prod.

## Commands

- `uv run rainmaker run`: discover live US-city temperature markets, forecast,
  rank by edge, print and write the report, persist the run.
- `uv run rainmaker settle`: record NOAA NCEI daily extremes for past markets
  into `outcomes`. NOAA is a documented proxy for Weather Underground, the true
  resolution source. Idempotent catch-up: NCEI lags a day or two, so unsettled
  markets are simply retried on later runs.
- `uv run rainmaker prune`: drop all-but-latest intraday rows per settled
  (market, UTC day) from prices/predictions/forecasts, to bound storage.
- `uv run rainmaker track`: print P&L and calibration over settled markets,
  plus a per-(variable, lead) Brier/hit-rate breakdown. P&L is hypothetical:
  one unit staked on every recommended bet at its listed ask, so a market
  re-recommended on several days counts as several bets. `--since YYYY-MM-DD`
  restricts the breakdown (not the pooled P&L/Brier line) to predictions from
  runs started on or after that date, matching `tail-check`'s `--since`.
- `uv run rainmaker attribution`: print per-segment P&L attribution over the
  same deduped bets as `track`, broken out by city, venue, variable, lead,
  edge bucket, and p_win bucket. `--since YYYY-MM-DD` prints a second,
  since-restricted attribution after the full-history one, from the same
  settled-rows read, matching `track`'s and `tail-check`'s `--since`.
- `uv run rainmaker snapshot`: upsert today's metrics row into
  `tracking_snapshot`. This is what the dashboard reads.
- `uv run rainmaker backfill --city <X>`: fit a calibration cell and backtest
  accuracy from history (NCEI/ASOS actuals vs Open-Meteo Previous Runs
  forecasts) for every (variable, lead) cell the live run can bet: TMAX and
  TMIN at leads 0-3 by default. A `mae=...F` field appears in the output line
  per cell. Use `--city all` to cover every city in one pass.
- `uv run rainmaker calibration-check`: print every stored calibration cell
  (read-only; never fits or writes). Flags a Student-t fit clustered at the
  df search bounds so it is visible without querying the table by hand.

## Ungradable settled rows (skip-and-count)

`track` and `snapshot` score every settled prediction row by grading its bucket
label against the actual value: first against the market's stored
`outcome_spec`, falling back to the label parsers (`parse_bucket_label`,
`parse_precip_bracket_label`) for legacy rows. A row whose label matches no
spec entry and that the fallback parser also cannot read is ungradable. It is
skipped rather than crashing the job, and counted: `track` and `snapshot`
print `skipped N ungradable settled row(s)` whenever that count is nonzero (no
line at all when it is zero). Watch that line as the coverage signal; a
climbing count without a matching parser fix below means something upstream is
recording labels the store can't join back to a settlement rule.

### Root cause (#330, #331, #333)

`daily-diagnostics` (settled-row scoring) started failing Aug 7, and
`weekly-metrics` Aug 10, both on
`ValueError: unrecognized precip bracket label: 'inches'` raised from
`domain.parse_precip_bracket_label`. Before the fix, every P&L/calibration
computation in `tracking.py` let that exception propagate, so one bad row
killed both jobs.

The label came from Kalshi's monthly-rain parser
(`kalshi/precip_markets.py`): `label=market.get("subtitle") or
market["ticker"]` trusted the venue's `subtitle` field with no check. Kalshi
sent a bare unit string ("inches", no digit) for some strikes of the NYC
August-2026 rain ladder (confirmed from the `daily-run` report artifacts for
runs `31086017767`/`31097248956`/`31110384503`, 2026-08-06); other strikes on
the same ladder that run had a normal digit-bearing subtitle ("4 inches", "2
inches"), so this was a per-strike defect, not a whole-ladder one.
`kalshi/markets.py` (temperature) had the identical `subtitle or ticker`
pattern, unexercised so far, so it got the same guard.

The stored `outcome_spec` didn't cover the bad rows either: `store/record.py`
overwrites a market's `outcome_spec` on every run
(`outcome_spec = excluded.outcome_spec`), so once Kalshi's subtitle recovered
to a normal value on a later run, the spec was rewritten with the good labels
and the earlier prediction/price rows recorded against `'inches'` were
orphaned; the label is the only join key back to a settlement rule, and it is
mutable at the market level while predictions are immutable at record time.
That mismatch is the general defect skip-and-count hardens against,
independent of the parser fix.

Fixed two ways: (1) `tracking.py` grades through one seam, `_won`, that
returns `None` for an ungradable row instead of raising; every caller
(`compute_pnl`/`_bet_won`, `_cell_stats`, `_segment_stats`,
`compute_live_calibration`, `compute_tail_calibration`) skips a `None` and
counts it. (2) `kalshi/precip_markets.py` and `kalshi/markets.py` no longer
trust a subtitle with no digit (or a missing one): they synthesize the
canonical label (`<2"`, `2-3"`, `>4"` for precip; `"{t}°F or below"` /
`"{lo}-{hi}°F"` / `"{t}°F or higher"` for temperature) from the already-parsed
strike geometry instead, and a ladder-level guard raises if two strikes
synthesize the same label. A well-formed, digit-bearing subtitle is never
rewritten, since historical rows join on it as-is.

### Disposition for the bad prod rows

Not executed here: prod DB access is out of scope for this change, and
re-grading historical bets is a non-goal. The rows recorded against the bare
`'inches'` label (Kalshi NYC monthly rain, prediction/price rows from the
2026-08-06 run window) stay ungradable forever; they will show up in
`track`'s skipped count until pruned. If an operator wants to reclaim them
instead of leaving them skipped, the repair is to re-derive each row's
canonical label from its strike geometry and rewrite `predictions.bucket` (and
the matching `prices.outcome`) to match, for example:

```sql
-- Illustrative only: confirm each row's true kind/lo/hi/threshold against the
-- Kalshi event you pulled it from before running anything against prod.
UPDATE predictions SET bucket = '<2"' WHERE market_id = '<event_ticker>' AND bucket = 'inches' AND <row identifies the <2" strike>;
UPDATE prices SET outcome = '<2"' WHERE market_id = '<event_ticker>' AND outcome = 'inches' AND <same row>;
```

Since the bare-subtitle strikes on one ladder can't be told apart from the
label alone (that's the whole defect), this needs the original Kalshi ticker
IDs recovered from the `daily-run` artifacts to know which strike each `bucket
= 'inches'` row actually was before rewriting it.

## Daily report runbook

### Run it

```sh
uv run rainmaker run
```

Optional flags: `--reports-dir <dir>` (default `reports/`) and `--db <path>`.

### What you get

- Terminal output and `reports/<date>.md`: the human report.
- `reports/<date>.json`: the same report, machine-readable.
- The datastore: every run is recorded, plus calibration and outcomes.

### How to read it

The report leads with **Recommended bets (ranked by edge)**. That is the list
to act on. If it says "No bets pass the gates today", there is nothing worth
betting and you stop there.

Each bet shows:

- `P(win)`: our forecast probability the outcome settles YES (0 to 1).
- `ask`: the YES price you would pay on Polymarket (0 to 1, ~= implied prob).
- `edge`: `P(win) - ask`. Positive edge is expected value in your favour.

A bet is recommended only if it clears the gates: `P(win)` at or above the
confidence floor (`CONFIDENCE_FLOOR`, currently 0.80) and at least
`MIN_SOURCES` forecast sources. Ranking is by edge, never by confidence alone:
a 95% outcome priced at 0.97 loses money; an 80% outcome at 0.55 is a good bet.
The per-market tables below the summary show every bucket if you want the full
picture.

### Placing the bet

For each recommended bet, open that market on Polymarket and buy YES up to the
listed ask. The bot never trades; order placement is manual (automated trading
is MVP 3.0).

## Timing

Day-of markets are nearly resolved (the mode bucket is already priced near 1.00),
so edge is usually near zero. Real edge tends to appear one to three days before
settlement, when the forecast distribution is still wide. The daily run catches
those windows.

## Calibration

A new city ships uncalibrated: the report labels its forecast `(uncalibrated)`
and widens the spread to stay conservative. To fit a correction from history:

```sh
uv run rainmaker backfill --city "Los Angeles"
# or: uv run rainmaker backfill --city all
```

The next run applies it and labels the forecast `(calibrated)`. Cells are
per-(station, variable, lead time); the default fits every cell the live run
can bet (TMAX and TMIN, leads 0-3). The output line includes a `mae=...F`
field showing the backtest mean absolute error. Student-t cells also print
`df=...`, so a refit's fitted degrees of freedom is verifiable straight from
the workflow log.

To refresh the cloud database without handling the DSN locally, trigger the
`backfill` workflow in the GitHub Actions tab (Run workflow). It runs
`backfill --city all` against Supabase using the `DATABASE_URL` repo secret.

## The dashboard

`dashboard/` is a read-only Next.js app showing today's recommended bets and the
latest tracking snapshot (P&L, record, ROI, Brier, hit rate).

Deploy: a Vercel project with root directory `dashboard/`, env vars
`SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` (used server-side only). The app
has no auth code; access is gated at the edge by Cloudflare Access. Local dev:
copy `dashboard/.env.example` to `dashboard/.env.local`, fill it in, then
`npm run dev` in `dashboard/`.

### Access hardening

The dashboard exposes the bot's recommended bets and performance, so it must not
be publicly reachable. Two doors have to be closed: the custom hostname (gated by
Cloudflare Access) and the auto-generated `*.vercel.app` URL (gated by Vercel
Deployment Protection, because it skips Cloudflare entirely). The hostname is
`rainmaker.strueller.de`. Mirror the operator's existing Access-fronted Vercel
project for the policy and protection settings.

1. Custom domain. Vercel project -> Settings -> Domains -> add
   `rainmaker.strueller.de`. In Cloudflare DNS add the CNAME Vercel shows, proxy
   ON (orange cloud), and set SSL/TLS mode to Full (strict). If Vercel's
   certificate issuance stalls while the record is proxied, set the record to
   DNS only (grey cloud) until the cert is issued, then turn the proxy back on.
   If the zone already forwards to another site (a Redirect Rule, Page Rule, or
   Bulk Redirect), scope it to exclude this hostname, or the request is 301'd
   away before it reaches Vercel or Access.
2. Cloudflare Access. Zero Trust -> Access -> Applications -> Add a self-hosted
   app for `rainmaker.strueller.de`, reusing the existing Allow policy. Access
   applications match by hostname, so an app on another host does not cover this
   one; create a new app and attach the same policy.
3. Close the vercel.app bypass. The auto-generated `*.vercel.app` deployment URLs
   do not pass through Cloudflare, so they are an unauthenticated side door, and
   they leak (Vercel posts preview URLs on PRs). Vercel project -> Settings ->
   Deployment Protection -> enable Vercel Authentication (mirror the other
   project) so all deployment URLs require login.

Verify:

```sh
# Custom host: Cloudflare Access login for this hostname.
curl -sI https://rainmaker.strueller.de
#   -> 302 to <team>.cloudflareaccess.com/.../login/rainmaker.strueller.de

# Preview deployments: gated by Vercel Authentication.
curl -sI https://rainmaker-bot-git-<branch>-<team>.vercel.app
#   -> 401 (Vercel SSO). The production alias rainmaker-bot.vercel.app instead
#      301-redirects to the custom host, which is gated, so that path is fine too.
```

Done when the custom hostname prompts for Cloudflare Access login and no
`*.vercel.app` URL serves the dashboard unauthenticated.

## Automation status

The scheduled cron is the automation for 1.0/2.0. `reports/<date>.json` and the
Supabase tables (`predictions`, `prices`, `outcomes`, `tracking_snapshot`) are
the integration points. Order placement stays manual; automated trading via the
CLOB API is MVP 3.0.
