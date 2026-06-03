# Operations

How to run and operate the bot. MVP 1.0 is advisory and read-only: it tells you
what to bet, you place the bet on Polymarket yourself.

## Daily report runbook

### Run it

```sh
uv run rainmaker run
```

Optional flags: `--reports-dir <dir>` (default `reports/`) and `--db <path>`
(default `rainmaker.db`). The command discovers every live US-city temperature
market, forecasts each one, ranks the outcomes by edge, prints the report, and
writes it to disk.

### What you get

- Terminal output and `reports/<date>.md`: the human report.
- `reports/<date>.json`: the same report, machine-readable (for automation).
- `rainmaker.db`: every run is recorded, plus any fitted calibration.

### How to read it

The report leads with **Recommended bets (ranked by edge)**. That is the list
to act on. If it says "No bets pass the gates today", there is nothing worth
betting and you stop there.

Each bet shows:

- `P(win)`: our forecast probability the outcome settles YES (0 to 1).
- `ask`: the YES price you would pay on Polymarket (0 to 1, ~= implied prob).
- `edge`: `P(win) - ask`. Positive edge is expected value in your favour.

A bet is recommended only if it clears the gates: `P(win)` at or above the
confidence floor (`CONFIDENCE_FLOOR`, currently 0.90) and at least
`MIN_SOURCES` forecast sources. Ranking is by edge, never by confidence alone:
a 95% outcome priced at 0.97 loses money; an 80% outcome at 0.55 is a good bet.
The per-market tables below the summary show every bucket if you want the full
picture, including high-edge but lower-confidence outcomes the gates exclude.

### Placing the bet

For each recommended bet, open that market on Polymarket and buy YES up to the
listed ask. The bot never trades; order placement is manual in 1.0 (automated
trading is MVP 3.0).

## Timing

Day-of markets are nearly resolved (the mode bucket is already priced near 1.00),
so edge is usually near zero. Real edge tends to appear one to three days before
settlement, when the forecast distribution is still wide. Running daily catches
those windows.

## Calibration

A new city ships uncalibrated: the report labels its forecast `(uncalibrated)`
and widens the spread to stay conservative. To fit a correction from history:

```sh
uv run rainmaker backfill --city "Los Angeles"
```

This pairs NOAA NCEI actuals with historical forecasts, fits a per-(station,
variable, lead) bias and spread-scale, and stores it in `rainmaker.db`. The next
run applies it and labels the forecast `(calibrated)`.

## Future automation

`reports/<date>.json` is the integration point. The CLI is deterministic and
safe to run on a schedule (cron, CI). A future version can read the JSON and
place orders through Polymarket's CLOB API; that is out of scope for 1.0.
