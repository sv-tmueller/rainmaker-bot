# Celsius alongside Fahrenheit on the dashboard - implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show Celsius next to Fahrenheit in the dashboard's bet rows: converted bucket labels plus a new Forecast column with the bot's mu in both units.

**Architecture:** Display-only, one file (`dashboard/app/page.tsx`). A `withCelsius` helper mirrors the label grammar of `parse_bucket_label` (src/rainmaker/polymarket/markets.py): a below/higher/above keyword with a signed threshold, else a signed range. Absolute conversion C = (F - 32) * 5/9 (distinct from the existing `degDelta` helper, which converts error deltas by 5/9 only). The bets query additionally selects `dist_params`; mu is parsed in the server component.

**Tech Stack:** Next.js server component, TypeScript. No new dependencies. No JS test infra exists and none is added; `npm run build`'s type check is the gate.

Spec: `docs/superpowers/specs/2026-06-04-accuracy-visibility-and-display-design.md`
Issue: #43

**Branch note:** `feat/43-celsius-display` is stacked on `feat/42-forecast-accuracy`
(both edit `page.tsx`); the PR base is the #42 branch and retargets to main when
PR #45 merges.

---

### Task 1: Celsius bucket labels and the Forecast column

**Files:**
- Modify: `dashboard/app/page.tsx`

Heed `dashboard/AGENTS.md`: check `node_modules/next/dist/docs/` if anything
surprises you. This change stays inside the existing server-component pattern.

- [ ] **Step 1: Add the helpers**

Next to the existing `pct`/`degDelta` helpers:

```tsx
function degC(f: number) {
  return (((f - 32) * 5) / 9).toFixed(1);
}

// Mirrors parse_bucket_label in src/rainmaker/polymarket/markets.py.
function withCelsius(label: string): string {
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
```

An unrecognized label renders unchanged (no °C suffix) rather than breaking
the page.

- [ ] **Step 2: Carry mu through the bets query**

The `Bet` type gains a nullable forecast:

```tsx
type Bet = {
  title: string;
  bucket: string;
  pWin: number;
  ask: number;
  edge: number;
  forecastF: number | null;
};
```

The predictions select adds `dist_params`:

```tsx
      db
        .from("predictions")
        .select("market_id, bucket, p_win, edge, dist_params")
        .eq("run_id", runId)
        .eq("recommended", 1),
```

The bets mapping parses mu defensively (blank cell on anything unparsable,
per the spec's error-handling section):

```tsx
    bets = (preds ?? [])
      .map((p) => {
        let forecastF: number | null = null;
        try {
          const mu = JSON.parse(p.dist_params as string)?.mu;
          if (typeof mu === "number") forecastF = mu;
        } catch {
          // no parsable mu -> blank forecast cell
        }
        return {
          title: titleOf.get(p.market_id) ?? p.market_id,
          bucket: p.bucket as string,
          pWin: p.p_win as number,
          ask: askOf.get(`${p.market_id}|${p.bucket}`) ?? 0,
          edge: p.edge as number,
          forecastF,
        };
      })
      .sort((a, b) => b.edge - a.edge);
```

- [ ] **Step 3: Render**

In the bets table: the Bucket cell becomes `{withCelsius(b.bucket)}`, and a
new Forecast column goes between Bucket and P(win):

Header row gains `<th>Forecast</th>` after `<th>Bucket</th>`.

Body row gains, after the bucket cell:

```tsx
                <td>{b.forecastF === null ? "" : `${b.forecastF.toFixed(1)}°F / ${degC(b.forecastF)}°C`}</td>
```

(If that line exceeds the file's prettier width, extract
`const forecast = ...` above the return; match however the build formats it.)

- [ ] **Step 4: Verify the build**

Run: `cd dashboard && npm run build`
Expected: compiles with no type errors.

Sanity-check the conversions by hand against the helper code (do not add test
infra): 69 -> 20.6, 68 -> 20.0, 72 -> 22.2; delta example unchanged in
`degDelta`.

- [ ] **Step 5: Commit**

```bash
git add dashboard/app/page.tsx
git commit -m "feat: show celsius alongside fahrenheit in dashboard bets

Bucket labels gain the converted bound(s); a new Forecast column shows
the bot's mu in both units. Display-only: storage and domain math stay
in Fahrenheit, the settlement unit."
```

---

## Verification (whole plan)

- `cd dashboard && npm run build` clean.
- `uv run pytest` untouched and green (no Python changes).
- Spot-check rendered strings by reading the helper: "69°F or below" becomes
  "69°F or below (<= 20.6°C)"; "68-69°F" becomes "68-69°F (20.0-20.6°C)";
  "72°F or higher" becomes "72°F or higher (>= 22.2°C)".
