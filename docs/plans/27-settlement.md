# Settle markets against NOAA actuals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Settle recorded markets whose settlement date has passed by recording the NOAA daily extreme into the `outcomes` table, via an idempotent `rainmaker settle` catch-up command run daily.

**Architecture:** Reuse the backfill's NCEI fetch (made variable-aware). A new `settle.py` finds unsettled past markets in the store, resolves each station's GHCND id from the recorded city via `STATIONS`, fetches the NOAA daily extreme, and records the outcome. No schema change (the `outcomes` table already exists). A `settle` CLI subcommand and a daily-workflow step run it against prod.

**Tech Stack:** Python 3.11+, httpx, sqlite3/psycopg via the `Conn` wrapper, pytest, pytest-httpx, NOAA NCEI.

---

## Task 1: Make the NCEI actuals fetch variable-aware

**Files:**
- Modify: `src/rainmaker/backfill.py`
- Test: `tests/test_backfill.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_backfill.py` (it already imports `re`, `date`, `httpx`, and `NCEI_URL`):

```python
def test_fetch_actuals_reads_tmin_when_asked(httpx_mock):
    rows = [{"DATE": "2026-03-01", "STATION": "X", "TMIN": "29"}]
    httpx_mock.add_response(url=re.compile(re.escape(NCEI_URL)), json=rows)
    with httpx.Client() as client:
        actuals = fetch_actuals("X", date(2026, 3, 1), date(2026, 3, 1), client, "TMIN")
    assert actuals == {date(2026, 3, 1): 29.0}
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_backfill.py::test_fetch_actuals_reads_tmin_when_asked -q`
Expected: FAIL with `TypeError` (current `fetch_actuals` takes 4 args, not 5).

- [ ] **Step 3: Add the `variable` parameter**

In `src/rainmaker/backfill.py`, replace the `fetch_actuals` function with:

```python
def fetch_actuals(
    ghcnd_id: str,
    start: date,
    end: date,
    client: httpx.Client,
    variable: str = "TMAX",
) -> dict[date, float]:
    """Daily extreme (degrees F) per date from NCEI daily-summaries. Raises on HTTP error.

    `variable` is the GHCND element to read: TMAX (daily high) or TMIN (daily low).
    """
    resp = client.get(
        NCEI_URL,
        params={
            "dataset": "daily-summaries",
            "stations": ghcnd_id,
            "dataTypes": variable,
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "units": "standard",
            "format": "json",
        },
    )
    resp.raise_for_status()
    rows: list[dict[str, Any]] = resp.json()
    return {
        date.fromisoformat(r["DATE"]): float(r[variable])
        for r in rows
        if r.get(variable) not in (None, "")
    }
```

- [ ] **Step 4: Pass the variable through `run_backfill`**

In the same file, in `run_backfill`, change the actuals line to pass the variable:

```python
    actuals = fetch_actuals(station.ghcnd_id, start, end, client, variable)
```

- [ ] **Step 5: Run the backfill tests**

Run: `uv run pytest tests/test_backfill.py -q`
Expected: PASS (the new TMIN test plus the existing TMAX tests, which use the default).

- [ ] **Step 6: Commit**

```bash
git add src/rainmaker/backfill.py tests/test_backfill.py
git commit -m "feat: make the NCEI actuals fetch variable-aware (TMAX/TMIN)"
```

---

## Task 2: Store helpers (record outcome, find unsettled)

**Files:**
- Modify: `src/rainmaker/store/record.py`
- Modify: `src/rainmaker/store/query.py`
- Create: `tests/test_settle.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_settle.py`:

```python
from datetime import date

from rainmaker.store.db import connect, init_schema
from rainmaker.store.query import unsettled_markets
from rainmaker.store.record import record_outcome


def _market(conn, market_id, city, variable, settlement_date):
    conn.execute(
        "INSERT INTO markets (id, city, variable, settlement_date) VALUES (?, ?, ?, ?)",
        (market_id, city, variable, settlement_date),
    )
    conn.commit()


def test_unsettled_markets_returns_past_markets_without_outcomes():
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "past", "NYC", "TMAX", "2026-05-30")
    _market(conn, "future", "NYC", "TMAX", "2030-01-01")
    _market(conn, "settled", "NYC", "TMAX", "2026-05-29")
    record_outcome(conn, "settled", 71.0, "2026-05-31T00:00:00Z")
    rows = unsettled_markets(conn, date(2026, 6, 3))
    conn.close()
    assert [r["market_id"] for r in rows] == ["past"]


def test_record_outcome_is_idempotent_upsert():
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "m1", "NYC", "TMAX", "2026-05-30")
    record_outcome(conn, "m1", 71.0, "2026-05-31T00:00:00Z")
    record_outcome(conn, "m1", 71.0, "2026-05-31T00:00:00Z")
    rows = conn.execute(
        "SELECT actual_value FROM outcomes WHERE market_id = ?", ("m1",)
    ).fetchall()
    conn.close()
    assert len(rows) == 1 and rows[0]["actual_value"] == 71.0
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_settle.py -q`
Expected: FAIL on import (`cannot import name 'unsettled_markets'` / `'record_outcome'`).

- [ ] **Step 3: Add `record_outcome` to `src/rainmaker/store/record.py`**

Append:

```python
def record_outcome(conn: Conn, market_id: str, actual_value: float, settled_at: str) -> None:
    """Upsert the settled actual for a market (keyed by market_id)."""
    conn.execute(
        "INSERT INTO outcomes (market_id, actual_value, settled_at) VALUES (?, ?, ?) "
        "ON CONFLICT(market_id) DO UPDATE SET "
        "actual_value = excluded.actual_value, settled_at = excluded.settled_at",
        (market_id, actual_value, settled_at),
    )
    conn.commit()
```

- [ ] **Step 4: Add `unsettled_markets` to `src/rainmaker/store/query.py`**

Add `from datetime import date` to the imports, then append:

```python
def unsettled_markets(conn: Conn, before: date) -> list[dict[str, Any]]:
    """Recorded markets with a past settlement date and no outcome yet."""
    rows = conn.execute(
        "SELECT m.id AS market_id, m.city AS city, m.variable AS variable, "
        "m.settlement_date AS settlement_date "
        "FROM markets m LEFT JOIN outcomes o ON o.market_id = m.id "
        "WHERE o.market_id IS NULL AND m.settlement_date < ? "
        "ORDER BY m.settlement_date",
        (before.isoformat(),),
    ).fetchall()
    return [dict(r) for r in rows]
```

- [ ] **Step 5: Settle imports and run the tests**

Run: `uv run ruff check --fix . && uv run ruff format . && uv run pytest tests/test_settle.py -q && uv run mypy src`
Expected: ruff settles the new imports, tests PASS, mypy `Success`.

- [ ] **Step 6: Commit**

```bash
git add src/rainmaker/store/record.py src/rainmaker/store/query.py tests/test_settle.py
git commit -m "feat: store helpers to record an outcome and find unsettled markets"
```

---

## Task 3: Settlement engine

**Files:**
- Create: `src/rainmaker/settle.py`
- Test: `tests/test_settle.py`

- [ ] **Step 1: Write the failing tests**

Add these imports to the top of `tests/test_settle.py` (alongside the existing ones):

```python
import re

import httpx

from rainmaker.backfill import NCEI_URL
from rainmaker.settle import run_settlement
```

Append the tests:

```python
def test_run_settlement_records_outcome(httpx_mock):
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "m1", "NYC", "TMAX", "2026-05-30")
    httpx_mock.add_response(
        url=re.compile(re.escape(NCEI_URL)),
        json=[{"DATE": "2026-05-30", "STATION": "USW00014732", "TMAX": "71"}],
    )
    with httpx.Client() as client:
        settled, waiting = run_settlement(conn, client, date(2026, 6, 3), "2026-06-03T00:00:00Z")
    row = conn.execute(
        "SELECT actual_value, settled_at FROM outcomes WHERE market_id = ?", ("m1",)
    ).fetchone()
    conn.close()
    assert (settled, waiting) == (1, 0)
    assert row["actual_value"] == 71.0
    assert row["settled_at"] == "2026-06-03T00:00:00Z"


def test_run_settlement_skips_when_no_data(httpx_mock):
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "m1", "NYC", "TMAX", "2026-05-30")
    httpx_mock.add_response(url=re.compile(re.escape(NCEI_URL)), json=[])
    with httpx.Client() as client:
        settled, waiting = run_settlement(conn, client, date(2026, 6, 3), "2026-06-03T00:00:00Z")
    n = conn.execute("SELECT count(*) AS n FROM outcomes").fetchone()["n"]
    conn.close()
    assert (settled, waiting) == (0, 1)
    assert n == 0


def test_run_settlement_idempotent(httpx_mock):
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "m1", "NYC", "TMAX", "2026-05-30")
    httpx_mock.add_response(
        url=re.compile(re.escape(NCEI_URL)),
        json=[{"DATE": "2026-05-30", "TMAX": "71"}],
    )
    with httpx.Client() as client:
        run_settlement(conn, client, date(2026, 6, 3), "2026-06-03T00:00:00Z")
        # m1 now has an outcome, so the second pass settles nothing and makes no request
        settled, waiting = run_settlement(conn, client, date(2026, 6, 3), "2026-06-03T00:00:00Z")
    conn.close()
    assert (settled, waiting) == (0, 0)


def test_run_settlement_skips_unknown_city():
    conn = connect(":memory:")
    init_schema(conn)
    _market(conn, "m1", "Atlantis", "TMAX", "2026-05-30")
    with httpx.Client() as client:
        settled, waiting = run_settlement(conn, client, date(2026, 6, 3), "2026-06-03T00:00:00Z")
    conn.close()
    assert (settled, waiting) == (0, 0)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_settle.py -q`
Expected: FAIL on import (`No module named 'rainmaker.settle'`).

- [ ] **Step 3: Create `src/rainmaker/settle.py`**

```python
"""Settle recorded markets against NOAA actuals (a proxy for Weather Underground).

For each recorded market whose settlement date has passed and that has no outcome
yet, fetch the NOAA daily extreme for its station/date/variable and record it.
Idempotent: already-settled markets are skipped, and a market whose NOAA data is
not published yet is left for a later run.
"""

import sys
from datetime import date

import httpx

from rainmaker.backfill import fetch_actuals
from rainmaker.config import STATIONS
from rainmaker.store.db import Conn
from rainmaker.store.query import unsettled_markets
from rainmaker.store.record import record_outcome


def run_settlement(
    conn: Conn, client: httpx.Client, today: date, settled_at: str
) -> tuple[int, int]:
    """Settle every unsettled past market that has NOAA data. Returns (settled, waiting)."""
    settled = 0
    waiting = 0
    for m in unsettled_markets(conn, today):
        station = STATIONS.get(m["city"])
        if station is None:
            print(f"skipping {m['market_id']}: unknown city {m['city']!r}", file=sys.stderr)
            continue
        day = date.fromisoformat(m["settlement_date"])
        actuals = fetch_actuals(station.ghcnd_id, day, day, client, m["variable"])
        value = actuals.get(day)
        if value is None:
            waiting += 1
            continue
        record_outcome(conn, m["market_id"], value, settled_at)
        settled += 1
    return settled, waiting
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_settle.py -q && uv run mypy src`
Expected: all settle tests PASS, mypy `Success`.

- [ ] **Step 5: Commit**

```bash
git add src/rainmaker/settle.py tests/test_settle.py
git commit -m "feat: settlement engine settling unsettled past markets against NOAA"
```

---

## Task 4: `rainmaker settle` CLI command

**Files:**
- Modify: `src/rainmaker/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cli.py`:

```python
def test_settle_command_reports_summary(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "run_settlement", lambda conn, client, today, settled_at: (2, 1))
    monkeypatch.setattr(cli.httpx, "Client", lambda **kw: _DummyClient())
    cli.main(["settle", "--db", str(tmp_path / "t.db")])
    out = capsys.readouterr().out
    assert "settled 2 market(s); 1 waiting" in out
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_cli.py::test_settle_command_reports_summary -q`
Expected: FAIL (`module 'rainmaker.cli' has no attribute 'run_settlement'`).

- [ ] **Step 3: Wire the command into `src/rainmaker/cli.py`**

Add the import next to `from rainmaker.backfill import run_backfill`:

```python
from rainmaker.settle import run_settlement
```

Add the `_settle` function after `_backfill`:

```python
def _settle(db_path: str) -> None:
    today = _today()
    settled_at = _now_iso()
    if "://" not in db_path:  # a Postgres DSN has no local parent dir to create
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    client = httpx.Client(headers={"User-Agent": NWS_USER_AGENT}, timeout=60.0)
    try:
        init_schema(conn)
        settled, waiting = run_settlement(conn, client, today, settled_at)
    finally:
        client.close()
        conn.close()
    print(f"settled {settled} market(s); {waiting} waiting on NCEI data -> {db_path}")
```

Register the subparser after the `backfill` parser block (before `args = parser.parse_args(argv)`):

```python
    settle = sub.add_parser("settle", help="settle past markets against NOAA actuals")
    settle.add_argument("--db", default=DB_PATH, help="SQLite database path")
```

Add the dispatch branch after the `backfill` branch:

```python
    elif args.command == "settle":
        _settle(db)
```

- [ ] **Step 4: Run the CLI tests**

Run: `uv run pytest tests/test_cli.py -q && uv run mypy src`
Expected: PASS and `Success`. Existing CLI tests are unaffected (they don't set `DATABASE_URL`, so they use the SQLite `--db` path).

- [ ] **Step 5: Commit**

```bash
git add src/rainmaker/cli.py tests/test_cli.py
git commit -m "feat: add the rainmaker settle CLI command"
```

---

## Task 5: Settle daily in the cloud

**Files:**
- Modify: `.github/workflows/daily-run.yml`

- [ ] **Step 1: Add a settle step after the run step**

In `.github/workflows/daily-run.yml`, insert this step immediately after the `Run the bot` step and before `Upload report artifacts`:

```yaml
      - name: Settle past markets
        env:
          DATABASE_URL: ${{ secrets.DATABASE_URL }}
        run: |
          case "$DATABASE_URL" in
            postgres://*|postgresql://*) ;;
            *) echo "DATABASE_URL is not a Postgres DSN; refusing to run"; exit 1 ;;
          esac
          uv run rainmaker settle
```

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/daily-run.yml
git commit -m "ci: settle past markets daily after the run"
```

---

## Task 6: Full verification and finalize

**Files:** none (verification only)

- [ ] **Step 1: Full check suite**

Run:
```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest -q
```
Expected: ruff clean, format clean, mypy `Success`, all tests pass (the Postgres integration test stays skipped without `DATABASE_URL`).

- [ ] **Step 2: Note on live verification**

A live settle does nothing useful yet: the only recorded prod markets settle today or later, so none are "past", and NCEI lags a day or two anyway. Settlement is verified by the unit tests here; the prod `settle` workflow step will record outcomes once markets' dates pass and NCEI publishes (confirm in the Supabase `outcomes` table in a couple of days). No special action needed now.

- [ ] **Step 3: Push and mark the PR ready**

```bash
git push
gh pr ready 28
```

---

## Notes

- Settlement uses NOAA NCEI as a documented proxy for Weather Underground (the true resolution source). It can differ by a degree on edge days.
- No schema change: settlement fills the existing `outcomes` table (`actual_value`, `settled_at`). The `won` column and P&L are sub-project 3.
- Out of scope (sub-project 3): P&L, calibration-accuracy tracking, the Vercel dashboard.
```
