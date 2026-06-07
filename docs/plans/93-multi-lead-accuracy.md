# Multi-lead backfill accuracy (1-3d) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fill the dashboard's `bt` (backtest) forecast-accuracy columns at leads 2 and 3, not just lead 1, so the accuracy table covers the 1-3d horizons where the betting edge lives (#93, relates to #58/#81/#89).

**Architecture:** Approach A (keep lead 1 unchanged, add higher leads). Lead 1 keeps the existing `run_backfill` path (historical-forecast multi-model daily -> Gaussian -> fits calibration **and** accuracy). For leads >= 2 a new accuracy-only path uses Open-Meteo's Previous Runs API: hourly `temperature_2m_previous_dayN` per model, reduced to the daily max (TMAX) / min (TMIN) per local day, averaged across `OPENMETEO_MODELS`, scored against the NCEI actual, saved as a `forecast_accuracy` row with `kind="backtest"` per lead. No calibration fit at higher leads (the live system learns those cells over time; previous-runs gives a clean point forecast, not a per-model spread we trust). `compute_accuracy` uses only the point forecast, so no sigma is needed. The `forecast_accuracy` primary key already includes `lead_time`, so there is no schema migration; the dashboard already groups accuracy by `lead_time`, so there is no dashboard change.

**Tech Stack:** Python 3.11, httpx, pydantic, numpy; pytest + pytest-httpx (fixture-mocked HTTP, never live).

**Key API fact (verified against the live endpoint during design):**
- Previous Runs API base URL: `https://previous-runs-api.open-meteo.com/v1/forecast`.
- `previous_dayN` is valid only on **hourly** variables, not daily. So a lead-N daily extreme is `max`/`min` of the hourly `temperature_2m_previous_dayN` series grouped by local day.
- `&models=` composes with hourly `previous_dayN`; keys return as `temperature_2m_previous_day{N}_{model}`.
- `start_date`/`end_date` bound the **valid** time (the day forecast). One request carries `previous_day1,2,3` together. Archive depth is back to Jan 2024, comfortably inside the 60-day default window.

**Known approximation (by design, Approach A):** lead 1's daily extreme is the model's native daily max/min; leads >= 2 use max/min of hourly. The `backfill` module is already an explicit approximation (see its docstring), so this residual method difference between lead 1 and leads >= 2 is acceptable and noted.

---

### Task 1: Previous Runs fetch + daily reduction

Add `fetch_historical_point_forecasts` to `backfill.py`: per-lead, per-date multi-model-mean daily extreme from the Previous Runs API.

**Files:**
- Modify: `src/rainmaker/backfill.py`
- Create: `tests/fixtures/openmeteo_previous_runs_klga.json`
- Test: `tests/test_backfill.py`

- [ ] **Step 1: Create the fixture**

`tests/fixtures/openmeteo_previous_runs_klga.json` (two local days, two leads, two of the five models; three hours/day so the max/min reduction is exercised):

```json
{
  "hourly": {
    "time": [
      "2026-03-01T00:00", "2026-03-01T12:00", "2026-03-01T18:00",
      "2026-03-02T00:00", "2026-03-02T12:00", "2026-03-02T18:00"
    ],
    "temperature_2m_previous_day2_gfs_seamless": [40.0, 50.0, 45.0, 30.0, 38.0, 35.0],
    "temperature_2m_previous_day2_ecmwf_ifs025": [42.0, 48.0, 44.0, 31.0, 36.0, 34.0],
    "temperature_2m_previous_day3_gfs_seamless": [38.0, 46.0, 43.0, 28.0, 34.0, 33.0],
    "temperature_2m_previous_day3_ecmwf_ifs025": [40.0, 44.0, 42.0, 29.0, 33.0, 32.0]
  }
}
```

Expected TMAX daily-max reductions, then mean across the two present models:
- lead 2: 2026-03-01 = mean(max(40,50,45)=50, max(42,48,44)=48) = 49.0; 2026-03-02 = mean(38, 36) = 37.0
- lead 3: 2026-03-01 = mean(46, 44) = 45.0; 2026-03-02 = mean(34, 33) = 33.5

- [ ] **Step 2: Write the failing test**

Add to `tests/test_backfill.py` (extend the existing import from `rainmaker.backfill` to add `PREVIOUS_RUNS_URL` and `fetch_historical_point_forecasts`):

```python
def _previous_runs_fixture() -> dict[str, Any]:
    return json.loads((FIXTURES / "openmeteo_previous_runs_klga.json").read_text())


def test_fetch_historical_point_forecasts_reduces_hourly_to_daily_mean(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(re.escape(PREVIOUS_RUNS_URL)), json=_previous_runs_fixture()
    )
    with httpx.Client() as client:
        point = fetch_historical_point_forecasts(
            KLGA, (2, 3), date(2026, 3, 1), date(2026, 3, 2), client
        )
    assert point[2] == {date(2026, 3, 1): pytest.approx(49.0), date(2026, 3, 2): pytest.approx(37.0)}
    assert point[3] == {date(2026, 3, 1): pytest.approx(45.0), date(2026, 3, 2): pytest.approx(33.5)}
    req = httpx_mock.get_requests()[0]
    assert "hourly=temperature_2m_previous_day2" in str(req.url)
    assert "previous_day3" in str(req.url)
    assert "models=" in str(req.url)


def test_fetch_historical_point_forecasts_uses_min_for_tmin(httpx_mock):
    data = {
        "hourly": {
            "time": ["2026-03-01T06:00", "2026-03-01T12:00"],
            "temperature_2m_previous_day2_gfs_seamless": [30.0, 41.0],
            "temperature_2m_previous_day2_ecmwf_ifs025": [32.0, 39.0],
        }
    }
    httpx_mock.add_response(url=re.compile(re.escape(PREVIOUS_RUNS_URL)), json=data)
    with httpx.Client() as client:
        point = fetch_historical_point_forecasts(
            KLGA, (2,), date(2026, 3, 1), date(2026, 3, 1), client, "TMIN"
        )
    # min reduction: gfs min 30, ecmwf min 32 -> mean 31.0
    assert point[2] == {date(2026, 3, 1): pytest.approx(31.0)}
    assert "daily=" not in str(httpx_mock.get_requests()[0].url)
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_backfill.py::test_fetch_historical_point_forecasts_reduces_hourly_to_daily_mean tests/test_backfill.py::test_fetch_historical_point_forecasts_uses_min_for_tmin -v`
Expected: FAIL with `ImportError` / `cannot import name 'PREVIOUS_RUNS_URL'` (and `fetch_historical_point_forecasts`).

- [ ] **Step 4: Implement**

In `src/rainmaker/backfill.py`, add the URL constant next to the existing ones (after `HISTORICAL_FORECAST_URL`):

```python
PREVIOUS_RUNS_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
```

Add the fetch function (place it after `fetch_historical_forecasts`):

```python
def fetch_historical_point_forecasts(
    station: Station,
    leads: tuple[int, ...],
    start: date,
    end: date,
    client: httpx.Client,
    variable: str = "TMAX",
) -> dict[int, dict[date, float]]:
    """Per-lead, per-date multi-model-mean daily extreme from the Previous Runs API.

    Each value is the daily max (TMAX) or min (TMIN) of the hourly temperature the
    models forecast `lead` days before the valid day, averaged across the models
    that reported it. `previous_dayN` is an hourly-only suffix, so the daily extreme
    is reduced here. Raises on HTTP error.
    """
    fields = [f"temperature_2m_previous_day{lead}" for lead in leads]
    resp = client.get(
        PREVIOUS_RUNS_URL,
        params={
            "latitude": str(station.lat),
            "longitude": str(station.lon),
            "hourly": ",".join(fields),
            "temperature_unit": "fahrenheit",
            "timezone": station.timezone,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "models": ",".join(OPENMETEO_MODELS),
        },
    )
    resp.raise_for_status()
    hourly: dict[str, Any] = resp.json()["hourly"]
    times = hourly["time"]
    reduce = max if variable == "TMAX" else min
    out: dict[int, dict[date, float]] = {}
    for lead in leads:
        per_model_daily: dict[date, list[float]] = {}
        for model in OPENMETEO_MODELS:
            values = hourly.get(f"temperature_2m_previous_day{lead}_{model}")
            if not values:
                continue  # this model did not report at this lead
            by_day: dict[date, list[float]] = {}
            for iso, value in zip(times, values):
                if value is None:
                    continue
                by_day.setdefault(date.fromisoformat(iso[:10]), []).append(value)
            for day, hours in by_day.items():
                per_model_daily.setdefault(day, []).append(reduce(hours))
        out[lead] = {
            day: statistics.fmean(extremes) for day, extremes in per_model_daily.items()
        }
    return out
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_backfill.py::test_fetch_historical_point_forecasts_reduces_hourly_to_daily_mean tests/test_backfill.py::test_fetch_historical_point_forecasts_uses_min_for_tmin -v`
Expected: PASS (2 passed).

- [ ] **Step 6: Commit**

```bash
git add src/rainmaker/backfill.py tests/test_backfill.py tests/fixtures/openmeteo_previous_runs_klga.json
git commit -m "feat: fetch lead-N point forecasts from Open-Meteo Previous Runs API"
```

---

### Task 2: Per-lead accuracy helper

Add `run_backfill_accuracy`: join the per-lead point forecasts to NCEI actuals and score each lead. Reuses `build_pairs` and `compute_accuracy`.

**Files:**
- Modify: `src/rainmaker/backfill.py`
- Test: `tests/test_backfill.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_backfill.py` (extend the `rainmaker.backfill` import to add `run_backfill_accuracy`). `ncei_actuals_klga.json` already provides 2026-03-01 = 43.0 and 2026-03-02 = 34.0.

```python
def test_run_backfill_accuracy_scores_each_lead(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(re.escape(PREVIOUS_RUNS_URL)), json=_previous_runs_fixture()
    )
    httpx_mock.add_response(url=re.compile(re.escape(NCEI_URL)), json=_actuals_fixture())
    with httpx.Client() as client:
        accs = run_backfill_accuracy(
            KLGA, "TMAX", (2, 3), date(2026, 3, 1), date(2026, 3, 2), client
        )
    assert set(accs) == {2, 3}
    # lead 2: mu 49.0 vs 43 (+6), 37.0 vs 34 (+3) -> bias 4.5, mae 4.5
    assert accs[2].n == 2
    assert accs[2].bias_f == pytest.approx(4.5)
    assert accs[2].mae_f == pytest.approx(4.5)
    # lead 3: mu 45.0 vs 43 (+2), 33.5 vs 34 (-0.5) -> bias 0.75, mae 1.25
    assert accs[3].bias_f == pytest.approx(0.75)
    assert accs[3].mae_f == pytest.approx(1.25)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_backfill.py::test_run_backfill_accuracy_scores_each_lead -v`
Expected: FAIL with `cannot import name 'run_backfill_accuracy'`.

- [ ] **Step 3: Implement**

In `src/rainmaker/backfill.py`, add after `run_backfill` (the existing `Accuracy`, `build_pairs`, `compute_accuracy`, and `Gaussian` imports are already present):

```python
def run_backfill_accuracy(
    station: Station,
    variable: str,
    leads: tuple[int, ...],
    start: date,
    end: date,
    client: httpx.Client,
) -> dict[int, Accuracy]:
    """Per-lead forecast accuracy (mae/bias) from the Previous Runs API vs NCEI actuals.

    Accuracy needs only the point forecast, so each per-date mean is wrapped in a
    placeholder-sigma Gaussian to reuse build_pairs/compute_accuracy. Leads with no
    overlapping actual are omitted.
    """
    point = fetch_historical_point_forecasts(station, leads, start, end, client, variable)
    actuals = fetch_actuals(station.ghcnd_id, start, end, client, variable)
    out: dict[int, Accuracy] = {}
    for lead in leads:
        forecasts = {day: Gaussian(mu=mu, sigma=1.0) for day, mu in point[lead].items()}
        pairs = build_pairs(forecasts, actuals)
        if pairs:
            out[lead] = compute_accuracy(pairs)
    return out
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_backfill.py::test_run_backfill_accuracy_scores_each_lead -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/rainmaker/backfill.py tests/test_backfill.py
git commit -m "feat: score per-lead backfill accuracy against NCEI actuals"
```

---

### Task 3: Wire the CLI `--leads`

Swap `backfill --lead` (singular) for `--leads` (default `1,2,3`); lead 1 keeps the existing calibration+accuracy path, higher leads save accuracy-only rows.

**Files:**
- Modify: `src/rainmaker/cli.py` (the `_backfill` function ~184-226, the `backfill` arg parser ~385-396, and the dispatch ~451-452)
- Test: `tests/test_backfill.py`

- [ ] **Step 1: Write the failing test**

This test drives the CLI `_backfill` end to end against a temp SQLite db and asserts three backtest rows land (lead 1 via the existing path, leads 2-3 via the new path). Add to `tests/test_backfill.py` (add imports: `from rainmaker.cli import _backfill` and `from rainmaker.store.db import connect`):

```python
def test_backfill_cli_saves_a_backtest_row_per_lead(httpx_mock, tmp_path, monkeypatch):
    import rainmaker.cli as cli

    httpx_mock.add_response(
        url=re.compile(re.escape(HISTORICAL_FORECAST_URL)), json=_hist_fixture()
    )
    httpx_mock.add_response(
        url=re.compile(re.escape(PREVIOUS_RUNS_URL)), json=_previous_runs_fixture()
    )
    httpx_mock.add_response(url=re.compile(re.escape(NCEI_URL)), json=_actuals_fixture())
    monkeypatch.setattr(cli, "_today", lambda: date(2026, 3, 6))
    db = str(tmp_path / "t.db")

    _backfill("NYC", "TMAX", 5, (1, 2, 3), db)

    conn = connect(db)
    rows = conn.execute(
        "SELECT lead_time, kind, n FROM forecast_accuracy "
        "WHERE station = 'KLGA' AND variable = 'TMAX' ORDER BY lead_time"
    ).fetchall()
    conn.close()
    leads = sorted(r[0] for r in rows)
    assert leads == [1, 2, 3]
    assert all(r[1] == "backtest" for r in rows)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_backfill.py::test_backfill_cli_saves_a_backtest_row_per_lead -v`
Expected: FAIL with `TypeError` (the current `_backfill` takes `lead: int`, not `leads: tuple`).

- [ ] **Step 3: Implement the CLI change**

In `src/rainmaker/cli.py`, replace the `_backfill` function body (currently lines ~184-226) with:

```python
def _backfill(city: str, variable: str, days: int, leads: tuple[int, ...], db_path: str) -> None:
    cities = sorted(STATIONS) if city == "all" else [city]
    end = _today() - timedelta(days=1)  # actuals lag real-time; stop at yesterday
    start = end - timedelta(days=days)
    if "://" not in db_path:  # a Postgres DSN has no local parent dir to create
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    client = httpx.Client(headers={"User-Agent": NWS_USER_AGENT}, timeout=60.0)
    label = _db_label(db_path)
    succeeded = 0
    try:
        init_schema(conn)
        for name in cities:
            station = STATIONS[name]
            now = _now_iso()
            city_ok = False
            if 1 in leads:  # lead 1 keeps the calibration + accuracy fit
                try:
                    cal, acc = run_backfill(station, variable, 1, start, end, client)
                except (httpx.HTTPError, ValueError) as exc:
                    if isinstance(exc, ValidationError):
                        raise  # schema bug, not a data gap; fail loud
                    print(f"{name}: backfill failed: {exc}", file=sys.stderr)
                else:
                    save_calibration(conn, cal, updated_at=now)
                    save_accuracy(
                        conn, station=cal.station, city=station.city, variable=cal.variable,
                        lead_time=cal.lead_time, kind="backtest", accuracy=acc, updated_at=now,
                    )
                    print(
                        f"calibrated {cal.station} {cal.variable} lead={cal.lead_time}: "
                        f"bias={cal.bias:+.2f}F spread_scale={cal.spread_scale:.2f} "
                        f"mae={acc.mae_f:.2f}F n={cal.n_samples} -> {label}"
                    )
                    city_ok = True
            higher = tuple(lead for lead in leads if lead != 1)
            if higher:  # higher leads are accuracy-only (no calibration fit)
                try:
                    accs = run_backfill_accuracy(station, variable, higher, start, end, client)
                except (httpx.HTTPError, ValueError) as exc:
                    if isinstance(exc, ValidationError):
                        raise
                    print(f"{name}: accuracy backfill failed: {exc}", file=sys.stderr)
                else:
                    for lead, acc in sorted(accs.items()):
                        save_accuracy(
                            conn, station=station.icao, city=station.city, variable=variable,
                            lead_time=lead, kind="backtest", accuracy=acc, updated_at=now,
                        )
                        print(
                            f"accuracy {station.icao} {variable} lead={lead}: "
                            f"mae={acc.mae_f:.2f}F bias={acc.bias_f:+.2f}F n={acc.n} -> {label}"
                        )
                        city_ok = True
            if city_ok:
                succeeded += 1
    finally:
        client.close()
        conn.close()
    if succeeded == 0:
        raise SystemExit(1)
```

Update the import near the top of `cli.py` (the existing `from rainmaker.backfill import run_backfill`) to:

```python
from rainmaker.backfill import run_backfill, run_backfill_accuracy
```

- [ ] **Step 4: Swap the CLI argument**

In `src/rainmaker/cli.py`, replace the backfill `--lead` argument (currently ~393-395):

```python
    backfill.add_argument(
        "--lead", type=int, default=1, help="forecast lead time the archive represents"
    )
```

with:

```python
    backfill.add_argument(
        "--leads",
        default="1,2,3",
        help="comma-separated leads in days; lead 1 fits calibration, higher leads are accuracy-only",
    )
```

And update the dispatch line (currently ~452):

```python
    elif args.command == "backfill":
        _backfill(args.city, args.variable, args.days, _parse_leads(args.leads), db)
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/test_backfill.py::test_backfill_cli_saves_a_backtest_row_per_lead -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/rainmaker/cli.py tests/test_backfill.py
git commit -m "feat: backfill accuracy at multiple leads via --leads (default 1,2,3)"
```

---

### Task 4: Full verification

**Files:** none (verification only).

- [ ] **Step 1: Run the whole backfill test module**

Run: `uv run pytest tests/test_backfill.py -v`
Expected: PASS (existing tests + the four new ones).

- [ ] **Step 2: Run the full suite incl. the golden e2e**

Run: `uv run pytest`
Expected: PASS. The golden e2e exercises the report pipeline, which this change does not touch, so it must stay green.

- [ ] **Step 3: Lint and type check**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy src`
Expected: clean. If `ruff format --check` flags the new code, run `uv run ruff format .` and re-commit.

- [ ] **Step 4: Confirm nothing else referenced the old `--lead`**

Run: `grep -rn -- "--lead\b" . --include='*.py' --include='*.yml' --include='*.md' | grep -v node_modules`
Expected: no remaining references to the singular `--lead` flag (the scheduled `.github/workflows/backfill.yml` calls `backfill --city all` with no lead flag, so it is unaffected and will start filling 2d/3d columns on the next run).

- [ ] **Step 5: Commit any formatting fixups**

```bash
git add -A && git commit -m "chore: ruff format multi-lead accuracy backfill" || echo "nothing to format"
```

---

## Self-review

- **Spec coverage:** issue asks to extend backfill to a set of leads and persist one backtest accuracy cell per lead so the 2d/3d `bt` columns fill -> Task 1 (lead-N point forecast), Task 2 (per-lead accuracy), Task 3 (`--leads` + per-lead save). Covered.
- **Out of scope (intentional):** no calibration fit at leads >= 2 (accuracy-only, per the approved design); no dashboard or schema change (both already lead-aware).
- **Type consistency:** `fetch_historical_point_forecasts(station, leads, start, end, client, variable) -> dict[int, dict[date, float]]`; `run_backfill_accuracy(...) -> dict[int, Accuracy]`; `_backfill(city, variable, days, leads, db_path)`. Names and signatures match across tasks.
