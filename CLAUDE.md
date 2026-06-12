# CLAUDE.md

Orientation for Claude Code sessions in this repo. Read this first.

## What this repo is

rainmaker-bot is an advisory bot for betting on US-city weather markets on
Polymarket. It gathers weather forecasts from multiple free sources, turns them
into a calibrated probability for each market outcome, compares that probability
to the market price, and produces a daily report of bets ranked by edge
(expected value). A human reviews the report and places bets manually.

Status: MVP 1.0 advisory is live for 11 US cities (phases 0-4 done, the Phase 5
city expansion, the TMIN slice, and the monthly-precipitation slice; only the
daily-precipitation form remains). MVP 2.0 is
code-complete: a scheduled GitHub Actions run (every 3h) persists to Supabase Postgres, settles
past markets against NOAA actuals, and writes a daily P&L/calibration snapshot;
the read-only dashboard lives in `dashboard/` (the Vercel + Cloudflare Access
deploy is an operator step). MVP 3.0 (automated trading) has not started.

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

- MVP 1.0: advisory. Done for temperature (11 cities, TMAX and TMIN) and the
  monthly-precipitation slice. Only the daily-binary precipitation form remains
  (its Phase 0 rules are captured; the daily form is just unbuilt).
- MVP 2.0: tracking. Done: Supabase Postgres store, daily scheduled runs,
  NOAA-proxy settlement, P&L/calibration tracking, and the web dashboard.
- MVP 3.0: automated trading via Polymarket's CLOB API. Not started.

The store is dual-backend: SQLite locally and in tests, Supabase Postgres when
`DATABASE_URL` is set (the cloud run). Keep the shared SQL portable; do not add
SQLite-only features. Changes to existing tables go through migrations in
`store/migrate.py`; new tables go in the base schema.

## Phase order

Phase 0 (discovery spike) is a hard gate. Do not build the forecast pipeline
before confirming that live Polymarket US-city weather markets exist and
documenting how they resolve. If they are absent or thin, stop and raise it (for
example, consider Kalshi) rather than building on an unverified premise. See the
spec for the full phase plan.

## Toolchain

Python 3.11+ managed with uv. Commands:

- Install: `uv sync`
- Run: `uv run rainmaker run` (discovers all live US-city markets; `--reports-dir`, `--db`)
- Settle: `uv run rainmaker settle` (record NOAA actuals for past markets)
- Prune: `uv run rainmaker prune` (drop all-but-latest intraday rows per
  (settled market, UTC day) from prices/predictions/forecasts; bounds storage)
- Track: `uv run rainmaker track` (P&L + calibration summary over settled markets)
- Snapshot: `uv run rainmaker snapshot` (upsert the daily metrics row the dashboard reads)
- Backfill: `uv run rainmaker backfill --city <X>` (fit a calibration cell and
  backtest accuracy from history; `--city all` covers every city)
- Backtest: `uv run rainmaker backtest` (forecast calibration and win-rate over
  history; synthetic ladder plus a real closed-market reality check, no P/L)
- Backtest P/L: `uv run rainmaker backtest-pnl` (hypothetical betting P/L over
  closed markets, replayed at several leads against the historical CLOB price;
  `--city`, `--days`, `--leads`, `--reports-dir`)
- Test: `uv run pytest`
- Lint: `uv run ruff check .`  Format: `uv run ruff format .`
- Type check: `uv run mypy src`

Every command uses local SQLite unless `DATABASE_URL` is set to a postgres DSN
(the scheduled GitHub Actions workflow sets it from a repo secret). Runtime deps:
httpx, pydantic, numpy, scipy, psycopg. The dashboard in `dashboard/` is
Next.js; verify it with `npm run build` there. API clients are tested against
saved JSON fixtures in `tests/fixtures/`, never live endpoints.

## Repo layout

```
.claude/
  agents/             role agents: architect, developer, tester, reviewer
  skills/             project skills: /kickoff, /grill-me, /to-issues, /sync-template
  settings.json       project settings; enables the superpowers plugin
src/rainmaker/
  config.py           station registry (11 cities), Target, source config constants
  cli.py              run/settle/prune/track/snapshot/backfill/backtest/backtest-pnl entry points
  backfill.py         NCEI actuals + historical forecasts -> calibration fit; GSOM monthly precip
  backtest.py         forecast calibration + win-rate over history (no P/L)
  pnl_backtest.py     replay closed markets at historical CLOB prices -> betting P/L
  settle.py           settle past markets against NOAA actuals (idempotent catch-up)
  tracking.py         hypothetical P&L + calibration scoring, daily snapshot
  forecasts/
    base.py           ForecastSample, ForecastSet, ForecastSource protocol
    nws.py            NWS fetch + parse
    openmeteo.py      Open-Meteo multi-model and ensemble fetch + parse
    precip.py         precip sourcing (Open-Meteo + NWS QPF + climatology) -> monthly-total moments
    aggregate.py      pool sources, coverage, freshness
  probability/
    distribution.py   pooled samples -> Gaussian (uncalibrated, sigma floor)
    precip_distribution.py  monthly total -> gamma by method of moments (var floor)
    calibration.py    per-(station, variable, lead) bias/spread fit + apply
    outcomes.py       integrate Gaussian over buckets (continuity-corrected)
    precip_outcomes.py  integrate the gamma over inch brackets + precip_settles
  ranking/
    edge.py           evaluate_market / evaluate_precip_market -> edge-ranked outcomes + gates
  report/
    render.py         terminal + markdown/JSON report, recommended-bets summary
  polymarket/
    client.py         Gamma discovery (read-only): temperature + monthly precip markets
    markets.py        event JSON -> Market (target + buckets, ICAO guard)
    precip_markets.py monthly-precip event JSON -> PrecipMonthlyMarket (inch brackets)
    prices.py         CLOB price-history client (read-only) + snap to a timestamp
  kalshi/
    client.py         Kalshi discovery (read-only): daily high/low temp + monthly rain, secondary venue
    markets.py        Kalshi temp ladder -> Market (reuses Bucket/Target; per-variable guard)
    precip_markets.py Kalshi rain ladder -> PrecipMonthlyMarket (reuses the precip path)
  store/
    db.py             dual-backend store (SQLite default, Postgres via DSN)
    migrate.py        forward schema migrations (schema_migrations)
    record.py         persist a run (runs/markets/prices/forecasts/predictions)
    query.py          read-back helpers
    prune.py          drop all-but-latest intraday rows per (settled market, UTC day)
dashboard/            read-only Next.js dashboard (Vercel, behind Cloudflare Access)
.github/workflows/
  daily-run.yml       scheduled cron (every 3h): run -> settle -> prune -> snapshot against Supabase
tests/
  fixtures/           saved API responses (NWS, Open-Meteo, NCEI, Polymarket)
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

### Sizing (t-shirt size per issue)

Every issue carries a t-shirt size label, estimated in human working hours:

- `size:S` - under 1 hour. One focused change.
- `size:M` - 1 to 3 hours. Write a sub-plan first.
- `size:L` - 4 to 6 hours. Split into smaller issues, or break into checkpointed
  sub-plans (below).
- `size:XL` - a full day, about 8 hours. Too big to start as one issue. Split it.

Hours are the yardstick, but the reason to keep issues small is the session: a
large issue risks hitting the session limit mid-task and bloats context until
quality drops. Size the issue when you file it, then re-check while planning. If
the full plan shows the work is bigger than its label, re-label and split rather
than push through.

### Sub-plans (checkpoint before deep work)

Before deep planning or implementation, post a short sub-plan first: a handful of
checkpoint bullets (the approach, the files you expect to touch, the order, the
verification step) on the issue or the draft PR. Cheap insurance: if the session
drops, the next one reads the checkpoint and resumes instead of restarting.
For anything sized `M` or larger, the sub-plan is also where you confirm the
work still fits one session and decompose it if it does not.
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
   Phase 0 (discovery spike) is a hard gate (see above). Check its `size:` label;
   if it is unsized, size it first, and if it is `L` or `XL`, decompose it before
   starting.
3. Post a short sub-plan on the issue (the checkpoint bullets above).
4. Create a branch and open a draft PR linking the issue (`Closes #N`).
5. Expand the sub-plan into a full plan via `superpowers:writing-plans`, saved to
   `docs/plans/<issue-number>-<slug>.md`.
6. Implement with TDD per the plan (see Testing).
7. Run the full check suite (lint, type check, tests including the golden e2e).
   It must pass before requesting review.
8. Mark the PR ready for review.

For a batch of refined, sized issues, `/kickoff` automates this flow per
issue, with the sub-plan comment standing in for step 5's full plan (see
"Agent team").

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

## Agent team

The template ships four role agents in `.claude/agents/` and a `/kickoff`
skill. The lead is the main session: subagents cannot call each other, so the
session running `/kickoff` routes every handoff, and GitHub (sub-plan and
verdict comments, draft PRs, labels) holds the state that makes a dropped
session resumable.

- `architect` - advisory, read-only: sub-plans, split proposals, arbitration.
- `developer` - one issue end to end in an isolated worktree.
- `tester` - independent verification on the branch, read-only.
- `reviewer` - spec pass then quality pass, read-only.

Refine and size issues in discussion first (`/grill-me` stress-tests the
plan, `/to-issues` turns it into sized issues); mark dependencies with a
literal `Blocked by: #N` line in the issue body. Then `/kickoff <issues>`
(user-typed only; it does not auto-trigger) runs unblocked issues in parallel
waves to ready PRs. Under `/kickoff` the sub-plan comment substitutes for the
full plan in `docs/plans/`. Merging stays human and gates the next wave. Caps,
routing, and report contracts live in `.claude/skills/kickoff/SKILL.md` and
the agent files; they are not repeated here.

Labels: `in-progress` (package dispatched; resume, do not restart) and
`needs-human` (parked: question, blocker, or exhausted fix loop), on top of
the sizing set.

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

## What not to do

- Don't push directly to `main`. Open a PR.
- Don't merge a PR that has not run the full check suite (lint, type check,
  tests including the golden e2e).
- Don't bypass git hooks (`--no-verify`). If a hook fails, fix the cause.
- Don't improve `.claude/` machinery only in this repo. Change the template
  (sv-tmueller/claude-template) first, then `/sync-template` it back here;
  local-only edits are overwritten by the next sync.
- Don't introduce a new dependency without saying why in the PR body.

## When in doubt

Ask. A 30-second clarifying question beats a 30-minute wrong direction.
