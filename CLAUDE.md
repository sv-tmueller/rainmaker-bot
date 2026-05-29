# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

rainmaker-bot is an advisory bot for betting on US-city weather markets on
Polymarket. It gathers weather forecasts from multiple free sources, turns them
into a calibrated probability for each market outcome, compares that probability
to the market price, and produces a daily report of bets ranked by edge
(expected value). A human reviews the report and places bets manually.

Status: pre-implementation. The repo is currently empty except for docs. Nothing
is scaffolded yet. Do not assume code, a `pyproject.toml`, or a test suite
exists until Phase 1 lands.

## Where decisions live

Read these before proposing changes that touch their area:

- `docs/superpowers/specs/2026-05-29-mvp1-advisory-design.md` - the approved MVP
  1.0 design. Architecture, the forecast/calibration/ranking logic, the data
  model, and the phase plan. This is authoritative until the code exists; once
  code lands, the code wins and this doc gets updated in the same PR.

When a contradiction appears between code and a doc, the code wins and the doc
is corrected in the same change.

## Core rules specific to this project

These are easy to get wrong and they break the whole premise:

- Rank by edge, not by raw confidence. The "best call" is the one where our
  forecast probability most exceeds the market price, subject to a confidence
  floor and a minimum-source gate. A 95% outcome priced at 97 cents loses money;
  an 80% outcome at 55 cents is a good bet. Never recommend on confidence alone.
- Forecast the exact quantity that settles the market. The resolution source
  (station, agency, rounding, settlement time) is sacred. Getting the wrong
  station or variable makes a perfect forecast worthless. Confirm each market's
  resolution rule before trusting any prediction for it.
- Never emit a recommendation on partial or stale data. If a forecast source is
  down, proceed with the rest and reflect reduced coverage in confidence. If
  Polymarket is down, abort with a clear message. Exclude data past its
  freshness limit and say so in the report.
- Free sources only in MVP 1.0: NWS/NOAA and Open-Meteo. Paid sources are a
  revenue-gated roadmap item, not part of 1.0. Do not add a paid API without it
  being an explicit decision.
- Polymarket access is read-only in 1.0. No order placement, no trading
  credentials, no funded wallet. Automated trading is MVP 3.0.

## Roadmap (do not pull later phases forward)

- MVP 1.0: advisory (current). Highest-effort foundation: the data has to be
  near-perfect.
- MVP 2.0: tracking. Settle markets against NOAA actuals, log P&L, report
  calibration over time. Likely the point we move SQLite to Supabase Postgres
  and add a web dashboard.
- MVP 3.0: automated trading via Polymarket's CLOB API.

The MVP 1.0 schema is SQLite but is designed to port to Supabase Postgres later
(JSON columns map to jsonb). Keep it portable; do not add SQLite-only features.

## Phase order

Phase 0 (discovery spike) is a hard gate. Do not build the forecast pipeline
before confirming that live Polymarket US-city weather markets exist and
documenting how they resolve. If they are absent or thin, stop and raise it (for
example, consider Kalshi) rather than building on an unverified premise. See the
spec for the full phase plan.

## Toolchain (intended, once scaffolded in Phase 1)

Python, with `httpx`, `pydantic`, `numpy`, `scipy`, `pandas`. CLI entry point
`rainmaker run` (and `rainmaker backfill` for calibration). When you scaffold
the project, record the exact install/test/lint/run commands here so future
sessions do not have to rediscover them.

## How we work

- Every unit of work is a GitHub issue first.
- Branch from `main` per issue: `feat/<issue-number>-<short-slug>` or
  `fix/<issue-number>-<short-slug>`. Merge via PR. PR references the issue with
  `Closes #N`. One topic per PR.
- Conventional Commits: `feat:`, `fix:`, `chore:`, `docs:`, `test:`,
  `refactor:`, `perf:`, `build:`, `ci:`. Imperative mood, lowercase, no period.
  The body explains why, not what.

## Testing

The math (distribution, calibration, outcome probability, edge ranking) is TDD:
write the failing test against synthetic inputs with known answers first, then
the code. API clients are tested against saved JSON fixtures, never live
endpoints. Keep a golden end-to-end test that turns fixture markets and fixture
forecasts into an expected ranked report.

## Writing style (commits, PRs, docs, comments)

- No em dashes. Use regular hyphens, commas, or parentheses.
- No AI-cliche phrases ("leverage", "robust", "seamless", "comprehensive",
  "elevate", "delve", "in the realm of", "it's worth noting", "moreover",
  "furthermore"). Plain direct English. Short sentences.
- Add a comment only when the why is non-obvious. Do not restate what the code
  does.

## When in doubt

Ask. A 30-second clarifying question beats a 30-minute wrong direction.
