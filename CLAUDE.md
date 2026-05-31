# CLAUDE.md

Orientation for Claude Code sessions in this repo. Read this first.

## What this repo is

rainmaker-bot is an advisory bot for betting on US-city weather markets on
Polymarket. It gathers weather forecasts from multiple free sources, turns them
into a calibrated probability for each market outcome, compares that probability
to the market price, and produces a daily report of bets ranked by edge
(expected value). A human reviews the report and places bets manually.

Status: Phase 2 complete: the pipeline produces a daily edge-ranked report. Phase 3 (SQLite persistence) is next.

## Working principles

The four principles in `~/.claude/CLAUDE.md` apply here: think before coding,
simplicity first, surgical changes, goal-driven execution. This file adds only
what is specific to this project; it does not repeat them.

## Where decisions live

Read these before proposing changes that touch their area:

- `docs/superpowers/specs/2026-05-29-mvp1-advisory-design.md` - the approved MVP
  1.0 design. Architecture, the forecast/calibration/ranking logic, the data
  model, and the phase plan. This is authoritative until the code exists; once
  code lands, the code wins and this doc gets updated in the same PR.
- `docs/architecture/` - stack and policy decisions, the data model, the domain
  math, extracted from the spec as the code lands. Empty at pre-implementation.
- `docs/operations/` - how to run, deploy, and operate the bot: environments,
  secrets policy, the daily-report runbook. Filled in from Phase 1.
- `docs/plans/` - implementation plans, one per issue as `<issue-number>-<slug>.md`.

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

## Toolchain

Python 3.11+ managed with uv. Commands:

- Install: `uv sync`
- Run: `uv run rainmaker run` (Phase 1: NYC highest-temp; `--city`, `--variable`, `--date` optional)
- Test: `uv run pytest`
- Lint: `uv run ruff check .`  Format: `uv run ruff format .`
- Type check: `uv run mypy src`

Runtime deps: httpx, pydantic. (numpy/scipy/pandas arrive with the Phase 2 probability engine.)
API clients are tested against saved JSON fixtures in `tests/fixtures/`, never live endpoints.

## Repo layout

```
src/rainmaker/
  config.py           station registry, Target, source config constants
  cli.py              `rainmaker run` entry point
  forecasts/
    base.py           ForecastSample, ForecastSet, ForecastSource protocol
    nws.py            NWS fetch + parse
    openmeteo.py      Open-Meteo multi-model and ensemble fetch + parse
    aggregate.py      pool sources, coverage, freshness
  probability/
    distribution.py   pooled samples -> Gaussian (uncalibrated, sigma floor)
    outcomes.py       integrate Gaussian over buckets (continuity-corrected)
  ranking/
    edge.py           evaluate_market -> edge-ranked outcomes + gates
  report/
    render.py         terminal + markdown/JSON report
  polymarket/
    client.py         Gamma discovery (read-only)
    markets.py        event JSON -> Market (target + buckets)
tests/
  fixtures/           saved API responses for KLGA (NWS + Open-Meteo)
  test_*.py           unit and I/O tests (pytest-httpx for mocked HTTP)
docs/
  architecture/       stack and policy decisions, data model, domain math
  operations/         run/deploy/operate: environments, secrets, runbooks
  plans/              implementation plans, <issue-number>-<slug>.md
  superpowers/specs/  approved designs, YYYY-MM-DD-<topic>-design.md
```

The golden end-to-end test (fixture markets and fixture forecasts in, expected
ranked report out) is the safety net for the whole pipeline; keep it green
before any change that touches forecasting, calibration, or ranking.

## How we work

### Issues and branches

- Every unit of work is a GitHub issue first.
- Branch from `main` per issue: `feat/<issue-number>-<short-slug>` or
  `fix/<issue-number>-<short-slug>`. Merge via PR. The PR references the issue
  with `Closes #N`. One topic per PR.

### Sub-plans (checkpoint before deep work)

Before deep planning or implementation, post a short sub-plan first: a handful of
checkpoint bullets (the approach, the files you expect to touch, the order, the
verification step) on the issue or the draft PR. Cheap insurance: if the session
drops, the next one reads the checkpoint and resumes instead of restarting.
Expanding it into a full plan in `docs/plans/` comes later (see "How to pick up a
task").

### Commits

- Conventional Commits: `feat:`, `fix:`, `chore:`, `docs:`, `test:`,
  `refactor:`, `perf:`, `build:`, `ci:`. Imperative mood, lowercase, no period.
  The body explains why, not what.

## How to pick up a task

1. `gh issue list --state open` (add `--label phase:<current>` if you use phase
   labels) to see what is available.
2. Pick an unassigned issue with no unresolved blockers. Respect the phase order:
   Phase 0 (discovery spike) is a hard gate (see above).
3. Post a short sub-plan on the issue (the checkpoint bullets above).
4. Create a branch and open a draft PR linking the issue (`Closes #N`).
5. Expand the sub-plan into a full plan via `superpowers:writing-plans`, saved to
   `docs/plans/<issue-number>-<slug>.md`.
6. Implement with TDD per the plan (see Testing).
7. Run the full check suite (lint, type check, tests including the golden e2e).
   It must pass before requesting review.
8. Mark the PR ready for review.

## Workflow defaults

Standing preferences for this project:

- Effort: maximum. Use deepest reasoning.
- Permission mode: bypass during development (user-controlled).
  <!-- Modes (set with /permissions or settings.json "defaultMode"): default = prompt on
       first use of each tool; acceptEdits = auto-accept edits, prompt other actions;
       plan = read-only; bypassPermissions = no prompts (the mode above). -->
- Superpowers: use relevant skills proactively (brainstorming, writing-plans,
  test-driven-development, subagent-driven-development, executing-plans,
  verification-before-completion).
- Parallel work: fan out subagents for independent research or implementation
  streams. Default to parallel over serial.

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
