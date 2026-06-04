# Forecast accuracy visibility - implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make forecast accuracy (degrees off, per city) visible on the dashboard, fed by both the calibration backtest and the bot's own settled predictions.

**Architecture:** One new `forecast_accuracy` table (base schema, both backends). The degrees-space math is one pure function `compute_accuracy(pairs)` in `probability/calibration.py`, reused by both writers: `run_backfill` returns accuracy alongside the calibration fit (kind=backtest, written by the CLI), and `tracking.compute_live_accuracy` builds pairs from settled predictions (kind=live, written by `write_snapshot`, so the daily cron needs no change). The dashboard gains one read-only section.

**Tech Stack:** Python 3.11 (numpy, pydantic, httpx), SQLite/Postgres dual backend, Next.js dashboard. No new dependencies.

Spec: `docs/superpowers/specs/2026-06-04-accuracy-visibility-and-display-design.md`
Issue: #42

**Note for implementers:** this branch (`feat/42-forecast-accuracy`) was cut from main before PR #44 (min-edge gate) merged, so `evaluate_market` here still has the pre-#44 signature. Do not "fix" that; merge order handles it.

---

### Task 1: `compute_accuracy` (pure math, TDD)

**Files:**
- Modify: `src/rainmaker/probability/calibration.py` (new model + function)
- Test: `tests/test_calibration.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_calibration.py` (its existing imports already include
`pytest` and `CalibrationPair`; extend the `rainmaker.probability.calibration`
import with `Accuracy, compute_accuracy`):

```python
def test_compute_accuracy_mae_and_bias():
    pairs = [
        CalibrationPair(mu=70.0, sigma=2.0, actual=68.0),  # error +2
        CalibrationPair(mu=70.0, sigma=2.0, actual=73.0),  # error -3
        CalibrationPair(mu=70.0, sigma=2.0, actual=69.0),  # error +1
    ]
    acc = compute_accuracy(pairs)
    assert acc.n == 3
    assert acc.mae_f == pytest.approx(2.0)  # (2 + 3 + 1) / 3
    assert acc.bias_f == pytest.approx(0.0)  # (2 - 3 + 1) / 3


def test_compute_accuracy_empty_raises():
    with pytest.raises(ValueError, match="no pairs"):
        compute_accuracy([])
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_calibration.py -v -k compute_accuracy`
Expected: FAIL with `ImportError: cannot import name 'Accuracy'`

- [ ] **Step 3: Implement**

In `src/rainmaker/probability/calibration.py`, after the `Calibration` model:

```python
class Accuracy(BaseModel):
    model_config = ConfigDict(frozen=True)

    n: int
    mae_f: float  # mean absolute error, degrees F
    bias_f: float  # mean signed error (mu - actual), degrees F
```

and after `fit_calibration`:

```python
def compute_accuracy(pairs: list[CalibrationPair]) -> Accuracy:
    """Degrees-space forecast accuracy over forecast-vs-actual pairs."""
    if not pairs:
        raise ValueError("cannot compute accuracy with no pairs")
    errors = np.array([p.mu - p.actual for p in pairs])
    return Accuracy(
        n=len(pairs), mae_f=float(np.mean(np.abs(errors))), bias_f=float(np.mean(errors))
    )
```

(`bias_f` is by construction the same value as `fit_calibration`'s `bias`;
storing it on the accuracy row keeps the table self-contained.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_calibration.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/rainmaker/probability/calibration.py tests/test_calibration.py
git commit -m "feat: compute degrees-space forecast accuracy from pairs

The backfill builds forecast-vs-actual pairs and keeps only the
calibration fit; MAE and signed error in degrees are the numbers a human
needs to judge the forecasts. One pure function both the backtest and
live paths can share."
```

---

### Task 2: `forecast_accuracy` table + `save_accuracy` (TDD)

**Files:**
- Modify: `src/rainmaker/store/db.py` (base schema, after the `calibration` table)
- Modify: `src/rainmaker/store/record.py` (new upsert)
- Test: `tests/test_store_record.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_store_record.py`. Add `import pytest` to its imports
(currently absent) and extend the store imports:

```python
from rainmaker.probability.calibration import Accuracy
from rainmaker.store.record import record_run, save_accuracy
```

(`record_run` is already imported; fold `save_accuracy` into that line.)

```python
def test_accuracy_save_and_upsert_round_trip():
    conn = connect(":memory:")
    init_schema(conn)
    save_accuracy(
        conn,
        station="KSEA",
        city="Seattle",
        variable="TMAX",
        lead_time=1,
        kind="backtest",
        accuracy=Accuracy(n=60, mae_f=2.1, bias_f=-0.4),
        updated_at="t0",
    )
    row = conn.execute("SELECT * FROM forecast_accuracy").fetchone()
    assert (row["station"], row["city"], row["kind"]) == ("KSEA", "Seattle", "backtest")
    assert row["n"] == 60
    assert row["mae_f"] == pytest.approx(2.1)

    # same key again -> upserted, not duplicated
    save_accuracy(
        conn,
        station="KSEA",
        city="Seattle",
        variable="TMAX",
        lead_time=1,
        kind="backtest",
        accuracy=Accuracy(n=61, mae_f=2.0, bias_f=-0.3),
        updated_at="t1",
    )
    rows = conn.execute("SELECT * FROM forecast_accuracy").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0]["n"] == 61
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_store_record.py -v -k accuracy`
Expected: FAIL with `ImportError: cannot import name 'save_accuracy'`

- [ ] **Step 3: Implement**

`src/rainmaker/store/db.py` - add to `_SQLITE_SCHEMA` between the
`calibration` and `tracking_snapshot` tables:

```sql
CREATE TABLE IF NOT EXISTS forecast_accuracy (
    station    TEXT NOT NULL,
    city       TEXT,
    variable   TEXT NOT NULL,
    lead_time  INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    n          INTEGER,
    mae_f      REAL,
    bias_f     REAL,
    updated_at TEXT,
    PRIMARY KEY (station, variable, lead_time, kind)
);
```

No `_POSTGRES_SCHEMA` change needed: the existing `" REAL,"` to
`" DOUBLE PRECISION,"` replace covers `mae_f`/`bias_f`, and the table has no
surrogate id. New tables go in the base schema per repo convention
(`CREATE TABLE IF NOT EXISTS` picks it up on existing databases).

`src/rainmaker/store/record.py` - extend the calibration import to
`from rainmaker.probability.calibration import Accuracy, Calibration` and add
after `save_calibration`:

```python
def save_accuracy(
    conn: Conn,
    *,
    station: str,
    city: str,
    variable: str,
    lead_time: int,
    kind: str,
    accuracy: Accuracy,
    updated_at: str,
) -> None:
    """Upsert one accuracy row (keyed by station, variable, lead_time, kind)."""
    conn.execute(
        """
        INSERT INTO forecast_accuracy
            (station, city, variable, lead_time, kind, n, mae_f, bias_f, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(station, variable, lead_time, kind) DO UPDATE SET
            city = excluded.city, n = excluded.n, mae_f = excluded.mae_f,
            bias_f = excluded.bias_f, updated_at = excluded.updated_at
        """,
        (
            station,
            city,
            variable,
            lead_time,
            kind,
            accuracy.n,
            accuracy.mae_f,
            accuracy.bias_f,
            updated_at,
        ),
    )
    conn.commit()
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_store_record.py tests/test_store_db.py tests/test_migrate.py -v`
Expected: all PASS (schema init stays idempotent).

- [ ] **Step 5: Commit**

```bash
git add src/rainmaker/store/db.py src/rainmaker/store/record.py tests/test_store_record.py
git commit -m "feat: forecast_accuracy table and upsert

Keyed by (station, variable, lead_time, kind) so the backtest and live
measurements of the same cell coexist; city is denormalized for the
dashboard, which has no station registry."
```

---

### Task 3: backfill returns accuracy (TDD)

**Files:**
- Modify: `src/rainmaker/backfill.py:100-113` (`run_backfill` return type)
- Test: `tests/test_backfill.py:93-106`

- [ ] **Step 1: Change the test to expect the tuple**

In `tests/test_backfill.py`, rewrite
`test_run_backfill_fits_calibration_from_history` (lines 93-106):

```python
def test_run_backfill_fits_calibration_and_accuracy_from_history(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(re.escape(HISTORICAL_FORECAST_URL)), json=_hist_fixture()
    )
    httpx_mock.add_response(url=re.compile(re.escape(NCEI_URL)), json=_actuals_fixture())
    with httpx.Client() as client:
        cal, acc = run_backfill(KLGA, "TMAX", 1, date(2026, 3, 1), date(2026, 3, 5), client)
    assert cal.station == "KLGA"
    assert cal.variable == "TMAX"
    assert cal.lead_time == 1
    assert cal.n_samples == 5
    # forecasts run cold across the window (mean signed error mu - actual is negative)
    assert cal.bias == pytest.approx(-2.38, abs=1e-2)
    assert cal.spread_scale > 0
    # accuracy is measured over the same pairs
    assert acc.n == 5
    assert acc.bias_f == pytest.approx(cal.bias)
    assert acc.mae_f >= abs(acc.bias_f)  # mean |e| always >= |mean e|
    assert acc.mae_f > 0
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_backfill.py -v -k run_backfill`
Expected: FAIL with a TypeError/ValueError on unpacking `Calibration` into two names.

- [ ] **Step 3: Implement**

In `src/rainmaker/backfill.py`, extend the calibration import to include
`Accuracy` and `compute_accuracy`, and change `run_backfill`:

```python
from rainmaker.probability.calibration import (
    Accuracy,
    Calibration,
    CalibrationPair,
    compute_accuracy,
    fit_calibration,
)
```

```python
def run_backfill(
    station: Station,
    variable: str,
    lead_time: int,
    start: date,
    end: date,
    client: httpx.Client,
) -> tuple[Calibration, Accuracy]:
    """Fetch history, build pairs, fit one calibration cell, measure accuracy."""
    forecasts = fetch_historical_forecasts(station, start, end, client)
    actuals = fetch_actuals(station.ghcnd_id, start, end, client, variable)
    pairs = build_pairs(forecasts, actuals)
    return fit_calibration(station.icao, variable, lead_time, pairs), compute_accuracy(pairs)
```

(Zero pairs: `fit_calibration` raises before `compute_accuracy` runs, so no
accuracy row can be written without a calibration fit. The CLI catches this
per city in Task 4.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_backfill.py -v`
Expected: all PASS. (`tests/test_cli.py` will now FAIL on
`test_backfill_fits_and_saves_calibration` because the CLI still consumes the
old return shape. Confirm the failure is exactly that one test, then proceed.)

- [ ] **Step 5: Do NOT commit yet**

The return-shape change and the CLI wiring are one atomic change; committing
here would leave the suite red. Task 4's commit covers both.

---

### Task 4: CLI backfill wiring, `--city all` (TDD)

**Files:**
- Modify: `src/rainmaker/cli.py` (`_backfill`, lines 123-144; import `save_accuracy`)
- Test: `tests/test_cli.py`

- [ ] **Step 1: Update and extend the CLI tests**

In `tests/test_cli.py`:

Update `test_backfill_fits_and_saves_calibration` to the new contract:

```python
def test_backfill_fits_and_saves_calibration_and_accuracy(monkeypatch, tmp_path, capsys):
    from rainmaker.probability.calibration import Accuracy

    cal = Calibration(
        station="KLGA", variable="TMAX", lead_time=1, bias=-2.0, spread_scale=1.1, n_samples=42
    )
    acc = Accuracy(n=42, mae_f=2.5, bias_f=-2.0)
    monkeypatch.setattr(cli, "run_backfill", lambda *a, **k: (cal, acc))
    monkeypatch.setattr(cli.httpx, "Client", lambda **kw: _DummyClient())
    db = tmp_path / "t.db"

    cli.main(["backfill", "--db", str(db), "--lead", "1"])

    out = capsys.readouterr().out
    assert "calibrated KLGA TMAX lead=1" in out
    assert "mae=2.50F" in out
    conn = connect(str(db))
    saved = load_calibration(conn, "KLGA", "TMAX", 1)
    row = conn.execute("SELECT * FROM forecast_accuracy").fetchone()
    conn.close()
    assert saved == cal
    assert (row["station"], row["city"], row["kind"]) == ("KLGA", "NYC", "backtest")
    assert row["n"] == 42
```

Add two new tests:

```python
def test_backfill_all_covers_every_city(monkeypatch, tmp_path):
    from rainmaker.config import STATIONS
    from rainmaker.probability.calibration import Accuracy

    def _fake(station, variable, lead, start, end, client):
        cal = Calibration(
            station=station.icao,
            variable=variable,
            lead_time=lead,
            bias=0.0,
            spread_scale=1.0,
            n_samples=42,
        )
        return cal, Accuracy(n=42, mae_f=2.0, bias_f=0.0)

    monkeypatch.setattr(cli, "run_backfill", _fake)
    monkeypatch.setattr(cli.httpx, "Client", lambda **kw: _DummyClient())
    db = tmp_path / "t.db"

    cli.main(["backfill", "--city", "all", "--db", str(db)])

    conn = connect(str(db))
    n = conn.execute("SELECT count(*) AS n FROM forecast_accuracy").fetchone()["n"]
    conn.close()
    assert n == len(STATIONS)


def test_backfill_exits_nonzero_when_all_cities_fail(monkeypatch, tmp_path, capsys):
    def _boom(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(cli, "run_backfill", _boom)
    monkeypatch.setattr(cli.httpx, "Client", lambda **kw: _DummyClient())

    with pytest.raises(SystemExit) as exc:
        cli.main(["backfill", "--city", "all", "--db", str(tmp_path / "t.db")])
    assert exc.value.code == 1
    assert "failed" in capsys.readouterr().err
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_cli.py -v -k backfill`
Expected: all three FAIL (the first on unpacking, the others on behavior that
does not exist yet).

- [ ] **Step 3: Implement**

In `src/rainmaker/cli.py`, extend the record import to
`from rainmaker.store.record import EvaluatedMarket, record_run, save_accuracy, save_calibration`
and replace `_backfill` (lines 123-144):

```python
def _backfill(city: str, variable: str, days: int, lead: int, db_path: str) -> None:
    cities = sorted(STATIONS) if city == "all" else [city]
    end = _today() - timedelta(days=1)  # actuals lag real-time; stop at yesterday
    start = end - timedelta(days=days)
    if "://" not in db_path:  # a Postgres DSN has no local parent dir to create
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    client = httpx.Client(headers={"User-Agent": NWS_USER_AGENT}, timeout=60.0)
    succeeded = 0
    try:
        init_schema(conn)
        for name in cities:
            station = STATIONS[name]
            try:
                cal, acc = run_backfill(station, variable, lead, start, end, client)
            except (httpx.HTTPError, ValueError) as exc:
                print(f"{name}: backfill failed: {exc}", file=sys.stderr)
                continue
            now = _now_iso()
            save_calibration(conn, cal, updated_at=now)
            save_accuracy(
                conn,
                station=cal.station,
                city=station.city,
                variable=cal.variable,
                lead_time=cal.lead_time,
                kind="backtest",
                accuracy=acc,
                updated_at=now,
            )
            succeeded += 1
            print(
                f"calibrated {cal.station} {cal.variable} lead={cal.lead_time}: "
                f"bias={cal.bias:+.2f}F spread_scale={cal.spread_scale:.2f} "
                f"mae={acc.mae_f:.2f}F n={cal.n_samples} -> {db_path}"
            )
    finally:
        client.close()
        conn.close()
    if succeeded == 0:
        raise SystemExit(1)
```

Notes:
- `ValueError` is caught alongside `httpx.HTTPError` so a city with zero pairs
  (NCEI gap) is skipped, not fatal.
- Behavior change for a single named city: a fetch failure now prints to
  stderr and exits 1 instead of dumping a traceback. Same exit-code contract
  either way: exit 0 if at least one city succeeded, 1 if all failed.
- Also update the `--city` help text in `main()`:
  `backfill.add_argument("--city", default="NYC", help="city key from the station registry, or 'all'")`

- [ ] **Step 4: Run the suite**

Run: `uv run pytest`
Expected: all PASS (including the Task 3 fallout, now fixed).

- [ ] **Step 5: Commit**

```bash
git add src/rainmaker/backfill.py tests/test_backfill.py src/rainmaker/cli.py tests/test_cli.py
git commit -m "feat: persist backtest accuracy from backfill, add --city all

The pairs are already built for the calibration fit; measuring MAE and
bias on them costs nothing. The dashboard needs every city populated,
so one command beats eleven; per-city failures are reported and skipped
so one NCEI gap does not abort the rest."
```

---

### Task 5: live accuracy from settled predictions (TDD)

**Files:**
- Modify: `src/rainmaker/tracking.py` (new `compute_live_accuracy`, extend `write_snapshot`)
- Test: `tests/test_tracking.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tracking.py` (add `import json` at the top; the existing
`_setup` stays untouched - its predictions have no `dist_params`, so they are
invisible to the live-accuracy query):

```python
def _setup_live(conn, city="NYC"):
    init_schema(conn)
    conn.execute(
        "INSERT INTO runs (id, started_at, status) VALUES (?, ?, ?)",
        ("r1", "2026-05-30T12:00:00+00:00", "ok"),
    )
    conn.execute(
        "INSERT INTO markets (id, city, variable, settlement_date) VALUES (?, ?, ?, ?)",
        ("m1", city, "TMAX", "2026-05-31"),
    )
    dist = json.dumps({"mu": 70.0, "sigma": 2.0, "n_sources": 2})
    for bucket in ("70-71°F", "72-73°F"):  # two buckets, same market -> one sample
        conn.execute(
            "INSERT INTO predictions "
            "(run_id, market_id, bucket, p_win, dist_params, edge, recommended, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("r1", "m1", bucket, 0.5, dist, 0.1, 1, "t"),
        )
    conn.execute(
        "INSERT INTO outcomes (market_id, actual_value, settled_at) VALUES (?, ?, ?)",
        ("m1", 73.0, "t"),
    )
    conn.commit()


def test_compute_live_accuracy_dedupes_buckets():
    from rainmaker.tracking import compute_live_accuracy

    conn = connect(":memory:")
    _setup_live(conn)
    rows = compute_live_accuracy(conn)
    conn.close()
    assert len(rows) == 1
    row = rows[0]
    assert (row["station"], row["city"], row["variable"], row["lead_time"]) == (
        "KLGA",
        "NYC",
        "TMAX",
        1,
    )
    acc = row["accuracy"]
    assert acc.n == 1  # two bucket rows collapse to one (run, market) sample
    assert acc.mae_f == pytest.approx(3.0)  # |70 - 73|
    assert acc.bias_f == pytest.approx(-3.0)  # forecast ran cold


def test_compute_live_accuracy_skips_unknown_city():
    from rainmaker.tracking import compute_live_accuracy

    conn = connect(":memory:")
    _setup_live(conn, city="Gotham")
    rows = compute_live_accuracy(conn)
    conn.close()
    assert rows == []


def test_compute_live_accuracy_empty_when_nothing_settled():
    from rainmaker.tracking import compute_live_accuracy

    conn = connect(":memory:")
    init_schema(conn)
    assert compute_live_accuracy(conn) == []
    conn.close()


def test_write_snapshot_writes_live_accuracy_rows():
    from rainmaker.tracking import write_snapshot

    conn = connect(":memory:")
    _setup_live(conn)
    write_snapshot(conn, "2026-06-04", "t")
    row = conn.execute("SELECT * FROM forecast_accuracy WHERE kind = 'live'").fetchone()
    conn.close()
    assert row is not None
    assert row["station"] == "KLGA"
    assert row["mae_f"] == pytest.approx(3.0)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_tracking.py -v -k live`
Expected: FAIL with `ImportError: cannot import name 'compute_live_accuracy'`
(the snapshot test fails on the missing row).

- [ ] **Step 3: Implement**

In `src/rainmaker/tracking.py`, add imports:

```python
import json
from collections import defaultdict
from datetime import date

from rainmaker.config import STATIONS
from rainmaker.probability.calibration import CalibrationPair, compute_accuracy
from rainmaker.store.record import save_accuracy
```

Add after `compute_calibration`:

```python
def compute_live_accuracy(conn: Conn) -> list[dict[str, Any]]:
    """Degrees-space accuracy of the bot's own forecasts over settled markets.

    One sample per (run, market): the predicted mu against the settled actual,
    grouped per (station, variable, lead). DISTINCT collapses the per-bucket
    prediction rows, which share one dist_params. Rows with an unknown city or
    no usable mu/sigma are skipped.
    """
    rows = conn.execute(
        "SELECT DISTINCT p.run_id AS run_id, p.market_id AS market_id, "
        "p.dist_params AS dist_params, m.city AS city, m.variable AS variable, "
        "m.settlement_date AS settlement_date, r.started_at AS started_at, "
        "o.actual_value AS actual_value "
        "FROM predictions p "
        "JOIN outcomes o ON o.market_id = p.market_id "
        "JOIN markets m ON m.id = p.market_id "
        "JOIN runs r ON r.id = p.run_id "
        "WHERE p.dist_params IS NOT NULL"
    ).fetchall()
    groups: dict[tuple[str, str, str, int], list[CalibrationPair]] = defaultdict(list)
    for r in (dict(row) for row in rows):
        station = STATIONS.get(r["city"])
        if station is None:
            continue
        params = json.loads(r["dist_params"])
        mu, sigma = params.get("mu"), params.get("sigma")
        if mu is None or sigma is None or sigma <= 0:
            continue
        lead = (
            date.fromisoformat(r["settlement_date"])
            - date.fromisoformat(r["started_at"][:10])
        ).days
        key = (station.icao, r["city"], r["variable"], lead)
        groups[key].append(CalibrationPair(mu=mu, sigma=sigma, actual=r["actual_value"]))
    return [
        {
            "station": station,
            "city": city,
            "variable": variable,
            "lead_time": lead,
            "accuracy": compute_accuracy(pairs),
        }
        for (station, city, variable, lead), pairs in sorted(groups.items())
    ]
```

Extend `write_snapshot`: after the existing snapshot upsert and before
`conn.commit()`, add:

```python
    for row in compute_live_accuracy(conn):
        save_accuracy(
            conn,
            station=row["station"],
            city=row["city"],
            variable=row["variable"],
            lead_time=row["lead_time"],
            kind="live",
            accuracy=row["accuracy"],
            updated_at=created_at,
        )
```

(`save_accuracy` commits internally, mirroring `save_calibration`; the final
`conn.commit()` in `write_snapshot` stays as the snapshot row's commit.)

- [ ] **Step 4: Run the suite**

Run: `uv run pytest tests/test_tracking.py -v` then `uv run pytest`
Expected: all PASS. The pre-existing snapshot tests stay green because
`_setup` predictions carry no `dist_params`.

- [ ] **Step 5: Commit**

```bash
git add src/rainmaker/tracking.py tests/test_tracking.py
git commit -m "feat: write live forecast accuracy at snapshot time

Settled predictions already store the forecast mu; comparing it to the
settled actual measures the bot's real pipeline in degrees, the number
the backtest only approximates. Snapshot runs daily, so the live rows
grow without workflow changes."
```

---

### Task 6: dashboard accuracy section

**Files:**
- Modify: `dashboard/app/page.tsx`

Heed `dashboard/AGENTS.md`: this Next.js version may differ from your training
data; check `node_modules/next/dist/docs/` if anything surprises you. This
change stays inside the existing server-component pattern.

- [ ] **Step 1: Extend the data fetch**

In `getData()` in `dashboard/app/page.tsx`, after the `tracking_snapshot`
query, add:

```tsx
  const { data: accRows } = await db
    .from("forecast_accuracy")
    .select("city, lead_time, kind, n, mae_f, bias_f")
    .order("city")
    .order("lead_time");

  type AccCell = { n: number; mae: number; bias: number } | null;
  type AccLine = { city: string; lead: number; backtest: AccCell; live: AccCell };
  const accMap = new Map<string, AccLine>();
  for (const r of accRows ?? []) {
    const key = `${r.city}|${r.lead_time}`;
    const line = accMap.get(key) ?? {
      city: r.city as string,
      lead: r.lead_time as number,
      backtest: null,
      live: null,
    };
    const cell = { n: r.n as number, mae: r.mae_f as number, bias: r.bias_f as number };
    if (r.kind === "backtest") line.backtest = cell;
    else line.live = cell;
    accMap.set(key, line);
  }
  const accuracy = [...accMap.values()];
```

and return it: `return { bets, snap, accuracy };` (adjust the destructuring in
`Page` to `const { bets, snap, accuracy } = await getData();`).

- [ ] **Step 2: Render the section**

Add helpers next to the existing `pct`:

```tsx
function degDelta(f: number) {
  return `${f.toFixed(1)}°F (${((f * 5) / 9).toFixed(1)}°C)`;
}

function accCell(c: { n: number; mae: number; bias: number } | null) {
  if (!c) return "–";
  const sign = c.bias >= 0 ? "+" : "";
  return `${degDelta(c.mae)}, bias ${sign}${degDelta(c.bias)}, n=${c.n}`;
}
```

Add the section between "Performance" and the snapshot footer:

```tsx
      <h2 className="mt-6 text-lg font-semibold">Forecast accuracy</h2>
      {accuracy.length === 0 ? (
        <p className="text-gray-500">No accuracy data yet.</p>
      ) : (
        <table className="mt-2 w-full text-sm">
          <thead>
            <tr className="text-left text-gray-500">
              <th>City</th>
              <th>Lead</th>
              <th>Backtest MAE</th>
              <th>Live MAE</th>
            </tr>
          </thead>
          <tbody>
            {accuracy.map((a, i) => (
              <tr key={i} className="border-t">
                <td>{a.city}</td>
                <td>{a.lead}d</td>
                <td>{accCell(a.backtest)}</td>
                <td>{accCell(a.live)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
```

- [ ] **Step 3: Verify the build**

Run: `cd dashboard && npm run build`
Expected: build succeeds with no type errors.

- [ ] **Step 4: Commit**

```bash
git add dashboard/app/page.tsx
git commit -m "feat: dashboard section for forecast accuracy

Shows MAE and bias per city and lead, backtest next to live, in
degrees F with the Celsius delta alongside."
```

---

### Task 7: docs touch-up and full verification

**Files:**
- Modify: `CLAUDE.md` (toolchain line for backfill)
- Check: `docs/operations/` for backfill references (update only if a runbook
  documents the backfill invocation)

- [ ] **Step 1: Update the backfill command doc**

In `CLAUDE.md`, the toolchain line
`- Backfill: \`uv run rainmaker backfill --city <X>\` (fit a calibration cell from history)`
becomes:

```
- Backfill: `uv run rainmaker backfill --city <X>` (fit a calibration cell and
  backtest accuracy from history; `--city all` covers every city)
```

Run `grep -rn "backfill" docs/operations/` and apply the same correction to
any runbook line that documents the command.

- [ ] **Step 2: Full verification**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check . && uv run mypy src`
Expected: everything green.
Run: `cd dashboard && npm run build`
Expected: build succeeds.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md docs/operations/
git commit -m "docs: document backfill accuracy output and --city all"
```

---

## Verification (whole plan)

- `uv run pytest` green, including golden e2e (untouched by this branch).
- `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src` clean.
- `npm run build` in `dashboard/` clean.
- Manual sanity (optional, operator): `uv run rainmaker backfill --city all`
  against local SQLite populates 11 backtest rows; the dashboard section
  renders them once the same command runs against Supabase.
