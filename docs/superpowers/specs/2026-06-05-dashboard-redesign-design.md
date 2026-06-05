# Dashboard redesign design

Issue: #55. Approved in the 2026-06-05 brainstorm.

## Goal

Turn the dashboard from an unstyled table dump into a personal daily-decision
tool the operator enjoys opening. One page, one daily flow: which bets should I
place, can I trust the forecasts, how am I doing over time. Quiet, dense,
data-first. No AI slop: no gradient heroes, no emoji headings, no heavy-shadow
card grids, no marketing copy, no decoration that competes with the data.

## Decisions from the brainstorm

- Desktop is the primary surface (operator pre-answer on the issue).
- Dark only. One palette to maintain.
- Layout: decision-first grid (option B). Bets own the full width; trust and
  track record share the row below.
- P&L over time is charted with a hand-rolled server-rendered SVG. No chart
  library, no new dependency.
- Per-market detail is dense inline: decision columns prominent, trust
  diagnostics (sigma, source count) in the same row but muted.
- Three content additions: Polymarket link per bet (markets.slug), run health
  line in the header (runs.coverage), recent settled bets list (outcomes join).
- The page stays fully server-rendered. No client JS anywhere.

## Page structure

Max width about 1200px, four zones top to bottom.

1. Header bar. "Rainmaker" wordmark left. Right: run health from the latest
   run: started_at time, ok_sources from runs.coverage, market count. The
   expected source set is {nws, open-meteo} (the names the pipeline writes).
   A missing source is named in amber.
2. KPI strip. From the latest tracking_snapshot: P&L in units (green/red),
   ROI, record (wins-losses), Brier, hit rate with n_scored. Large mono
   numbers, small-caps muted labels. Snapshot date right-aligned, faint.
3. Recommended bets, full width, edge-sorted. Columns: Market (title links to
   the Polymarket event page when slug is present, opens in a new tab), Bucket
   (deg F with deg C conversion, current parse_bucket_label mirror kept),
   Forecast mu (deg F with deg C), P(win), Ask, Edge (green, bold). Then
   muted: sigma and n sources for that market in this run (predictions.confidence is never recorded, so it is not shown).
4. Bottom row, two panels.
   - Left, about 60 percent: forecast accuracy pivot. City rows, one column
     per distinct lead time present in the data (today 1d/2d/3d). Cell: live
     MAE primary, signed bias colored by
     direction, n and backtest MAE muted. TMAX only today; the pivot gains a
     variable dimension when TMIN accuracy lands.
   - Right, about 40 percent: track record. SVG line of total_pnl by
     snapshot_date (total_pnl is already cumulative), dashed zero baseline,
     last value labeled. Below it the last 10 settled bets: settle date,
     market, predicted p_win, W/L, per-bet P&L.

## Visual language

- Palette: page ground #0c0f13, panels #0e1217 with 1px #1f242b hairlines.
  Text levels: primary #e3e6ea, muted #7d8590, faint #586069.
- Color only for meaning: green #3fb950 for positive edge, P&L, wins; red
  #f85149 for losses; amber #d29922 for degraded data (missing source). Bias
  direction: warm orange #e3893d for forecasting too hot, cool blue #6cb6ff
  for too cold. Nothing else is colored.
- Type: Geist Sans for titles and labels, Geist Mono with tabular figures for
  every number. Section labels are small-caps letter-spaced, not big headings.
  Market titles stay in Geist Sans so they scan as words.
- No shadows, no gradients, 4px corner radius, density from hairline row rules.
- Palette lives in app/globals.css as CSS variables, color-scheme: dark.

## Data layer

All reads, one server render per request, force-dynamic kept. Queries run in
parallel where independent:

- Latest run: id, started_at, coverage (JSON: n_markets, ok_sources).
- Bets: predictions (recommended = 1) for the latest run, now also reading
  sigma and n_sources from dist_params (already recorded per prediction);
  prices for ask; markets for title and slug (bounded to the ids on the page).
- Snapshot history: all tracking_snapshot rows ordered by snapshot_date
  ascending. Latest row feeds the KPI strip, the series feeds the chart.
- Accuracy: forecast_accuracy as today, pivoted to city x lead in code.
- Settled bets: the same join tracking.compute_pnl uses (predictions x prices
  x outcomes x markets), last 10 by settled_at. Per-bet P&L mirrors
  compute_pnl: buy one share at ask, won gives 1 - ask, lost gives -ask.
  Outcomes are read newest-first with a fixed limit so reads stay bounded as history grows.

No new tables, no writes, no schema changes.

## Components

All inside dashboard/, all server components:

- lib/supabase.ts: unchanged.
- lib/data.ts: every query above plus row types. page.tsx keeps no query code.
- lib/format.ts: pct, degC, withCelsius, degDelta move here unchanged.
- app/layout.tsx: metadata fixed to "Rainmaker", dark body.
- app/globals.css: palette tokens, color-scheme: dark.
- app/page.tsx: fetch once, compose components.
- components/RunHealth.tsx, KpiStrip.tsx, BetsTable.tsx, AccuracyGrid.tsx,
  PnlChart.tsx, SettledList.tsx: presentational, one zone each.

## Empty and degraded states

Each state is designed, not accidental:

- No runs yet: header says so, bets section shows its empty state.
- No bets pass the gates: "No bets pass the gates today." Quiet normal state.
- Expected source missing from ok_sources: named in amber in the header.
- No snapshots: KPI strip shows dashes; chart and settled list replaced by
  "no settled results yet".
- Fewer than 2 snapshot points: no line chart, numbers still shown.
- Missing slug: market title renders unlinked.
- Unparsable dist_params: blank forecast and sigma cells (current behavior).
- Missing SUPABASE_URL or key: server throws, as today.

## Out of scope

- Mobile layout work beyond not breaking at narrow widths.
- Light theme.
- Any chart library or other new dependency.
- App-level auth (Cloudflare Access stays the gate).
- Backend, store, or pipeline changes of any kind.

## Verification

- npm run build in dashboard/ is the hard gate (type check plus build).
- Visual pass against the dev server with live Supabase data.
- One stubbed-empty-data render to eyeball every empty state.
- uv run pytest stays green (nothing in src/ or tests/ is touched).

Implementation uses the three design skills per the issue: impeccable,
ui-ux-pro-max, frontend-design.
