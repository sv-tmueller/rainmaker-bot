# rainmaker-bot

An advisory bot for betting on US-city weather markets on Polymarket. It gathers
weather forecasts from free sources, turns them into a calibrated probability for
each market outcome, compares that probability to the market price, and produces
a daily report of bets ranked by edge (expected value). A human reviews the
report and places every bet manually.

## Status

Pre-implementation. Only docs exist so far; no code is scaffolded yet.

## How it decides

Recommendations are ranked by edge, not by raw confidence. A 95% outcome priced
at 97 cents loses money; an 80% outcome at 55 cents is a good bet. Each forecast
targets the exact quantity that settles the market (the resolution station,
agency, rounding, and settlement time), and nothing is recommended on partial or
stale data.

## Roadmap

- MVP 1.0 (current): advisory. Free sources only (NWS/NOAA and Open-Meteo),
  Polymarket read-only, bets placed by hand.
- MVP 2.0: tracking. Settle markets against NOAA actuals, log P&L, report
  calibration over time.
- MVP 3.0: automated trading via Polymarket's CLOB API.

## For contributors

- `CLAUDE.md` - how we work in this repo, and the rules that are easy to get
  wrong.
- `docs/superpowers/specs/2026-05-29-mvp1-advisory-design.md` - the approved MVP
  1.0 design.
