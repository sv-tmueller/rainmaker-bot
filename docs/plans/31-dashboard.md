# Read-only dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A read-only web dashboard (Next.js on Vercel, behind Cloudflare Access) showing today's recommended bets and the bot's P&L/calibration, backed by a daily Python-written snapshot in Supabase.

**Architecture:** Python computes the metrics (reusing `tracking.py`) and upserts a `tracking_snapshot` row daily. The Next.js app in `dashboard/` reads Supabase server-side (service-role key) and renders today's recommendations plus the latest snapshot. No app auth (Cloudflare Access gates it).

**Tech Stack:** Python 3.11+ (backend); Next.js (App Router) + TypeScript + Tailwind + `@supabase/supabase-js` (frontend); pytest.

---

## Task 1: Snapshot table and writer

**Files:**
- Modify: `src/rainmaker/store/db.py` (base schema)
- Modify: `src/rainmaker/tracking.py`
- Test: `tests/test_tracking.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tracking.py` (it already has `_setup`, `connect`, `init_schema`, `pytest`):

```python
def test_write_snapshot_persists_metrics():
    from rainmaker.tracking import write_snapshot

    conn = connect(":memory:")
    _setup(conn)
    write_snapshot(conn, "2026-06-04", "2026-06-04T00:00:00Z")
    row = conn.execute(
        "SELECT * FROM tracking_snapshot WHERE snapshot_date = ?", ("2026-06-04",)
    ).fetchone()
    conn.close()
    assert row["n_bets"] == 2
    assert row["total_pnl"] == pytest.approx(0.30)
    assert row["n_scored"] == 2


def test_write_snapshot_is_idempotent_per_day():
    from rainmaker.tracking import write_snapshot

    conn = connect(":memory:")
    _setup(conn)
    write_snapshot(conn, "2026-06-04", "t1")
    write_snapshot(conn, "2026-06-04", "t2")
    n = conn.execute("SELECT count(*) AS n FROM tracking_snapshot").fetchone()["n"]
    conn.close()
    assert n == 1
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_tracking.py -k snapshot -q`
Expected: FAIL: no `tracking_snapshot` table and no `write_snapshot`.

- [ ] **Step 3: Add the table to the base schema in `src/rainmaker/store/db.py`**

Inside the `_SQLITE_SCHEMA` string, append this table definition before the closing `"""` (after the `calibration` table). The existing `_POSTGRES_SCHEMA` derivation turns the `REAL` columns into `DOUBLE PRECISION` automatically:

```sql

CREATE TABLE IF NOT EXISTS tracking_snapshot (
    snapshot_date TEXT PRIMARY KEY,
    n_bets        INTEGER,
    wins          INTEGER,
    losses        INTEGER,
    total_pnl     REAL,
    roi           REAL,
    brier         REAL,
    hit_rate      REAL,
    n_scored      INTEGER,
    created_at    TEXT
);
```

- [ ] **Step 4: Add `write_snapshot` to `src/rainmaker/tracking.py`**

Append:

```python
def write_snapshot(conn: Conn, on_date: str, created_at: str) -> dict[str, Any]:
    """Compute the current P&L/calibration and upsert a snapshot row for on_date."""
    pnl = compute_pnl(conn)
    cal = compute_calibration(conn)
    conn.execute(
        "INSERT INTO tracking_snapshot "
        "(snapshot_date, n_bets, wins, losses, total_pnl, roi, brier, hit_rate, "
        "n_scored, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(snapshot_date) DO UPDATE SET "
        "n_bets = excluded.n_bets, wins = excluded.wins, losses = excluded.losses, "
        "total_pnl = excluded.total_pnl, roi = excluded.roi, brier = excluded.brier, "
        "hit_rate = excluded.hit_rate, n_scored = excluded.n_scored, "
        "created_at = excluded.created_at",
        (
            on_date,
            pnl["n_bets"],
            pnl["wins"],
            pnl["losses"],
            pnl["total_pnl"],
            pnl["roi"],
            cal["brier"],
            cal["hit_rate"],
            cal["n"],
            created_at,
        ),
    )
    conn.commit()
    return {"pnl": pnl, "calibration": cal}
```

- [ ] **Step 5: Run the tracking tests and type check**

Run: `uv run pytest tests/test_tracking.py -q && uv run mypy src`
Expected: PASS and `Success`.

- [ ] **Step 6: Commit**

```bash
git add src/rainmaker/store/db.py src/rainmaker/tracking.py tests/test_tracking.py
git commit -m "feat: tracking_snapshot table and write_snapshot"
```

---

## Task 2: `rainmaker snapshot` CLI and workflow step

**Files:**
- Modify: `src/rainmaker/cli.py`
- Modify: `.github/workflows/daily-run.yml`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cli.py`:

```python
def test_snapshot_command_writes_and_reports(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        cli,
        "write_snapshot",
        lambda conn, on_date, created_at: {
            "pnl": {"n_bets": 2, "wins": 1, "losses": 1, "total_pnl": 0.3, "roi": 0.42},
            "calibration": {"n": 2, "brier": 0.13, "hit_rate": 0.5},
        },
    )
    cli.main(["snapshot", "--db", str(tmp_path / "t.db")])
    out = capsys.readouterr().out
    assert "snapshot" in out and "2 bets" in out
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_cli.py::test_snapshot_command_writes_and_reports -q`
Expected: FAIL (`module 'rainmaker.cli' has no attribute 'write_snapshot'`).

- [ ] **Step 3: Wire the command into `src/rainmaker/cli.py`**

Add to the tracking import line:

```python
from rainmaker.tracking import compute_calibration, compute_pnl, write_snapshot
```

Add the `_snapshot` function after `_track`:

```python
def _snapshot(db_path: str) -> None:
    on_date = _today().isoformat()
    if "://" not in db_path:  # a Postgres DSN has no local parent dir to create
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        init_schema(conn)
        result = write_snapshot(conn, on_date, _now_iso())
    finally:
        conn.close()
    p = result["pnl"]
    print(f"snapshot {on_date}: {p['n_bets']} bets, total {p['total_pnl']:+.2f}u -> {db_path}")
```

Register the subparser after the `track` parser block:

```python
    snapshot = sub.add_parser("snapshot", help="write a daily P&L/calibration snapshot row")
    snapshot.add_argument("--db", default=DB_PATH, help="SQLite database path")
```

Add the dispatch branch after the `track` branch:

```python
    elif args.command == "snapshot":
        _snapshot(db)
```

- [ ] **Step 4: Add the workflow step**

In `.github/workflows/daily-run.yml`, insert this step immediately after the `Settle past markets` step and before `Upload report artifacts`:

```yaml
      - name: Write tracking snapshot
        env:
          DATABASE_URL: ${{ secrets.DATABASE_URL }}
        run: |
          case "$DATABASE_URL" in
            postgres://*|postgresql://*) ;;
            *) echo "DATABASE_URL is not a Postgres DSN; refusing to run"; exit 1 ;;
          esac
          uv run rainmaker snapshot
```

- [ ] **Step 5: Run the CLI tests and type check**

Run: `uv run pytest tests/test_cli.py -q && uv run mypy src`
Expected: PASS and `Success`.

- [ ] **Step 6: Commit**

```bash
git add src/rainmaker/cli.py .github/workflows/daily-run.yml tests/test_cli.py
git commit -m "feat: rainmaker snapshot command and daily workflow step"
```

---

## Task 3: Scaffold the Next.js dashboard

**Files:**
- Create: `dashboard/` (via `create-next-app`)
- Create: `dashboard/lib/supabase.ts`, `dashboard/.env.example`
- Modify: `dashboard/.gitignore`

- [ ] **Step 1: Scaffold the app**

Run from the repo root:
```bash
npx create-next-app@latest dashboard --typescript --tailwind --app --no-src-dir --no-eslint --use-npm --no-import-alias
```
Expected: a `dashboard/` Next.js project is created (it has its own `.gitignore` ignoring `node_modules/` and `.next/`).

- [ ] **Step 2: Add the Supabase client dependency**

Run:
```bash
cd dashboard && npm install @supabase/supabase-js && cd ..
```

- [ ] **Step 3: Create the server-only Supabase client**

Create `dashboard/lib/supabase.ts`:

```ts
import { createClient } from "@supabase/supabase-js";

// Server-only: uses the service-role key, which must never reach the browser.
export function serverClient() {
  const url = process.env.SUPABASE_URL;
  const key = process.env.SUPABASE_SERVICE_ROLE_KEY;
  if (!url || !key) {
    throw new Error("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set");
  }
  return createClient(url, key, { auth: { persistSession: false } });
}
```

- [ ] **Step 4: Document env and ignore local env files**

Create `dashboard/.env.example`:

```
SUPABASE_URL=
SUPABASE_SERVICE_ROLE_KEY=
```

Append to `dashboard/.gitignore`:

```
.env
.env.local
```

- [ ] **Step 5: Verify it builds**

Run: `cd dashboard && npm run build && cd ..`
Expected: the default app type-checks and builds successfully.

- [ ] **Step 6: Commit**

```bash
git add dashboard
git commit -m "build: scaffold the Next.js dashboard with a Supabase client"
```

---

## Task 4: Dashboard page (today's bets + performance)

**Files:**
- Modify: `dashboard/app/page.tsx`

- [ ] **Step 1: Replace `dashboard/app/page.tsx`**

```tsx
import { serverClient } from "../lib/supabase";

export const dynamic = "force-dynamic"; // always read live data, never prerender

type Bet = { title: string; bucket: string; pWin: number; ask: number; edge: number };

async function getData() {
  const db = serverClient();

  const { data: runs } = await db
    .from("runs")
    .select("id")
    .order("started_at", { ascending: false })
    .limit(1);
  const runId = runs?.[0]?.id as string | undefined;

  let bets: Bet[] = [];
  if (runId) {
    const [{ data: preds }, { data: prices }, { data: markets }] = await Promise.all([
      db.from("predictions").select("market_id, bucket, p_win, edge").eq("run_id", runId).eq("recommended", 1),
      db.from("prices").select("market_id, outcome, price").eq("run_id", runId),
      db.from("markets").select("id, title"),
    ]);
    const askOf = new Map((prices ?? []).map((p) => [`${p.market_id}|${p.outcome}`, p.price as number]));
    const titleOf = new Map((markets ?? []).map((m) => [m.id, m.title as string]));
    bets = (preds ?? [])
      .map((p) => ({
        title: titleOf.get(p.market_id) ?? p.market_id,
        bucket: p.bucket as string,
        pWin: p.p_win as number,
        ask: askOf.get(`${p.market_id}|${p.bucket}`) ?? 0,
        edge: p.edge as number,
      }))
      .sort((a, b) => b.edge - a.edge);
  }

  const { data: snaps } = await db
    .from("tracking_snapshot")
    .select("*")
    .order("snapshot_date", { ascending: false })
    .limit(1);
  const snap = snaps?.[0] ?? null;

  return { bets, snap };
}

function pct(x: number) {
  return `${(x * 100).toFixed(0)}%`;
}

export default async function Page() {
  const { bets, snap } = await getData();
  return (
    <main className="mx-auto max-w-3xl p-6 font-sans">
      <h1 className="text-2xl font-bold">Rainmaker</h1>

      <h2 className="mt-6 text-lg font-semibold">Recommended bets</h2>
      {bets.length === 0 ? (
        <p className="text-gray-500">No bets pass the gates right now.</p>
      ) : (
        <table className="mt-2 w-full text-sm">
          <thead>
            <tr className="text-left text-gray-500">
              <th>Market</th>
              <th>Bucket</th>
              <th>P(win)</th>
              <th>Ask</th>
              <th>Edge</th>
            </tr>
          </thead>
          <tbody>
            {bets.map((b, i) => (
              <tr key={i} className="border-t">
                <td>{b.title}</td>
                <td>{b.bucket}</td>
                <td>{pct(b.pWin)}</td>
                <td>{b.ask.toFixed(2)}</td>
                <td>{b.edge >= 0 ? "+" : ""}{b.edge.toFixed(2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h2 className="mt-6 text-lg font-semibold">Performance</h2>
      {!snap || snap.n_bets === 0 ? (
        <p className="text-gray-500">No settled results yet.</p>
      ) : (
        <ul className="mt-2 text-sm">
          <li>
            P&amp;L: {snap.total_pnl >= 0 ? "+" : ""}
            {snap.total_pnl.toFixed(2)}u over {snap.n_bets} bets ({snap.wins}-{snap.losses}), ROI{" "}
            {snap.roi >= 0 ? "+" : ""}
            {pct(snap.roi)}
          </li>
          <li>
            Calibration: Brier {snap.brier === null ? "n/a" : snap.brier.toFixed(3)}, hit rate{" "}
            {snap.hit_rate === null ? "n/a" : pct(snap.hit_rate)} (n={snap.n_scored})
          </li>
        </ul>
      )}
      <p className="mt-6 text-xs text-gray-400">
        {snap ? `snapshot ${snap.snapshot_date}` : "no snapshot yet"}
      </p>
    </main>
  );
}
```

- [ ] **Step 2: Verify it builds**

Run: `cd dashboard && npm run build && cd ..`
Expected: type-checks and builds. (`force-dynamic` means the page is not prerendered, so the build does not need Supabase env vars.)

- [ ] **Step 3: Commit**

```bash
git add dashboard/app/page.tsx
git commit -m "feat: dashboard page showing today's bets and performance"
```

---

## Task 5: Full verification and finalize

**Files:** none (verification only)

- [ ] **Step 1: Python check suite**

Run:
```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest -q
```
Expected: ruff clean (it does not scan `dashboard/`), format clean, mypy `Success`, all Python tests pass.

- [ ] **Step 2: Frontend build**

Run: `cd dashboard && npm run build && cd ..`
Expected: builds clean.

- [ ] **Step 3: Local snapshot smoke (Python)**

Run: `uv run rainmaker snapshot --db /tmp/rm_snap.db`
Expected: prints `snapshot <today>: 0 bets, total +0.00u -> /tmp/rm_snap.db` (a fresh db has no settled data).

- [ ] **Step 4: Note on the deploy (operator)**

Manual, not in this PR: create the Vercel project with root directory `dashboard/`, set `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`, and put Cloudflare Access in front of the hostname. The daily workflow's new `snapshot` step starts populating `tracking_snapshot` on the next run; today's bets render as soon as a run exists.

- [ ] **Step 5: Push and mark the PR ready**

```bash
git push
gh pr ready 32
```

---

## Notes

- `tracking_snapshot` is a new table, so the base-schema `CREATE TABLE IF NOT EXISTS` creates it on prod at the next `init_schema` (no migration).
- The service-role key is server-only (`dashboard/lib/supabase.ts`, used only in the server component). Cloudflare Access gates the whole app, so no in-app auth.
- `ruff`/`mypy`/`pytest` cover Python only; the frontend is verified by `next build` and a manual check, per the spec.
- Out of scope: charts/time-series (the snapshot history enables them later), accounts, and any write actions.
```
