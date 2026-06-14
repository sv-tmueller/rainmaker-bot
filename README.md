# rainmaker-bot

An advisory bot for betting on US-city weather markets on Polymarket. It gathers
weather forecasts from free sources, turns them into a calibrated probability for
each market outcome, compares that probability to the market price, and produces
a daily report of bets ranked by edge (expected value). A human reviews the
report and places every bet manually.

## Status

MVP 1.0 (advisory) and MVP 2.0 (tracking) are live for 11 US cities across
temperature (TMAX and TMIN) and monthly-precipitation markets, on both Polymarket
and Kalshi:

- A scheduled GitHub Actions run (every 3h) discovers live markets, forecasts,
  ranks by edge, and writes a report.
- It settles past markets against NOAA actuals and records a daily
  P&L/calibration snapshot to Supabase Postgres.
- A read-only dashboard (in `dashboard/`) shows the recommended bets and the
  track record, deployed on Vercel behind Cloudflare Access.

Remaining MVP 1.0 slice: the daily-binary precipitation form (will it rain on a
given day). MVP 3.0 (automated trading) has not started.

## How it decides

Recommendations are ranked by edge, not by raw confidence. A 95% outcome priced
at 97 cents loses money; an 80% outcome at 55 cents is a good bet. Each forecast
targets the exact quantity that settles the market (the resolution station,
agency, rounding, and settlement time), and nothing is recommended on partial or
stale data.

## Running it

Python 3.11+ with [uv](https://docs.astral.sh/uv/).

```sh
uv sync                                # install
uv run rainmaker run                   # daily edge-ranked report (reports/ + datastore)
uv run rainmaker settle                # record NOAA actuals for past markets
uv run rainmaker track                 # P&L and calibration over settled markets
uv run rainmaker snapshot              # write the daily metrics row the dashboard reads
uv run rainmaker backfill --city all   # fit calibration from history
uv run rainmaker backtest --city all   # forecast calibration + win-rate over history
```

Commands use local SQLite unless `DATABASE_URL` points at Postgres (the cloud run
sets it from a secret). Checks: `uv run pytest`, `uv run ruff check .`,
`uv run mypy src`. The dashboard is verified with `npm run build` in `dashboard/`.

## Roadmap

- MVP 1.0: advisory. Done for temperature (TMAX and TMIN) and monthly
  precipitation, on Polymarket and Kalshi; only the daily-binary precipitation
  form remains. Free sources only (NWS/NOAA and Open-Meteo), read-only access,
  bets placed by hand.
- MVP 2.0: tracking. Done. Settle against NOAA actuals, log P&L, report
  calibration over time, scheduled cloud run, web dashboard.
- MVP 3.0: automated trading via Polymarket's CLOB API. Not started.

## For contributors

- `CLAUDE.md` - how we work in this repo, and the rules that are easy to get
  wrong.
- `docs/operations/README.md` - how to run, deploy, and operate the bot.
- `docs/superpowers/specs/2026-05-29-mvp1-advisory-design.md` - the approved MVP
  1.0 design.

## License

**Copyright © 2026 Thomas Mueller. All rights reserved.**

This is proprietary software. No license is granted to use, copy, modify, merge, publish, distribute, sublicense, or sell any part of this software, in whole or in part, in any other project — public or private — without prior written permission from the copyright holder.

Unauthorized reuse of any portion of this code constitutes copyright infringement and will be pursued accordingly.
