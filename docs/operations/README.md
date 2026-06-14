# Operations

How to run and operate the bot. MVP 1.0/2.0 are advisory and tracking: the bot
tells you what to bet and scores itself afterwards, but never trades. You place
bets on Polymarket yourself.

## The scheduled cloud run

GitHub Actions runs `.github/workflows/daily-run.yml` every 3 hours, on the hour
(and on manual `workflow_dispatch`): `rainmaker run`, then `rainmaker settle`,
then `rainmaker prune`, then `rainmaker snapshot`, all against Supabase Postgres
via the `DATABASE_URL`
repository secret (the Supabase session-pooler connection string). Each step
refuses to run unless that secret is a Postgres DSN, so a misconfigured secret
fails loud instead of silently writing to a throwaway SQLite file in the runner.
The dated md/json report is attached to each run as an artifact.

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
- `uv run rainmaker track`: print P&L and calibration over settled markets.
  P&L is hypothetical: one unit staked on every recommended bet at its listed
  ask, so a market re-recommended on several days counts as several bets.
- `uv run rainmaker snapshot`: upsert today's metrics row into
  `tracking_snapshot`. This is what the dashboard reads.
- `uv run rainmaker backfill --city <X>`: fit a calibration cell and backtest
  accuracy from history (NCEI actuals vs Open-Meteo historical forecasts). A
  `mae=...F` field appears in the output line. Use `--city all` to cover every
  city in one pass.

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
per-(station, variable, lead time); the default fits lead 1. The output line
includes a `mae=...F` field showing the backtest mean absolute error.

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
