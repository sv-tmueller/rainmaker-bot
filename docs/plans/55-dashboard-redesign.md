# Dashboard Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild `dashboard/app/page.tsx` into the dark, decision-first dashboard approved in `docs/superpowers/specs/2026-06-05-dashboard-redesign-design.md`.

**Architecture:** One server-rendered Next.js page, zero client JS. A new `lib/data.ts` owns every Supabase read; six presentational server components render one zone each. Palette lives as CSS variables in `globals.css` consumed through Tailwind v4 `@theme` tokens.

**Tech Stack:** Next 16.2.7 (App Router, server components only), Tailwind v4, @supabase/supabase-js, Geist Sans / Geist Mono (already loaded in `layout.tsx`).

**Context for the implementer:**

- Branch `feat/55-dashboard-redesign` exists with the spec committed; draft PR #56 is open. Work on this branch.
- `dashboard/AGENTS.md` warns that this Next version may differ from training data. The plan uses only basic server components, `metadata`, and plain JSX; if anything else is needed, read `dashboard/node_modules/next/dist/docs/` first.
- There is no JS test infra and none is added (issue constraint). The verification gate per task is `npm run build` in `dashboard/`. Visual verification happens in Tasks 11-12.
- Design skills per the issue: read the approved spec before Task 1; use frontend-design sensibilities while writing components (the code below is the approved design); Task 12 runs an impeccable + ui-ux-pro-max polish pass on the rendered page.
- Repo writing style: no em dashes, plain English, comments only where the why is non-obvious.

**Domain facts the code relies on (verified against the Python source):**

- `predictions.dist_params` is JSON: `{"mu": float, "sigma": float, "n_sources": int}` (`store/record.py:116`).
- `runs.coverage` is JSON: `{"n_markets": int, "ok_sources": [str]}` (`store/record.py:42`).
- Forecast source names are exactly `nws` and `open-meteo` (`forecasts/nws.py:51`, `forecasts/openmeteo.py:128`).
- `markets.slug` is the Polymarket event slug (`polymarket/markets.py:127`); the event URL is `https://polymarket.com/event/<slug>`.
- `outcomes.won` is never populated. Win/loss is derived: parse the bucket label (`polymarket/markets.py:17`), round the actual half-to-even (Python `round`), compare (`tracking.py:_won`). Per-bet P&L: buy one share at ask; won gives `1 - ask`, lost gives `-ask` (`tracking.py:compute_pnl`). A market re-recommended across runs counts as separate bets.
- `tracking_snapshot.total_pnl` is cumulative; plotting it by `snapshot_date` is the P&L curve.
- `forecast_accuracy` is TMAX-only today; the pivot keys on (city, lead) and gains a variable dimension when TMIN lands.

---

### Task 1: Design tokens and page shell

**Files:**
- Modify: `dashboard/app/globals.css`
- Modify: `dashboard/app/layout.tsx`

- [ ] **Step 1: Replace `dashboard/app/globals.css` with the dark token set**

```css
@import "tailwindcss";

:root {
  --background: #0c0f13;
  --foreground: #e3e6ea;
  --panel: #0e1217;
  --line: #1f242b;
  --line-soft: #1a1f26;
  --muted: #7d8590;
  --faint: #586069;
  --pos: #3fb950;
  --neg: #f85149;
  --warn: #d29922;
  --warm: #e3893d;
  --cool: #6cb6ff;
}

@theme inline {
  --color-background: var(--background);
  --color-foreground: var(--foreground);
  --color-panel: var(--panel);
  --color-line: var(--line);
  --color-line-soft: var(--line-soft);
  --color-muted: var(--muted);
  --color-faint: var(--faint);
  --color-pos: var(--pos);
  --color-neg: var(--neg);
  --color-warn: var(--warn);
  --color-warm: var(--warm);
  --color-cool: var(--cool);
  --font-sans: var(--font-geist-sans);
  --font-mono: var(--font-geist-mono);
}

html {
  color-scheme: dark;
}

body {
  background: var(--background);
  color: var(--foreground);
  font-family: var(--font-sans), system-ui, sans-serif;
}
```

This deletes the light palette and the `prefers-color-scheme` block (dark only per spec) and replaces the Arial fallback with Geist.

- [ ] **Step 2: Fix the metadata in `dashboard/app/layout.tsx`**

Replace the `metadata` export only; fonts and structure stay:

```tsx
export const metadata: Metadata = {
  title: "Rainmaker",
  description: "Daily weather-market betting advisory",
};
```

- [ ] **Step 3: Build**

Run: `cd dashboard && npm run build`
Expected: exit 0, "Compiled successfully".

- [ ] **Step 4: Commit**

```bash
git add dashboard/app/globals.css dashboard/app/layout.tsx
git commit -m "feat: dark design tokens and page metadata for the dashboard"
```

---

### Task 2: Shared formatting helpers

**Files:**
- Create: `dashboard/lib/format.ts`
- Modify: `dashboard/app/page.tsx` (imports only)

- [ ] **Step 1: Create `dashboard/lib/format.ts`**

`pct`, `degC`, `withCelsius` move verbatim from `page.tsx`; `signed` is new. `degDelta` and `accCell` are NOT moved: they die with the old accuracy table in Task 10.

```ts
export function pct(x: number) {
  return `${(x * 100).toFixed(0)}%`;
}

export function degC(f: number) {
  return (((f - 32) * 5) / 9).toFixed(1);
}

// Mirrors parse_bucket_label in src/rainmaker/polymarket/markets.py.
export function withCelsius(label: string): string {
  const lowered = label.toLowerCase();
  if (lowered.includes("below") || lowered.includes("higher") || lowered.includes("above")) {
    const m = label.match(/-?\d+/);
    if (!m) return label;
    const op = lowered.includes("below") ? "<=" : ">=";
    return `${label} (${op} ${degC(+m[0])}°C)`;
  }
  const m = label.match(/(-?\d+)\s*-\s*(-?\d+)/);
  if (!m) return label;
  return `${label} (${degC(+m[1])}-${degC(+m[2])}°C)`;
}

export function signed(x: number, digits = 2) {
  return `${x >= 0 ? "+" : ""}${x.toFixed(digits)}`;
}
```

- [ ] **Step 2: Point `page.tsx` at the new module**

In `dashboard/app/page.tsx`: delete the local `pct`, `degC`, `withCelsius` function definitions and add at the top:

```tsx
import { degC, pct, withCelsius } from "../lib/format";
```

Keep the local `degDelta` and `accCell` for now (still used by the old accuracy table until Task 10).

- [ ] **Step 3: Build**

Run: `cd dashboard && npm run build`
Expected: exit 0.

- [ ] **Step 4: Commit**

```bash
git add dashboard/lib/format.ts dashboard/app/page.tsx
git commit -m "refactor: move dashboard formatting helpers to lib/format"
```

---

### Task 3: Data layer

**Files:**
- Create: `dashboard/lib/data.ts`
- Modify: `docs/superpowers/specs/2026-06-05-dashboard-redesign-design.md` (one line)

- [ ] **Step 1: Correct the spec**

Implementation found `n_sources` already recorded in `dist_params`, so no forecasts query is needed. In the spec's Data layer section, replace:

```
  sigma from dist_params and confidence; prices for ask; markets for title and
  slug; forecasts grouped by market_id for distinct source counts.
```

with:

```
  sigma and n_sources from dist_params (already recorded per prediction) and
  confidence; prices for ask; markets for title and slug.
```

- [ ] **Step 2: Create `dashboard/lib/data.ts`**

```ts
import { serverClient } from "./supabase";

export type Bet = {
  title: string;
  slug: string | null;
  bucket: string;
  mu: number | null;
  sigma: number | null;
  nSources: number | null;
  confidence: number | null;
  pWin: number;
  ask: number;
  edge: number;
};

export type RunInfo = {
  startedAt: string;
  okSources: string[] | null;
  nMarkets: number | null;
};

export type Snapshot = {
  date: string;
  nBets: number;
  wins: number;
  losses: number;
  totalPnl: number;
  roi: number;
  brier: number | null;
  hitRate: number | null;
  nScored: number;
};

export type AccCell = { n: number; mae: number; bias: number };
export type AccSlot = { live: AccCell | null; backtest: AccCell | null };
export type AccRow = { city: string; cells: Record<number, AccSlot> };
export type Accuracy = { leads: number[]; rows: AccRow[] };

export type SettledBet = {
  date: string;
  title: string;
  pWin: number;
  won: boolean;
  pnl: number;
};

// Python round() is half-to-even, Math.round is half-up. NOAA actuals can land
// exactly on .5°F (7.5°C is 45.5°F), so mirror the settlement math exactly.
function roundHalfEven(x: number): number {
  const f = Math.floor(x);
  if (x - f === 0.5) return f % 2 === 0 ? f : f + 1;
  return Math.round(x);
}

// Mirrors _won in src/rainmaker/tracking.py (parse_bucket_label + comparison).
// Returns null for an unparsable label so the row is skipped, not miscounted.
function wonBucket(label: string, actual: number): boolean | null {
  const lowered = label.toLowerCase();
  const v = roundHalfEven(actual);
  const t = label.match(/-?\d+/);
  if (lowered.includes("below")) return t ? v <= +t[0] : null;
  if (lowered.includes("higher") || lowered.includes("above")) return t ? v >= +t[0] : null;
  const m = label.match(/(-?\d+)\s*-\s*(-?\d+)/);
  if (!m || +m[1] > +m[2]) return null;
  return +m[1] <= v && v <= +m[2];
}

export async function getDashboardData() {
  const db = serverClient();

  const [runsQ, marketsQ, snapsQ, accQ, outcomesQ] = await Promise.all([
    db.from("runs").select("id, started_at, coverage").order("started_at", { ascending: false }).limit(1),
    db.from("markets").select("id, title, slug, settlement_date"),
    db.from("tracking_snapshot").select("*").order("snapshot_date", { ascending: true }),
    db.from("forecast_accuracy").select("city, lead_time, kind, n, mae_f, bias_f").order("city").order("lead_time"),
    db.from("outcomes").select("market_id, actual_value, settled_at"),
  ]);

  const titleOf = new Map((marketsQ.data ?? []).map((m) => [m.id, m.title as string]));
  const slugOf = new Map((marketsQ.data ?? []).map((m) => [m.id, (m.slug as string | null) ?? null]));
  const settleDateOf = new Map(
    (marketsQ.data ?? []).map((m) => [m.id, (m.settlement_date as string | null) ?? null]),
  );

  let run: RunInfo | null = null;
  const runRow = runsQ.data?.[0];
  if (runRow) {
    let okSources: string[] | null = null;
    let nMarkets: number | null = null;
    try {
      const cov = JSON.parse(runRow.coverage as string);
      if (Array.isArray(cov?.ok_sources)) okSources = cov.ok_sources;
      if (typeof cov?.n_markets === "number") nMarkets = cov.n_markets;
    } catch {
      // unparsable coverage -> timestamp-only health line
    }
    run = { startedAt: runRow.started_at as string, okSources, nMarkets };
  }

  let bets: Bet[] = [];
  if (runRow) {
    const [{ data: preds }, { data: prices }] = await Promise.all([
      db
        .from("predictions")
        .select("market_id, bucket, p_win, edge, confidence, dist_params")
        .eq("run_id", runRow.id)
        .eq("recommended", 1),
      db.from("prices").select("market_id, outcome, price").eq("run_id", runRow.id),
    ]);
    const askOf = new Map((prices ?? []).map((p) => [`${p.market_id}|${p.outcome}`, p.price as number]));
    bets = (preds ?? [])
      .map((p) => {
        let mu: number | null = null;
        let sigma: number | null = null;
        let nSources: number | null = null;
        try {
          const d = JSON.parse(p.dist_params as string);
          if (typeof d?.mu === "number") mu = d.mu;
          if (typeof d?.sigma === "number") sigma = d.sigma;
          if (typeof d?.n_sources === "number") nSources = d.n_sources;
        } catch {
          // no parsable dist_params -> blank forecast cells
        }
        return {
          title: titleOf.get(p.market_id) ?? (p.market_id as string),
          slug: slugOf.get(p.market_id) ?? null,
          bucket: p.bucket as string,
          mu,
          sigma,
          nSources,
          confidence: (p.confidence as number | null) ?? null,
          pWin: p.p_win as number,
          ask: askOf.get(`${p.market_id}|${p.bucket}`) ?? 0,
          edge: p.edge as number,
        };
      })
      .sort((a, b) => b.edge - a.edge);
  }

  const snapshots: Snapshot[] = (snapsQ.data ?? []).map((s) => ({
    date: s.snapshot_date as string,
    nBets: s.n_bets as number,
    wins: s.wins as number,
    losses: s.losses as number,
    totalPnl: s.total_pnl as number,
    roi: s.roi as number,
    brier: s.brier as number | null,
    hitRate: s.hit_rate as number | null,
    nScored: s.n_scored as number,
  }));

  // TMAX-only today; add variable to the key when TMIN accuracy lands.
  const accMap = new Map<string, AccRow>();
  const leadSet = new Set<number>();
  for (const r of accQ.data ?? []) {
    const city = r.city as string;
    const lead = r.lead_time as number;
    leadSet.add(lead);
    const row = accMap.get(city) ?? { city, cells: {} };
    const cell = { n: r.n as number, mae: r.mae_f as number, bias: r.bias_f as number };
    const slot = row.cells[lead] ?? { live: null, backtest: null };
    if (r.kind === "backtest") slot.backtest = cell;
    else slot.live = cell;
    row.cells[lead] = slot;
    accMap.set(city, row);
  }
  const accuracy: Accuracy = {
    leads: [...leadSet].sort((a, b) => a - b),
    rows: [...accMap.values()].sort((a, b) => a.city.localeCompare(b.city)),
  };

  // Mirrors tracking.compute_pnl: each recommended prediction with a price on a
  // settled market is one one-unit bet; re-recommendations are separate bets.
  let settled: SettledBet[] = [];
  const outcomes = outcomesQ.data ?? [];
  if (outcomes.length > 0) {
    // Bound the .in() filters: the list shows 10 bets, so the 30 most recently
    // settled markets are plenty and the query URL stays short as outcomes grow.
    const settledIds = [...outcomes]
      .sort((a, b) => ((a.settled_at as string) < (b.settled_at as string) ? 1 : -1))
      .slice(0, 30)
      .map((o) => o.market_id);
    const [{ data: betPreds }, { data: betPrices }] = await Promise.all([
      db
        .from("predictions")
        .select("market_id, run_id, bucket, p_win")
        .eq("recommended", 1)
        .not("bucket", "is", null)
        .in("market_id", settledIds),
      db.from("prices").select("run_id, market_id, outcome, price").in("market_id", settledIds),
    ]);
    const priceOf = new Map(
      (betPrices ?? []).map((p) => [`${p.run_id}|${p.market_id}|${p.outcome}`, p.price as number]),
    );
    const outcomeOf = new Map(outcomes.map((o) => [o.market_id, o]));
    settled = (betPreds ?? [])
      .flatMap((p) => {
        const o = outcomeOf.get(p.market_id);
        const ask = priceOf.get(`${p.run_id}|${p.market_id}|${p.bucket}`);
        if (!o || ask === undefined) return [];
        const won = wonBucket(p.bucket as string, o.actual_value as number);
        if (won === null) return [];
        return [
          {
            settledAt: o.settled_at as string,
            date: (settleDateOf.get(p.market_id) ?? (o.settled_at as string)).slice(0, 10),
            title: titleOf.get(p.market_id) ?? (p.market_id as string),
            pWin: p.p_win as number,
            won,
            pnl: won ? 1 - ask : -ask,
          },
        ];
      })
      .sort((a, b) => (a.settledAt < b.settledAt ? 1 : -1))
      .slice(0, 10)
      .map(({ settledAt: _settledAt, ...rest }) => rest);
  }

  return { run, bets, snapshots, accuracy, settled };
}
```

- [ ] **Step 3: Build**

Run: `cd dashboard && npm run build`
Expected: exit 0. `getDashboardData` is unused so far; that is fine.

- [ ] **Step 4: Commit**

```bash
git add dashboard/lib/data.ts docs/superpowers/specs/2026-06-05-dashboard-redesign-design.md
git commit -m "feat: dashboard data layer for the redesigned page

n_sources comes from dist_params, which already records it per
prediction; the spec's forecasts-table query is unnecessary and the
spec is corrected in this commit. W/L derivation mirrors tracking._won
including half-to-even rounding."
```

---

### Task 4: RunHealth component

**Files:**
- Create: `dashboard/components/RunHealth.tsx`

- [ ] **Step 1: Create `dashboard/components/RunHealth.tsx`**

```tsx
import type { RunInfo } from "../lib/data";

// The source names the pipeline writes (forecasts/nws.py, forecasts/openmeteo.py).
const EXPECTED_SOURCES = ["nws", "open-meteo"];

export function RunHealth({ run }: { run: RunInfo | null }) {
  if (!run) {
    return <span className="font-mono text-[11px] text-faint">no runs yet</span>;
  }
  const ok = run.okSources ?? [];
  const missing = run.okSources === null ? [] : EXPECTED_SOURCES.filter((s) => !ok.includes(s));
  return (
    <span className="font-mono text-[11px] text-muted">
      run {run.startedAt.slice(0, 16).replace("T", " ")} UTC
      {(ok.length > 0 || missing.length > 0) && <span className="mx-2 text-faint">·</span>}
      {ok.map((s) => (
        <span key={s} className="mr-2">
          {s} <span className="text-pos">✓</span>
        </span>
      ))}
      {missing.map((s) => (
        <span key={s} className="mr-2 text-warn">
          {s} missing
        </span>
      ))}
      {run.nMarkets !== null && (
        <>
          <span className="mx-2 text-faint">·</span>
          {run.nMarkets} markets
        </>
      )}
    </span>
  );
}
```

- [ ] **Step 2: Build**

Run: `cd dashboard && npm run build`
Expected: exit 0.

- [ ] **Step 3: Commit**

```bash
git add dashboard/components/RunHealth.tsx
git commit -m "feat: run health line for the dashboard header"
```

---

### Task 5: KpiStrip component

**Files:**
- Create: `dashboard/components/KpiStrip.tsx`

- [ ] **Step 1: Create `dashboard/components/KpiStrip.tsx`**

```tsx
import type { Snapshot } from "../lib/data";
import { pct, signed } from "../lib/format";

function Kpi({
  label,
  value,
  tone,
  sub,
}: {
  label: string;
  value: string;
  tone?: "pos" | "neg";
  sub?: string;
}) {
  const toneClass = tone === "pos" ? "text-pos" : tone === "neg" ? "text-neg" : "";
  return (
    <div>
      <div className="text-[10px] uppercase tracking-[0.1em] text-muted">{label}</div>
      <div className={`font-mono text-[22px] font-semibold ${toneClass}`}>
        {value}
        {sub && <span className="ml-1 text-xs font-normal text-faint">{sub}</span>}
      </div>
    </div>
  );
}

export function KpiStrip({ snap }: { snap: Snapshot | null }) {
  const has = snap !== null && snap.nBets > 0;
  const tone = (x: number): "pos" | "neg" => (x >= 0 ? "pos" : "neg");
  return (
    <div className="flex items-baseline gap-12 py-5">
      <Kpi
        label="P&L"
        value={has ? `${signed(snap.totalPnl)}u` : "–"}
        tone={has ? tone(snap.totalPnl) : undefined}
      />
      <Kpi
        label="ROI"
        value={has ? `${signed(snap.roi * 100, 1)}%` : "–"}
        tone={has ? tone(snap.roi) : undefined}
      />
      <Kpi label="Record" value={has ? `${snap.wins}-${snap.losses}` : "–"} />
      <Kpi label="Brier" value={has && snap.brier !== null ? snap.brier.toFixed(3) : "–"} />
      <Kpi
        label="Hit rate"
        value={has && snap.hitRate !== null ? pct(snap.hitRate) : "–"}
        sub={has ? `n${snap.nScored}` : undefined}
      />
      {snap && (
        <div className="ml-auto font-mono text-[11px] text-faint">snapshot {snap.date}</div>
      )}
    </div>
  );
}
```

- [ ] **Step 2: Build**

Run: `cd dashboard && npm run build`
Expected: exit 0.

- [ ] **Step 3: Commit**

```bash
git add dashboard/components/KpiStrip.tsx
git commit -m "feat: KPI strip for the dashboard"
```

---

### Task 6: BetsTable component

**Files:**
- Create: `dashboard/components/BetsTable.tsx`

- [ ] **Step 1: Create `dashboard/components/BetsTable.tsx`**

Decision columns prominent; sigma, confidence, source count muted (`text-faint`). Edge is always positive on recommended rows (the gates require it), so it is always green.

```tsx
import type { Bet } from "../lib/data";
import { degC, pct, signed, withCelsius } from "../lib/format";

export function BetsTable({ bets }: { bets: Bet[] }) {
  return (
    <section className="rounded border border-line bg-panel px-4 py-4">
      <div className="text-[10px] uppercase tracking-[0.1em] text-muted">
        Recommended bets{" "}
        <span className="normal-case tracking-normal text-faint">· edge-sorted</span>
      </div>
      {bets.length === 0 ? (
        <p className="mt-3 text-sm text-muted">No bets pass the gates today.</p>
      ) : (
        <table className="mt-2.5 w-full border-collapse text-sm">
          <thead>
            <tr className="text-left text-[10px] uppercase tracking-[0.08em] text-faint">
              <th className="py-1 font-medium">Market</th>
              <th className="font-medium">Bucket</th>
              <th className="text-right font-medium">Forecast</th>
              <th className="text-right font-medium">P(win)</th>
              <th className="text-right font-medium">Ask</th>
              <th className="text-right font-medium">Edge</th>
              <th className="pl-7 text-right font-medium">σ</th>
              <th className="text-right font-medium">Conf</th>
              <th className="text-right font-medium">Src</th>
            </tr>
          </thead>
          <tbody className="font-mono text-xs">
            {bets.map((b, i) => (
              <tr key={i} className="border-t border-line-soft">
                <td className="py-1.5 font-sans text-[13px]">
                  {b.slug ? (
                    <a
                      href={`https://polymarket.com/event/${b.slug}`}
                      target="_blank"
                      rel="noreferrer"
                      className="hover:underline"
                    >
                      {b.title} <span className="text-[10px] text-faint">↗</span>
                    </a>
                  ) : (
                    b.title
                  )}
                </td>
                <td className="text-foreground/80">{withCelsius(b.bucket)}</td>
                <td className="text-right">
                  {b.mu === null ? (
                    ""
                  ) : (
                    <>
                      {b.mu.toFixed(1)}° <span className="text-faint">{degC(b.mu)}C</span>
                    </>
                  )}
                </td>
                <td className="text-right">{pct(b.pWin)}</td>
                <td className="text-right">{b.ask.toFixed(2)}</td>
                <td className="text-right font-semibold text-pos">{signed(b.edge)}</td>
                <td className="pl-7 text-right text-faint">
                  {b.sigma === null ? "" : b.sigma.toFixed(1)}
                </td>
                <td className="text-right text-faint">
                  {b.confidence === null ? "" : b.confidence.toFixed(2)}
                </td>
                <td className="text-right text-faint">{b.nSources ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
```

- [ ] **Step 2: Build**

Run: `cd dashboard && npm run build`
Expected: exit 0.

- [ ] **Step 3: Commit**

```bash
git add dashboard/components/BetsTable.tsx
git commit -m "feat: edge-sorted bets table with muted trust diagnostics"
```

---

### Task 7: AccuracyGrid component

**Files:**
- Create: `dashboard/components/AccuracyGrid.tsx`

- [ ] **Step 1: Create `dashboard/components/AccuracyGrid.tsx`**

City rows, one column per lead time present in the data. Live MAE primary; bias colored warm (forecasting too hot) or cool (too cold); n and backtest MAE faint.

```tsx
import type { AccSlot, Accuracy } from "../lib/data";
import { signed } from "../lib/format";

function Cell({ slot }: { slot: AccSlot | undefined }) {
  if (!slot || (!slot.live && !slot.backtest)) {
    return <td className="text-right text-faint">–</td>;
  }
  const { live, backtest } = slot;
  if (!live) {
    return (
      <td className="text-right text-faint">
        bt {backtest!.mae.toFixed(1)}° n{backtest!.n}
      </td>
    );
  }
  return (
    <td className="text-right">
      {live.mae.toFixed(1)}°{" "}
      <span className={live.bias >= 0 ? "text-warm" : "text-cool"}>{signed(live.bias, 1)}</span>{" "}
      <span className="text-faint">
        n{live.n}
        {backtest ? ` · bt ${backtest.mae.toFixed(1)}°` : ""}
      </span>
    </td>
  );
}

export function AccuracyGrid({ accuracy }: { accuracy: Accuracy }) {
  return (
    <section className="rounded border border-line bg-panel px-4 py-4">
      <div className="text-[10px] uppercase tracking-[0.1em] text-muted">
        Forecast accuracy{" "}
        <span className="normal-case tracking-normal text-faint">
          · live MAE, bias, n · bt = backtest
        </span>
      </div>
      {accuracy.rows.length === 0 ? (
        <p className="mt-3 text-sm text-muted">No accuracy data yet.</p>
      ) : (
        <table className="mt-2.5 w-full border-collapse font-mono text-xs">
          <thead>
            <tr className="text-left font-sans text-[10px] uppercase tracking-[0.08em] text-faint">
              <th className="py-1 font-medium">City</th>
              {accuracy.leads.map((l) => (
                <th key={l} className="text-right font-medium">
                  {l}d
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {accuracy.rows.map((row) => (
              <tr key={row.city} className="border-t border-line-soft">
                <td className="py-1.5 font-sans text-[13px]">{row.city}</td>
                {accuracy.leads.map((l) => (
                  <Cell key={l} slot={row.cells[l]} />
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
```

- [ ] **Step 2: Build**

Run: `cd dashboard && npm run build`
Expected: exit 0.

- [ ] **Step 3: Commit**

```bash
git add dashboard/components/AccuracyGrid.tsx
git commit -m "feat: forecast accuracy pivot, city by lead time"
```

---

### Task 8: PnlChart component

**Files:**
- Create: `dashboard/components/PnlChart.tsx`

- [ ] **Step 1: Create `dashboard/components/PnlChart.tsx`**

Server-rendered SVG, no library. Renders nothing with fewer than 2 points (spec: no line to draw).

```tsx
import type { Snapshot } from "../lib/data";
import { signed } from "../lib/format";

export function PnlChart({ snapshots }: { snapshots: Snapshot[] }) {
  if (snapshots.length < 2) return null;
  const W = 600;
  const H = 150;
  const PAD = 8;
  const BOTTOM = 18;
  const pnls = snapshots.map((s) => s.totalPnl);
  const hi = Math.max(0, ...pnls);
  const lo = Math.min(0, ...pnls);
  const span = hi - lo || 1;
  const x = (i: number) => PAD + (i * (W - 2 * PAD)) / (snapshots.length - 1);
  const y = (v: number) => PAD + ((hi - v) * (H - PAD - BOTTOM)) / span;
  const points = snapshots.map((s, i) => `${x(i).toFixed(1)},${y(s.totalPnl).toFixed(1)}`).join(" ");
  const last = snapshots[snapshots.length - 1];
  const toneClass = last.totalPnl >= 0 ? "text-pos" : "text-neg";
  const fillClass = last.totalPnl >= 0 ? "fill-pos" : "fill-neg";
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="block w-full" role="img" aria-label="P&L over time">
      <line
        x1={PAD}
        y1={y(0)}
        x2={W - PAD}
        y2={y(0)}
        className="text-line"
        stroke="currentColor"
        strokeDasharray="3 4"
      />
      <polyline
        points={points}
        fill="none"
        className={toneClass}
        stroke="currentColor"
        strokeWidth="1.5"
      />
      <circle
        cx={x(snapshots.length - 1)}
        cy={y(last.totalPnl)}
        r="2.5"
        className={toneClass}
        fill="currentColor"
      />
      <text
        x={W - PAD}
        y={Math.max(10, y(last.totalPnl) - 7)}
        textAnchor="end"
        className={`font-mono text-[11px] ${fillClass}`}
      >
        {signed(last.totalPnl, 1)}u
      </text>
      <text x={PAD} y={H - 4} className="fill-faint font-mono text-[10px]">
        {snapshots[0].date}
      </text>
      <text x={W - PAD} y={H - 4} textAnchor="end" className="fill-faint font-mono text-[10px]">
        {last.date}
      </text>
    </svg>
  );
}
```

- [ ] **Step 2: Build**

Run: `cd dashboard && npm run build`
Expected: exit 0.

- [ ] **Step 3: Commit**

```bash
git add dashboard/components/PnlChart.tsx
git commit -m "feat: hand-rolled SVG P&L chart"
```

---

### Task 9: SettledList component

**Files:**
- Create: `dashboard/components/SettledList.tsx`

- [ ] **Step 1: Create `dashboard/components/SettledList.tsx`**

```tsx
import type { SettledBet } from "../lib/data";
import { pct, signed } from "../lib/format";

export function SettledList({ settled }: { settled: SettledBet[] }) {
  if (settled.length === 0) return null;
  return (
    <table className="w-full border-collapse font-mono text-xs">
      <tbody>
        {settled.map((s, i) => (
          <tr key={i} className="border-t border-line-soft">
            <td className="py-1 pr-2 text-faint">{s.date.slice(5)}</td>
            <td className="font-sans text-[13px]">{s.title}</td>
            <td className="text-right text-muted">{pct(s.pWin)}</td>
            <td className={`text-right ${s.won ? "text-pos" : "text-neg"}`}>
              {s.won ? "W" : "L"}
            </td>
            <td className={`text-right ${s.won ? "text-pos" : "text-neg"}`}>{signed(s.pnl)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
```

- [ ] **Step 2: Build**

Run: `cd dashboard && npm run build`
Expected: exit 0.

- [ ] **Step 3: Commit**

```bash
git add dashboard/components/SettledList.tsx
git commit -m "feat: recent settled bets list"
```

---

### Task 10: Compose the page

**Files:**
- Modify: `dashboard/app/page.tsx` (full rewrite)

- [ ] **Step 1: Replace `dashboard/app/page.tsx` entirely**

This deletes the old `getData`, `degDelta`, `accCell`, and the three old sections. Everything they did now lives in `lib/data.ts` and the components.

```tsx
import { AccuracyGrid } from "../components/AccuracyGrid";
import { BetsTable } from "../components/BetsTable";
import { KpiStrip } from "../components/KpiStrip";
import { PnlChart } from "../components/PnlChart";
import { RunHealth } from "../components/RunHealth";
import { SettledList } from "../components/SettledList";
import { getDashboardData } from "../lib/data";

export const dynamic = "force-dynamic"; // always read live data, never prerender

export default async function Page() {
  const { run, bets, snapshots, accuracy, settled } = await getDashboardData();
  const snap = snapshots.length > 0 ? snapshots[snapshots.length - 1] : null;
  return (
    <main className="mx-auto w-full max-w-[1200px] px-9 py-7">
      <header className="flex items-baseline justify-between border-b border-line pb-3.5">
        <h1 className="text-[15px] font-semibold tracking-tight">Rainmaker</h1>
        <RunHealth run={run} />
      </header>

      <KpiStrip snap={snap} />

      <BetsTable bets={bets} />

      <div className="mt-3.5 grid grid-cols-5 gap-3.5">
        <div className="col-span-3">
          <AccuracyGrid accuracy={accuracy} />
        </div>
        <section className="col-span-2 rounded border border-line bg-panel px-4 py-4">
          <div className="text-[10px] uppercase tracking-[0.1em] text-muted">Track record</div>
          {snapshots.length === 0 ? (
            <p className="mt-3 text-sm text-muted">No settled results yet.</p>
          ) : (
            <>
              <div className="mt-2.5">
                <PnlChart snapshots={snapshots} />
              </div>
              {settled.length > 0 && (
                <>
                  <div className="mt-3 text-[10px] uppercase tracking-[0.1em] text-muted">
                    Recent settled
                  </div>
                  <div className="mt-1.5">
                    <SettledList settled={settled} />
                  </div>
                </>
              )}
            </>
          )}
        </section>
      </div>
    </main>
  );
}
```

- [ ] **Step 2: Build**

Run: `cd dashboard && npm run build`
Expected: exit 0, no unused-import or type errors.

- [ ] **Step 3: Commit**

```bash
git add dashboard/app/page.tsx
git commit -m "feat: compose the redesigned dashboard page

Closes out the decision-first grid: header with run health, KPI strip,
edge-sorted bets, accuracy pivot, P&L chart with recent settled bets."
```

---

### Task 11: Empty states and live visual pass

**Files:**
- Temporary (not committed): `dashboard/app/page.tsx`

- [ ] **Step 1: Check env**

Run: `ls dashboard/.env.local`
If missing, STOP and ask the operator for `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` (see `dashboard/.env.example`). Do not guess or reuse keys from elsewhere.

- [ ] **Step 2: Eyeball every empty state with stubbed data**

In `dashboard/app/page.tsx`, temporarily replace the line

```tsx
  const { run, bets, snapshots, accuracy, settled } = await getDashboardData();
```

with

```tsx
  const { run, bets, snapshots, accuracy, settled } = {
    run: null,
    bets: [],
    snapshots: [],
    accuracy: { leads: [], rows: [] },
    settled: [],
  } as Awaited<ReturnType<typeof getDashboardData>>;
```

Run: `cd dashboard && npm run dev`, open http://localhost:3000.
Expected, per the spec's empty-states section: "no runs yet" in the header, dashes in the KPI strip, "No bets pass the gates today.", "No accuracy data yet.", "No settled results yet." No crashes, no blank page.

- [ ] **Step 3: Revert the stub**

Run: `git restore dashboard/app/page.tsx`
Then confirm: `git status` shows a clean tree.

- [ ] **Step 4: Visual pass with live data**

Run: `cd dashboard && npm run dev`, open http://localhost:3000.
Check against the spec: dark page, four zones, edge-sorted bets with working Polymarket links (click one), accuracy pivot populated, P&L line with zero baseline, settled list showing W/L. Fix anything broken; if code changes, run `npm run build` and commit with a `fix:` message describing what was wrong.

---

### Task 12: Polish pass and finish

**Files:**
- Possibly modify: any `dashboard/` file touched above

- [ ] **Step 1: Design-skill review**

With the dev server running, invoke the impeccable skill on the page (audit/polish mode) and ui-ux-pro-max for a review pass. Apply only changes consistent with the approved spec: spacing, alignment, contrast, hierarchy. No new sections, no new dependencies, no layout changes.

- [ ] **Step 2: Gates**

Run, from the repo root:

```bash
cd dashboard && npm run build && cd ..
uv run pytest
uv run ruff check .
```

Expected: build exit 0; pytest all green (nothing in `src/` or `tests/` was touched); ruff clean.

- [ ] **Step 3: Commit any polish changes**

```bash
git add dashboard/
git commit -m "polish: spacing, alignment, and contrast tuning from design review"
```

Skip if the review produced no changes.

- [ ] **Step 4: Push and mark the PR ready**

```bash
git push
gh pr ready 56
```

Then comment on PR #56 summarizing what was verified (build, empty states, live visual pass, links).
