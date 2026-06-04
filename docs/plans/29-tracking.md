# P&L + calibration tracking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Score the bot's recommendations and forecasts against settled outcomes via a `rainmaker track` command: hypothetical P&L (flat unit stake) and calibration (Brier + hit rate), after adding a migration mechanism and a `bucket` column to predictions.

**Architecture:** A small versioned migration runner (`schema_migrations` + `apply_migrations`, called from `init_schema`) adds `predictions.bucket`; the recorder writes the bucket label going forward. P&L and calibration are computed on read by joining `predictions` + `prices` + `outcomes`. A new CLI subcommand prints the summary.

**Tech Stack:** Python 3.11+, sqlite3/psycopg via the `Conn` wrapper, pytest.

---

## Task 1: Migration mechanism

**Files:**
- Create: `src/rainmaker/store/migrate.py`
- Modify: `src/rainmaker/store/db.py` (`init_schema`)
- Test: `tests/test_migrate.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_migrate.py`:

```python
from rainmaker.store.db import connect, init_schema
from rainmaker.store.migrate import apply_migrations


def test_migration_adds_predictions_bucket_column():
    conn = connect(":memory:")
    init_schema(conn)
    conn.execute(
        "INSERT INTO predictions (run_id, market_id, bucket, p_win, edge, recommended, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (None, None, "70-71°F", 0.9, 0.1, 1, "2026-06-04T00:00:00Z"),
    )
    conn.commit()
    row = conn.execute("SELECT bucket FROM predictions").fetchone()
    conn.close()
    assert row["bucket"] == "70-71°F"


def test_apply_migrations_is_idempotent():
    conn = connect(":memory:")
    init_schema(conn)
    apply_migrations(conn)  # second pass must not error
    n = conn.execute("SELECT count(*) AS n FROM schema_migrations").fetchone()["n"]
    conn.close()
    assert n == 1
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_migrate.py -q`
Expected: FAIL on import (`No module named 'rainmaker.store.migrate'`).

- [ ] **Step 3: Create `src/rainmaker/store/migrate.py`**

```python
"""Forward schema migrations, tracked so each runs once.

The base schema in db.py is the initial shape; every change since is a migration
here. Both backends accept `ALTER TABLE ... ADD COLUMN`.
"""

from datetime import UTC, datetime

from rainmaker.store.db import Conn

_MIGRATIONS: list[tuple[str, list[str]]] = [
    ("0001_predictions_bucket", ["ALTER TABLE predictions ADD COLUMN bucket TEXT"]),
]


def apply_migrations(conn: Conn) -> None:
    """Run each not-yet-applied migration once and record it. Idempotent."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (id TEXT PRIMARY KEY, applied_at TEXT)"
    )
    applied = {r["id"] for r in conn.execute("SELECT id FROM schema_migrations").fetchall()}
    for migration_id, statements in _MIGRATIONS:
        if migration_id in applied:
            continue
        for statement in statements:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_migrations (id, applied_at) VALUES (?, ?)",
            (migration_id, datetime.now(UTC).isoformat()),
        )
    conn.commit()
```

- [ ] **Step 4: Call migrations from `init_schema` in `src/rainmaker/store/db.py`**

Replace the `init_schema` function with:

```python
def init_schema(conn: Conn) -> None:
    """Create every table if absent, then apply forward migrations. Idempotent."""
    for statement in _schema_for(conn.backend).split(";"):
        if statement.strip():
            conn.execute(statement)
    conn.commit()
    # Imported here (not at module top) to avoid a db <-> migrate import cycle.
    from rainmaker.store.migrate import apply_migrations

    apply_migrations(conn)
```

- [ ] **Step 5: Run the migration tests and type check**

Run: `uv run pytest tests/test_migrate.py -q && uv run mypy src`
Expected: PASS and `Success`.

- [ ] **Step 6: Commit**

```bash
git add src/rainmaker/store/migrate.py src/rainmaker/store/db.py tests/test_migrate.py
git commit -m "feat: schema migration mechanism; add predictions.bucket (0001)"
```

---

## Task 2: Record the bucket on each prediction

**Files:**
- Modify: `src/rainmaker/store/record.py` (`_record_predictions`)
- Test: `tests/test_store_record.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_store_record.py` (it already has `_evaluated`, `connect`, `init_schema`, `record_run`):

```python
def test_record_predictions_stores_bucket():
    conn = connect(":memory:")
    init_schema(conn)
    market, fs, report = _evaluated()
    record_run(
        conn,
        run_id="run-1",
        started_at="t0",
        finished_at="t1",
        status="ok",
        evaluated=[(market, fs, report)],
    )
    rows = conn.execute("SELECT bucket FROM predictions WHERE run_id = ?", ("run-1",)).fetchall()
    conn.close()
    assert {r["bucket"] for r in rows} == {o.bucket_label for o in report.outcomes}
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_store_record.py::test_record_predictions_stores_bucket -q`
Expected: FAIL: predictions store `bucket = NULL` today, so the set is `{None}`, not the bucket labels.

- [ ] **Step 3: Store the bucket label in `_record_predictions`**

In `src/rainmaker/store/record.py`, replace the `INSERT` inside `_record_predictions` with the bucket-aware version:

```python
        conn.execute(
            "INSERT INTO predictions "
            "(run_id, market_id, bucket, p_win, confidence, dist_params, edge, recommended, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            # confidence stays NULL: no calibrated confidence metric is recorded here.
            (
                run_id,
                market_id,
                o.bucket_label,
                o.p_win,
                None,
                dist_params,
                o.edge,
                int(o.recommended),
                created_at,
            ),
        )
```

- [ ] **Step 4: Run the store tests**

Run: `uv run pytest tests/test_store_record.py -q`
Expected: PASS (the new test plus the existing record tests, which don't assert on column count).

- [ ] **Step 5: Commit**

```bash
git add src/rainmaker/store/record.py tests/test_store_record.py
git commit -m "feat: record the bucket label on each prediction"
```

---

## Task 3: P&L and calibration computation

**Files:**
- Create: `src/rainmaker/tracking.py`
- Test: `tests/test_tracking.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_tracking.py`:

```python
import pytest

from rainmaker.store.db import connect, init_schema
from rainmaker.tracking import compute_calibration, compute_pnl


def _setup(conn):
    init_schema(conn)
    conn.execute("INSERT INTO runs (id, started_at, status) VALUES (?, ?, ?)", ("r1", "t", "ok"))
    conn.execute(
        "INSERT INTO markets (id, city, variable, settlement_date) VALUES (?, ?, ?, ?)",
        ("m1", "NYC", "TMAX", "2026-05-30"),
    )
    for outcome, price in (("70-71°F", 0.40), ("72-73°F", 0.30)):
        conn.execute(
            "INSERT INTO prices (run_id, market_id, outcome, price, implied_prob, captured_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("r1", "m1", outcome, price, price, "t"),
        )
    for bucket, p_win in (("70-71°F", 0.93), ("72-73°F", 0.50)):
        conn.execute(
            "INSERT INTO predictions "
            "(run_id, market_id, bucket, p_win, edge, recommended, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("r1", "m1", bucket, p_win, 0.1, 1, "t"),
        )
    # actual 71 -> 70-71 wins, 72-73 loses
    conn.execute(
        "INSERT INTO outcomes (market_id, actual_value, settled_at) VALUES (?, ?, ?)",
        ("m1", 71.0, "t"),
    )
    conn.commit()


def test_compute_pnl_sums_recommended_bets():
    conn = connect(":memory:")
    _setup(conn)
    pnl = compute_pnl(conn)
    conn.close()
    assert pnl["n_bets"] == 2
    assert (pnl["wins"], pnl["losses"]) == (1, 1)
    assert pnl["total_pnl"] == pytest.approx(0.30)  # (1 - 0.40) + (-0.30)
    assert pnl["roi"] == pytest.approx(0.30 / 0.70)  # staked = 0.40 + 0.30


def test_compute_calibration_brier_and_hit_rate():
    conn = connect(":memory:")
    _setup(conn)
    cal = compute_calibration(conn)
    conn.close()
    assert cal["n"] == 2
    assert cal["brier"] == pytest.approx(((0.93 - 1) ** 2 + (0.50 - 0) ** 2) / 2)
    assert cal["hit_rate"] == pytest.approx(0.5)


def test_compute_pnl_empty_when_nothing_settled():
    conn = connect(":memory:")
    init_schema(conn)
    pnl = compute_pnl(conn)
    conn.close()
    assert pnl == {"n_bets": 0, "wins": 0, "losses": 0, "total_pnl": 0.0, "roi": 0.0}
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_tracking.py -q`
Expected: FAIL on import (`No module named 'rainmaker.tracking'`).

- [ ] **Step 3: Create `src/rainmaker/tracking.py`**

```python
"""Score the bot against settled outcomes: hypothetical P&L and calibration.

Computed on read from predictions + prices + outcomes. Each recommended
prediction row is one one-unit bet (a market re-recommended across daily runs
counts as separate bets). Tracking only covers rows with a bucket recorded.
"""

from typing import Any

from rainmaker.polymarket.markets import parse_bucket_label
from rainmaker.store.db import Conn


def _won(bucket_label: str, actual_value: float) -> bool:
    kind, lo, hi, threshold = parse_bucket_label(bucket_label)
    v = round(actual_value)
    if kind == "below":
        assert threshold is not None
        return v <= threshold
    if kind == "above":
        assert threshold is not None
        return v >= threshold
    assert lo is not None and hi is not None
    return lo <= v <= hi


def _settled_rows(conn: Conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT p.bucket AS bucket, p.p_win AS p_win, p.recommended AS recommended, "
        "pr.price AS ask, o.actual_value AS actual_value "
        "FROM predictions p "
        "JOIN outcomes o ON o.market_id = p.market_id "
        "JOIN prices pr ON pr.run_id = p.run_id AND pr.market_id = p.market_id "
        "AND pr.outcome = p.bucket "
        "WHERE p.bucket IS NOT NULL AND pr.price IS NOT NULL"
    ).fetchall()
    return [dict(r) for r in rows]


def compute_pnl(conn: Conn) -> dict[str, Any]:
    """Hypothetical P&L over recommended bets at a flat one-unit stake."""
    total_pnl = 0.0
    total_staked = 0.0
    wins = 0
    n = 0
    for r in _settled_rows(conn):
        if not r["recommended"]:
            continue
        n += 1
        ask = r["ask"]
        total_staked += ask
        if _won(r["bucket"], r["actual_value"]):
            wins += 1
            total_pnl += 1 - ask
        else:
            total_pnl -= ask
    roi = total_pnl / total_staked if total_staked else 0.0
    return {
        "n_bets": n,
        "wins": wins,
        "losses": n - wins,
        "total_pnl": total_pnl,
        "roi": roi,
    }


def compute_calibration(conn: Conn) -> dict[str, Any]:
    """Brier score over all settled bucket-predictions, plus recommended hit rate."""
    rows = _settled_rows(conn)
    if not rows:
        return {"n": 0, "brier": None, "hit_rate": None}
    brier = sum(
        (r["p_win"] - (1.0 if _won(r["bucket"], r["actual_value"]) else 0.0)) ** 2 for r in rows
    ) / len(rows)
    recommended = [r for r in rows if r["recommended"]]
    hit_rate = (
        sum(1 for r in recommended if _won(r["bucket"], r["actual_value"])) / len(recommended)
        if recommended
        else None
    )
    return {"n": len(rows), "brier": brier, "hit_rate": hit_rate}
```

- [ ] **Step 4: Run the tracking tests and type check**

Run: `uv run pytest tests/test_tracking.py -q && uv run mypy src`
Expected: PASS and `Success`.

- [ ] **Step 5: Commit**

```bash
git add src/rainmaker/tracking.py tests/test_tracking.py
git commit -m "feat: hypothetical P&L and calibration computed from the store"
```

---

## Task 4: `rainmaker track` CLI command

**Files:**
- Modify: `src/rainmaker/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cli.py`:

```python
def test_track_command_reports_pnl_and_calibration(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        cli,
        "compute_pnl",
        lambda conn: {"n_bets": 2, "wins": 1, "losses": 1, "total_pnl": 0.3, "roi": 0.42},
    )
    monkeypatch.setattr(
        cli, "compute_calibration", lambda conn: {"n": 2, "brier": 0.127, "hit_rate": 0.5}
    )
    cli.main(["track", "--db", str(tmp_path / "t.db")])
    out = capsys.readouterr().out
    assert "P&L: 2 bets, 1-1" in out
    assert "Brier 0.127" in out
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_cli.py::test_track_command_reports_pnl_and_calibration -q`
Expected: FAIL (`module 'rainmaker.cli' has no attribute 'compute_pnl'`).

- [ ] **Step 3: Wire the command into `src/rainmaker/cli.py`**

Add the import next to the other `from rainmaker...` imports (after `from rainmaker.store.record import ...`):

```python
from rainmaker.tracking import compute_calibration, compute_pnl
```

Add the `_track` function after `_settle`:

```python
def _track(db_path: str) -> None:
    if "://" not in db_path:  # a Postgres DSN has no local parent dir to create
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        init_schema(conn)
        pnl = compute_pnl(conn)
        cal = compute_calibration(conn)
    finally:
        conn.close()
    print(
        f"P&L: {pnl['n_bets']} bets, {pnl['wins']}-{pnl['losses']}, "
        f"total {pnl['total_pnl']:+.2f}u, ROI {pnl['roi']:+.1%}"
    )
    brier = "n/a" if cal["brier"] is None else f"{cal['brier']:.3f}"
    hit = "n/a" if cal["hit_rate"] is None else f"{cal['hit_rate']:.0%}"
    print(f"calibration: Brier {brier}, recommended hit rate {hit} (n={cal['n']})")
```

Register the subparser after the `settle` parser block:

```python
    track = sub.add_parser("track", help="report P&L and calibration over settled markets")
    track.add_argument("--db", default=DB_PATH, help="SQLite database path")
```

Add the dispatch branch after the `settle` branch:

```python
    elif args.command == "track":
        _track(db)
```

- [ ] **Step 4: Run the CLI tests and type check**

Run: `uv run pytest tests/test_cli.py -q && uv run mypy src`
Expected: PASS and `Success`.

- [ ] **Step 5: Commit**

```bash
git add src/rainmaker/cli.py tests/test_cli.py
git commit -m "feat: add the rainmaker track CLI command"
```

---

## Task 5: Full verification and finalize

**Files:** none (verification only)

- [ ] **Step 1: Full check suite**

Run:
```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest -q
```
Expected: ruff clean, format clean, mypy `Success`, all tests pass (the Postgres integration test stays skipped without `DATABASE_URL`).

- [ ] **Step 2: Local smoke**

Run: `uv run rainmaker track --db /tmp/rm_track.db`
Expected: prints `P&L: 0 bets, 0-0, total +0.00u, ROI +0.0%` and `calibration: Brier n/a, recommended hit rate n/a (n=0)` (a fresh db has no settled data). This confirms the command wiring and the empty-data path.

- [ ] **Step 3: Note on live data**

No useful live numbers yet: prod settlement is still catching up (NCEI lag), and only predictions recorded after this lands carry a bucket. Real P&L/calibration accrue over the coming days; check `rainmaker track` against prod (or the Supabase data) once settled markets with bucketed predictions exist.

- [ ] **Step 4: Push and mark the PR ready**

```bash
git push
gh pr ready 30
```

---

## Notes

- Bet semantics: each recommended prediction row is one one-unit bet. Because the bot re-recommends a market on each daily run until it settles, a market can contribute several bets at different asks. This measures "bet one unit every time the bot recommended"; deduping to one bet per market is a possible later refinement.
- No new tables beyond `schema_migrations`; P&L and calibration are derived on read.
- Out of scope (sub-project 4): the Vercel dashboard and any by-date time series (the dashboard will add the time dimension over these derivations).
```
