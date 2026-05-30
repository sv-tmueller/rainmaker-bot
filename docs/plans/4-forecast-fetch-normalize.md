# Phase 1: Forecast Fetch + Normalization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** For one target (NYC highest-temperature at station KLGA on a given local date), fetch forecasts from NWS and Open-Meteo and return a normalized, pooled set of forecast samples with per-source coverage.

**Architecture:** A `src/rainmaker` package. `config.py` holds a station registry and source config. Each forecast source (`nws.py`, `openmeteo.py`) splits a thin `fetch_raw` (httpx I/O) from a pure `parse(json, target)` so parsing is tested against saved fixtures. `aggregate.py` pools sources, handles per-source failure, drops stale samples, and records coverage. `cli.py` wires it into `rainmaker run`. The probability engine (Phase 2) consumes only the normalized `ForecastSample` list and never learns which API a value came from.

**Tech Stack:** Python 3.11+, uv (env/deps/lock), httpx, pydantic v2; pytest + pytest-httpx, ruff, mypy.

**Scope note:** Phase 1 implements the `TMAX` (highest temperature) path only and sources the target from the config station registry. `TMIN`, more cities, the Polymarket client, the probability/ranking engine, and persistence are later phases. Deps numpy/scipy/pandas are deliberately deferred to Phase 2.

**Fixtures:** Real API responses for KLGA were captured on 2026-05-30 and committed under `tests/fixtures/`. Tests assert against these static files and never hit the network. Known values for target date **2026-05-31** (index 1 in the Open-Meteo daily arrays):
- NWS daytime high: **76 F**; `updateTime` `2026-05-30T14:23:35+00:00`.
- Open-Meteo multi-model: gfs_seamless **74.6**, ecmwf_ifs025 **77.3**, icon_seamless **74.9**, gem_seamless **75.1**, meteofrance_seamless **73.8**.
- Open-Meteo GFS ensemble: **30** members; member01 **73.0**.

---

### Task 1: Project scaffold

**Files:**
- Create: `pyproject.toml`
- Create: `src/rainmaker/__init__.py` (empty)
- Create: `src/rainmaker/forecasts/__init__.py` (empty)
- Create: `tests/__init__.py` (empty)
- Create: `.gitignore`

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[project]
name = "rainmaker-bot"
version = "0.1.0"
description = "Advisory bot for US-city weather markets on Polymarket"
requires-python = ">=3.11"
dependencies = [
    "httpx>=0.27",
    "pydantic>=2.7",
]

[project.scripts]
rainmaker = "rainmaker.cli:main"

[dependency-groups]
dev = [
    "pytest>=8",
    "pytest-httpx>=0.30",
    "ruff>=0.5",
    "mypy>=1.10",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/rainmaker"]

[tool.ruff]
line-length = 100
src = ["src", "tests"]

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]

[tool.mypy]
python_version = "3.11"
strict = true
files = ["src"]

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
```

- [ ] **Step 2: Write `.gitignore`**

```gitignore
.venv/
__pycache__/
*.pyc
.pytest_cache/
.mypy_cache/
.ruff_cache/
dist/
*.egg-info/
```

- [ ] **Step 3: Create empty package/test init files**

Create `src/rainmaker/__init__.py`, `src/rainmaker/forecasts/__init__.py`, `tests/__init__.py` as empty files. (The `tests/fixtures/*.json` files are already committed on this branch.)

- [ ] **Step 4: Sync and verify the toolchain runs**

Run: `uv sync`
Expected: creates `.venv` and `uv.lock`, installs deps without error.

Run: `uv run pytest -q`
Expected: `no tests ran` (exit 5) or 0 collected, no import errors.

Run: `uv run ruff check .` and `uv run mypy src`
Expected: both pass (no files to check yet for mypy beyond empty package).

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock .gitignore src tests
git commit -m "build: scaffold rainmaker package with uv, ruff, mypy, pytest"
```

---

### Task 2: Config (station registry, Target)

**Files:**
- Create: `src/rainmaker/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py
from datetime import date

from rainmaker.config import STATIONS, build_target


def test_nyc_station_resolves_to_klga():
    s = STATIONS["NYC"]
    assert s.icao == "KLGA"
    assert s.timezone == "America/New_York"
    assert abs(s.lat - 40.7792) < 1e-6
    assert abs(s.lon - (-73.8803)) < 1e-6


def test_build_target():
    t = build_target("NYC", "TMAX", date(2026, 5, 31))
    assert t.station.icao == "KLGA"
    assert t.variable == "TMAX"
    assert t.local_date == date(2026, 5, 31)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'rainmaker.config'`.

- [ ] **Step 3: Write `src/rainmaker/config.py`**

```python
from datetime import date
from typing import Literal

from pydantic import BaseModel

Variable = Literal["TMAX", "TMIN"]


class Station(BaseModel):
    city: str
    icao: str
    name: str
    lat: float
    lon: float
    timezone: str
    wunderground_url: str


class Target(BaseModel):
    station: Station
    variable: Variable
    local_date: date


STATIONS: dict[str, Station] = {
    "NYC": Station(
        city="NYC",
        icao="KLGA",
        name="LaGuardia Airport",
        lat=40.7792,
        lon=-73.8803,
        timezone="America/New_York",
        wunderground_url="https://www.wunderground.com/history/daily/us/ny/new-york-city/KLGA",
    ),
}

# Source config
NWS_USER_AGENT = "rainmaker-bot (thomas.mueller@solvvision.de)"
OPENMETEO_MODELS = [
    "gfs_seamless",
    "ecmwf_ifs025",
    "icon_seamless",
    "gem_seamless",
    "meteofrance_seamless",
]
OPENMETEO_ENSEMBLE_MODELS = ["gfs_seamless", "ecmwf_ifs025", "icon_seamless"]
OPENMETEO_FORECAST_DAYS = 7
FRESHNESS_LIMIT_HOURS = 24


def build_target(city: str, variable: Variable, local_date: date) -> Target:
    return Target(station=STATIONS[city], variable=variable, local_date=local_date)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/rainmaker/config.py tests/test_config.py
git commit -m "feat: add station registry and Target config"
```

---

### Task 3: Normalized models and source protocol

**Files:**
- Create: `src/rainmaker/forecasts/base.py`
- Test: `tests/test_base.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_base.py
from datetime import date, datetime, timezone

from rainmaker.forecasts.base import ForecastSample, ForecastSet, SourceCoverage


def test_forecast_sample_construct():
    s = ForecastSample(
        source="nws",
        model="nws",
        member=None,
        station="KLGA",
        variable="TMAX",
        target_date=date(2026, 5, 31),
        lead_time_days=1,
        value_f=76.0,
        issued_at=datetime(2026, 5, 30, 14, 23, 35, tzinfo=timezone.utc),
    )
    assert s.value_f == 76.0
    assert s.member is None


def test_forecast_set_holds_samples_and_coverage():
    cov = SourceCoverage(source="nws", ok=True, n_samples=1)
    fs = ForecastSet(target=None, samples=[], coverage=[cov])
    assert fs.coverage[0].ok is True
    assert fs.coverage[0].error is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_base.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'rainmaker.forecasts.base'`.

- [ ] **Step 3: Write `src/rainmaker/forecasts/base.py`**

```python
from datetime import date, datetime
from typing import Protocol

from pydantic import BaseModel

from rainmaker.config import Target


class ForecastSample(BaseModel):
    source: str
    model: str
    member: int | None
    station: str
    variable: str
    target_date: date
    lead_time_days: int
    value_f: float
    issued_at: datetime | None


class SourceCoverage(BaseModel):
    source: str
    ok: bool
    n_samples: int
    error: str | None = None


class ForecastSet(BaseModel):
    target: Target | None
    samples: list[ForecastSample]
    coverage: list[SourceCoverage]


class ForecastSource(Protocol):
    name: str

    def fetch(self, target: Target) -> list[ForecastSample]: ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_base.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/rainmaker/forecasts/base.py tests/test_base.py
git commit -m "feat: add normalized ForecastSample/ForecastSet models and source protocol"
```

---

### Task 4: NWS parse (pure, against fixture)

**Files:**
- Create: `src/rainmaker/forecasts/nws.py`
- Test: `tests/test_nws.py`
- Uses: `tests/fixtures/nws_forecast_klga.json` (already committed)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_nws.py
import json
from datetime import date
from pathlib import Path

from rainmaker.config import build_target
from rainmaker.forecasts.nws import parse

FIXTURES = Path(__file__).parent / "fixtures"


def _forecast_fixture() -> dict:
    return json.loads((FIXTURES / "nws_forecast_klga.json").read_text())


def test_parse_returns_daytime_high_for_target_date():
    target = build_target("NYC", "TMAX", date(2026, 5, 31))
    samples = parse(_forecast_fixture(), target)
    assert len(samples) == 1
    s = samples[0]
    assert s.source == "nws"
    assert s.model == "nws"
    assert s.member is None
    assert s.station == "KLGA"
    assert s.variable == "TMAX"
    assert s.value_f == 76.0
    assert s.lead_time_days == 1
    assert s.issued_at is not None


def test_parse_returns_empty_when_date_absent():
    target = build_target("NYC", "TMAX", date(2030, 1, 1))
    assert parse(_forecast_fixture(), target) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_nws.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'rainmaker.forecasts.nws'`.

- [ ] **Step 3: Write `src/rainmaker/forecasts/nws.py` (parse only for now)**

```python
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from rainmaker.config import NWS_USER_AGENT, Target
from rainmaker.forecasts.base import ForecastSample

NWS_BASE = "https://api.weather.gov"


def parse(forecast_json: dict, target: Target) -> list[ForecastSample]:
    if target.variable != "TMAX":
        raise NotImplementedError("Phase 1 supports TMAX only")
    props = forecast_json["properties"]
    issued_at = datetime.fromisoformat(props["updateTime"])
    issued_local = issued_at.astimezone(ZoneInfo(target.station.timezone)).date()
    for period in props["periods"]:
        start = datetime.fromisoformat(period["startTime"])
        if period["isDaytime"] and start.date() == target.local_date:
            if period["temperatureUnit"] != "F":
                raise ValueError(f"expected Fahrenheit, got {period['temperatureUnit']}")
            return [
                ForecastSample(
                    source="nws",
                    model="nws",
                    member=None,
                    station=target.station.icao,
                    variable="TMAX",
                    target_date=target.local_date,
                    lead_time_days=(target.local_date - issued_local).days,
                    value_f=float(period["temperature"]),
                    issued_at=issued_at,
                )
            ]
    return []


def fetch_raw(target: Target, client: httpx.Client) -> dict:
    points = client.get(f"{NWS_BASE}/points/{target.station.lat},{target.station.lon}")
    points.raise_for_status()
    forecast_url = points.json()["properties"]["forecast"]
    forecast = client.get(forecast_url)
    forecast.raise_for_status()
    return forecast.json()


class NwsSource:
    name = "nws"

    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def fetch(self, target: Target) -> list[ForecastSample]:
        return parse(fetch_raw(target, self.client), target)
```

(`fetch_raw`/`NwsSource` are included now; they are exercised in Task 5. `NWS_USER_AGENT` import is used by the CLI in Task 8 to set the client header; it is fine to leave imported here only if referenced. To avoid an unused-import lint error, do NOT import `NWS_USER_AGENT` in this file - remove it from the import line; it is shown above by mistake. Correct import line: `from rainmaker.config import Target`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_nws.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Run ruff to confirm no unused import**

Run: `uv run ruff check src/rainmaker/forecasts/nws.py`
Expected: pass. If `NWS_USER_AGENT` is flagged unused, remove it from the import.

- [ ] **Step 6: Commit**

```bash
git add src/rainmaker/forecasts/nws.py tests/test_nws.py
git commit -m "feat: parse NWS daily high into normalized samples"
```

---

### Task 5: NWS fetch_raw (I/O, mocked)

**Files:**
- Modify: `tests/test_nws.py` (add fetch tests)

- [ ] **Step 1: Write the failing test (append to tests/test_nws.py)**

```python
import httpx
import pytest

from rainmaker.forecasts.nws import NwsSource


def test_fetch_calls_points_then_forecast_and_sets_user_agent(httpx_mock):
    points_body = {
        "properties": {"forecast": "https://api.weather.gov/gridpoints/OKX/37,46/forecast"}
    }
    httpx_mock.add_response(
        url="https://api.weather.gov/points/40.7792,-73.8803", json=points_body
    )
    httpx_mock.add_response(
        url="https://api.weather.gov/gridpoints/OKX/37,46/forecast",
        json=_forecast_fixture(),
    )
    client = httpx.Client(headers={"User-Agent": "rainmaker-bot (test)"})
    target = build_target("NYC", "TMAX", date(2026, 5, 31))
    samples = NwsSource(client).fetch(target)
    client.close()
    assert len(samples) == 1
    assert samples[0].value_f == 76.0
    requests = httpx_mock.get_requests()
    assert all(r.headers["User-Agent"] == "rainmaker-bot (test)" for r in requests)


def test_fetch_raises_on_http_error(httpx_mock):
    httpx_mock.add_response(
        url="https://api.weather.gov/points/40.7792,-73.8803", status_code=500
    )
    client = httpx.Client()
    target = build_target("NYC", "TMAX", date(2026, 5, 31))
    with pytest.raises(httpx.HTTPStatusError):
        NwsSource(client).fetch(target)
    client.close()
```

- [ ] **Step 2: Run test to verify it fails/passes**

Run: `uv run pytest tests/test_nws.py -v`
Expected: the two new tests PASS (fetch_raw/NwsSource were written in Task 4). If `pytest-httpx` is missing, `uv sync` first.

- [ ] **Step 3: Commit**

```bash
git add tests/test_nws.py
git commit -m "test: cover NWS fetch_raw I/O with mocked HTTP"
```

---

### Task 6: Open-Meteo multi-model parse (pure, against fixture)

**Files:**
- Create: `src/rainmaker/forecasts/openmeteo.py`
- Test: `tests/test_openmeteo.py`
- Uses: `tests/fixtures/openmeteo_multimodel_klga.json` (committed)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_openmeteo.py
import json
from datetime import date
from pathlib import Path

from rainmaker.config import build_target
from rainmaker.forecasts.openmeteo import parse_multimodel

FIXTURES = Path(__file__).parent / "fixtures"


def _multimodel_fixture() -> dict:
    return json.loads((FIXTURES / "openmeteo_multimodel_klga.json").read_text())


def test_parse_multimodel_returns_one_sample_per_model():
    target = build_target("NYC", "TMAX", date(2026, 5, 31))
    samples = parse_multimodel(_multimodel_fixture(), target)
    by_model = {s.model: s.value_f for s in samples}
    assert by_model == {
        "gfs_seamless": 74.6,
        "ecmwf_ifs025": 77.3,
        "icon_seamless": 74.9,
        "gem_seamless": 75.1,
        "meteofrance_seamless": 73.8,
    }
    for s in samples:
        assert s.source == "open-meteo"
        assert s.member is None
        assert s.station == "KLGA"
        assert s.lead_time_days == 1
        assert s.issued_at is None


def test_parse_multimodel_empty_when_date_absent():
    target = build_target("NYC", "TMAX", date(2030, 1, 1))
    assert parse_multimodel(_multimodel_fixture(), target) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_openmeteo.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'rainmaker.forecasts.openmeteo'`.

- [ ] **Step 3: Write `src/rainmaker/forecasts/openmeteo.py` (multi-model first)**

```python
import re

import httpx

from rainmaker.config import (
    OPENMETEO_ENSEMBLE_MODELS,
    OPENMETEO_FORECAST_DAYS,
    OPENMETEO_MODELS,
    Target,
)
from rainmaker.forecasts.base import ForecastSample

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
_MEMBER_RE = re.compile(r"temperature_2m_max_member(\d+)")


def _daily_field(variable: str) -> str:
    if variable != "TMAX":
        raise NotImplementedError("Phase 1 supports TMAX only")
    return "temperature_2m_max"


def _target_index(daily: dict, target: Target) -> int | None:
    iso = target.local_date.isoformat()
    times = daily["time"]
    return times.index(iso) if iso in times else None


def parse_multimodel(data: dict, target: Target) -> list[ForecastSample]:
    daily = data["daily"]
    idx = _target_index(daily, target)
    if idx is None:
        return []
    field = _daily_field(target.variable)
    out: list[ForecastSample] = []
    for model in OPENMETEO_MODELS:
        values = daily.get(f"{field}_{model}")
        if not values or values[idx] is None:
            continue
        out.append(
            ForecastSample(
                source="open-meteo",
                model=model,
                member=None,
                station=target.station.icao,
                variable=target.variable,
                target_date=target.local_date,
                lead_time_days=idx,
                value_f=float(values[idx]),
                issued_at=None,
            )
        )
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_openmeteo.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/rainmaker/forecasts/openmeteo.py tests/test_openmeteo.py
git commit -m "feat: parse Open-Meteo multi-model daily highs into samples"
```

---

### Task 7: Open-Meteo ensemble parse (pure, against fixture)

**Files:**
- Modify: `src/rainmaker/forecasts/openmeteo.py` (add `parse_ensemble`)
- Modify: `tests/test_openmeteo.py` (add ensemble tests)
- Uses: `tests/fixtures/openmeteo_ensemble_gfs_klga.json` (committed)

- [ ] **Step 1: Write the failing test (append to tests/test_openmeteo.py)**

```python
def _ensemble_fixture() -> dict:
    return json.loads((FIXTURES / "openmeteo_ensemble_gfs_klga.json").read_text())


def test_parse_ensemble_returns_one_sample_per_member():
    target = build_target("NYC", "TMAX", date(2026, 5, 31))
    from rainmaker.forecasts.openmeteo import parse_ensemble

    samples = parse_ensemble(_ensemble_fixture(), target, "gfs_seamless")
    assert len(samples) == 30
    members = {s.member for s in samples}
    assert members == set(range(1, 31))
    m1 = next(s for s in samples if s.member == 1)
    assert m1.value_f == 73.0
    assert m1.model == "gfs_seamless_ens"
    assert m1.source == "open-meteo"
    assert m1.lead_time_days == 1
    assert m1.issued_at is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_openmeteo.py -v`
Expected: FAIL with `ImportError: cannot import name 'parse_ensemble'`.

- [ ] **Step 3: Add `parse_ensemble` to `src/rainmaker/forecasts/openmeteo.py`**

```python
def parse_ensemble(data: dict, target: Target, ens_model: str) -> list[ForecastSample]:
    daily = data["daily"]
    idx = _target_index(daily, target)
    if idx is None:
        return []
    out: list[ForecastSample] = []
    for key, values in daily.items():
        match = _MEMBER_RE.fullmatch(key)
        if match is None or not values or values[idx] is None:
            continue
        out.append(
            ForecastSample(
                source="open-meteo",
                model=f"{ens_model}_ens",
                member=int(match.group(1)),
                station=target.station.icao,
                variable=target.variable,
                target_date=target.local_date,
                lead_time_days=idx,
                value_f=float(values[idx]),
                issued_at=None,
            )
        )
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_openmeteo.py -v`
Expected: PASS (4 passed total in this file).

- [ ] **Step 5: Commit**

```bash
git add src/rainmaker/forecasts/openmeteo.py tests/test_openmeteo.py
git commit -m "feat: parse Open-Meteo ensemble members into samples"
```

---

### Task 8: OpenMeteoSource fetch (I/O, mocked)

**Files:**
- Modify: `src/rainmaker/forecasts/openmeteo.py` (add `fetch_raw_*` + `OpenMeteoSource`)
- Modify: `tests/test_openmeteo.py` (add fetch test)

- [ ] **Step 1: Write the failing test (append to tests/test_openmeteo.py)**

```python
import httpx


def test_open_meteo_source_pools_multimodel_and_ensemble(httpx_mock):
    from rainmaker.forecasts.openmeteo import ENSEMBLE_URL, FORECAST_URL, OpenMeteoSource

    httpx_mock.add_response(url__startswith=FORECAST_URL, json=_multimodel_fixture())
    # one ensemble call per configured ensemble model; reuse the gfs fixture for each
    httpx_mock.add_response(url__startswith=ENSEMBLE_URL, json=_ensemble_fixture())

    client = httpx.Client()
    target = build_target("NYC", "TMAX", date(2026, 5, 31))
    samples = OpenMeteoSource(client).fetch(target)
    client.close()

    # 5 multi-model + (30 members * number of ensemble models)
    multimodel = [s for s in samples if s.member is None]
    ensemble = [s for s in samples if s.member is not None]
    assert len(multimodel) == 5
    assert len(ensemble) == 30 * 3  # OPENMETEO_ENSEMBLE_MODELS has 3 entries
```

Note: `httpx_mock` matches each queued response once per request. Because all three ensemble requests share the same `ENSEMBLE_URL` prefix, add the ensemble response three times (once per model) OR set `httpx_mock` to be reusable. Use the explicit form: add the ensemble response 3 times.

Adjust the test setup to:

```python
    httpx_mock.add_response(url__startswith=FORECAST_URL, json=_multimodel_fixture())
    for _ in range(3):
        httpx_mock.add_response(url__startswith=ENSEMBLE_URL, json=_ensemble_fixture())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_openmeteo.py -v`
Expected: FAIL with `ImportError: cannot import name 'OpenMeteoSource'`.

- [ ] **Step 3: Add fetch functions and `OpenMeteoSource` to `openmeteo.py`**

```python
def _common_params(target: Target) -> dict[str, str]:
    return {
        "latitude": str(target.station.lat),
        "longitude": str(target.station.lon),
        "daily": _daily_field(target.variable),
        "temperature_unit": "fahrenheit",
        "timezone": target.station.timezone,
        "forecast_days": str(OPENMETEO_FORECAST_DAYS),
    }


def fetch_raw_multimodel(target: Target, client: httpx.Client) -> dict:
    params = _common_params(target) | {"models": ",".join(OPENMETEO_MODELS)}
    resp = client.get(FORECAST_URL, params=params)
    resp.raise_for_status()
    return resp.json()


def fetch_raw_ensemble(target: Target, client: httpx.Client, ens_model: str) -> dict:
    params = _common_params(target) | {"models": ens_model}
    resp = client.get(ENSEMBLE_URL, params=params)
    resp.raise_for_status()
    return resp.json()


class OpenMeteoSource:
    name = "open-meteo"

    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def fetch(self, target: Target) -> list[ForecastSample]:
        samples = parse_multimodel(fetch_raw_multimodel(target, self.client), target)
        for ens_model in OPENMETEO_ENSEMBLE_MODELS:
            data = fetch_raw_ensemble(target, self.client, ens_model)
            samples.extend(parse_ensemble(data, target, ens_model))
        return samples
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_openmeteo.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/rainmaker/forecasts/openmeteo.py tests/test_openmeteo.py
git commit -m "feat: add Open-Meteo source fetch pooling multi-model and ensemble"
```

---

### Task 9: Aggregate (coverage + freshness)

**Files:**
- Create: `src/rainmaker/forecasts/aggregate.py`
- Test: `tests/test_aggregate.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_aggregate.py
from datetime import date, datetime, timedelta, timezone

from rainmaker.config import build_target
from rainmaker.forecasts.aggregate import aggregate
from rainmaker.forecasts.base import ForecastSample

TARGET = build_target("NYC", "TMAX", date(2026, 5, 31))
NOW = datetime(2026, 5, 30, 15, 0, tzinfo=timezone.utc)


def _sample(model: str, issued_at):
    return ForecastSample(
        source="x", model=model, member=None, station="KLGA", variable="TMAX",
        target_date=date(2026, 5, 31), lead_time_days=1, value_f=70.0, issued_at=issued_at,
    )


class _StubSource:
    def __init__(self, name, samples=None, error=None):
        self.name = name
        self._samples = samples or []
        self._error = error

    def fetch(self, target):
        if self._error:
            raise self._error
        return self._samples


def test_aggregate_pools_samples_and_records_ok_coverage():
    src = _StubSource("a", samples=[_sample("m1", NOW), _sample("m2", NOW)])
    fs = aggregate(TARGET, [src], now=NOW)
    assert len(fs.samples) == 2
    assert fs.coverage[0].source == "a"
    assert fs.coverage[0].ok is True
    assert fs.coverage[0].n_samples == 2


def test_aggregate_records_failure_and_continues():
    good = _StubSource("good", samples=[_sample("m1", NOW)])
    bad = _StubSource("bad", error=RuntimeError("source down"))
    fs = aggregate(TARGET, [good, bad], now=NOW)
    assert len(fs.samples) == 1
    cov = {c.source: c for c in fs.coverage}
    assert cov["good"].ok is True
    assert cov["bad"].ok is False
    assert "source down" in cov["bad"].error


def test_aggregate_drops_stale_samples_but_keeps_unknown_issue_time():
    stale = _sample("stale", NOW - timedelta(hours=48))
    fresh = _sample("fresh", NOW - timedelta(hours=1))
    unknown = _sample("unknown", None)
    src = _StubSource("a", samples=[stale, fresh, unknown])
    fs = aggregate(TARGET, [src], now=NOW, freshness_limit_hours=24)
    kept = {s.model for s in fs.samples}
    assert kept == {"fresh", "unknown"}
    assert fs.coverage[0].n_samples == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_aggregate.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'rainmaker.forecasts.aggregate'`.

- [ ] **Step 3: Write `src/rainmaker/forecasts/aggregate.py`**

```python
from datetime import datetime, timedelta, timezone

from rainmaker.config import FRESHNESS_LIMIT_HOURS, Target
from rainmaker.forecasts.base import ForecastSample, ForecastSet, ForecastSource, SourceCoverage


def _is_fresh(sample: ForecastSample, now: datetime, limit_hours: int) -> bool:
    if sample.issued_at is None:
        return True
    return (now - sample.issued_at) <= timedelta(hours=limit_hours)


def aggregate(
    target: Target,
    sources: list[ForecastSource],
    now: datetime | None = None,
    freshness_limit_hours: int = FRESHNESS_LIMIT_HOURS,
) -> ForecastSet:
    now = now or datetime.now(timezone.utc)
    samples: list[ForecastSample] = []
    coverage: list[SourceCoverage] = []
    for source in sources:
        try:
            fetched = source.fetch(target)
        except Exception as exc:  # noqa: BLE001 - one source failing must not abort the run
            coverage.append(
                SourceCoverage(source=source.name, ok=False, n_samples=0, error=str(exc))
            )
            continue
        fresh = [s for s in fetched if _is_fresh(s, now, freshness_limit_hours)]
        samples.extend(fresh)
        coverage.append(SourceCoverage(source=source.name, ok=True, n_samples=len(fresh)))
    return ForecastSet(target=target, samples=samples, coverage=coverage)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_aggregate.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/rainmaker/forecasts/aggregate.py tests/test_aggregate.py
git commit -m "feat: aggregate sources with coverage and freshness handling"
```

---

### Task 10: CLI wiring (`rainmaker run`)

**Files:**
- Create: `src/rainmaker/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli.py
from datetime import date

from rainmaker import cli
from rainmaker.config import build_target
from rainmaker.forecasts.base import ForecastSample, ForecastSet, SourceCoverage


def test_cli_run_prints_samples_and_coverage(monkeypatch, capsys):
    target = build_target("NYC", "TMAX", date(2026, 5, 31))
    fake = ForecastSet(
        target=target,
        samples=[
            ForecastSample(
                source="nws", model="nws", member=None, station="KLGA", variable="TMAX",
                target_date=date(2026, 5, 31), lead_time_days=1, value_f=76.0, issued_at=None,
            )
        ],
        coverage=[SourceCoverage(source="nws", ok=True, n_samples=1)],
    )

    def fake_aggregate(target, sources):
        return fake

    monkeypatch.setattr(cli, "aggregate", fake_aggregate)
    monkeypatch.setattr(cli.httpx, "Client", lambda **kw: _DummyClient())

    cli.main(["run", "--city", "NYC", "--variable", "TMAX", "--date", "2026-05-31"])
    out = capsys.readouterr().out
    assert "KLGA" in out
    assert "76.0" in out
    assert "nws" in out


class _DummyClient:
    def close(self):
        pass
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'rainmaker.cli'`.

- [ ] **Step 3: Write `src/rainmaker/cli.py`**

```python
import argparse
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from rainmaker.config import NWS_USER_AGENT, STATIONS, build_target
from rainmaker.forecasts.aggregate import aggregate
from rainmaker.forecasts.base import ForecastSet
from rainmaker.forecasts.nws import NwsSource
from rainmaker.forecasts.openmeteo import OpenMeteoSource


def _default_date(timezone_name: str) -> date:
    today_local = datetime.now(ZoneInfo(timezone_name)).date()
    return today_local + timedelta(days=1)


def _print_report(fs: ForecastSet) -> None:
    target = fs.target
    assert target is not None
    print(f"Target: {target.station.icao} {target.variable} {target.local_date}")
    print("Coverage:")
    for c in fs.coverage:
        status = "ok" if c.ok else f"FAILED ({c.error})"
        print(f"  {c.source:12} {status:30} samples={c.n_samples}")
    print(f"Samples ({len(fs.samples)}):")
    print(f"  {'source':12} {'model':24} {'member':>6} {'value_f':>8} {'lead':>4}")
    for s in sorted(fs.samples, key=lambda x: (x.source, x.model, x.member or 0)):
        member = "" if s.member is None else str(s.member)
        print(f"  {s.source:12} {s.model:24} {member:>6} {s.value_f:>8.1f} {s.lead_time_days:>4}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="rainmaker")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="fetch and normalize forecasts for one target")
    run.add_argument("--city", default="NYC")
    run.add_argument("--variable", default="TMAX")
    run.add_argument("--date", default=None, help="YYYY-MM-DD local; default tomorrow")
    args = parser.parse_args(argv)

    if args.command == "run":
        station = STATIONS[args.city]
        target_date = date.fromisoformat(args.date) if args.date else _default_date(station.timezone)
        target = build_target(args.city, args.variable, target_date)
        client = httpx.Client(headers={"User-Agent": NWS_USER_AGENT}, timeout=30.0)
        try:
            fs = aggregate(target, [NwsSource(client), OpenMeteoSource(client)])
        finally:
            client.close()
        _print_report(fs)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cli.py -v`
Expected: PASS (1 passed).

- [ ] **Step 5: Commit**

```bash
git add src/rainmaker/cli.py tests/test_cli.py
git commit -m "feat: add rainmaker run CLI printing normalized samples and coverage"
```

---

### Task 11: Full check suite, live smoke, docs

**Files:**
- Modify: `CLAUDE.md` (record toolchain commands and repo layout)

- [ ] **Step 1: Run the full check suite**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest -q`
Expected: all pass. Fix any ruff/mypy issues inline (e.g. unused imports, missing annotations) and re-run.

- [ ] **Step 2: Live end-to-end smoke (manual, not a test)**

Run: `uv run rainmaker run --city NYC --variable TMAX`
Expected: prints a Target line, a Coverage block showing `nws` and `open-meteo` ok, and a Samples table of real values. Row count is roughly 125: 1 NWS + 5 multi-model + ~119 ensemble. Ensemble member counts vary per model (gfs 30, ecmwf 50, icon 39), so do not assume a uniform count. This is the "end to end" deliverable. If a source is down, coverage shows it FAILED and the run still prints the rest.

- [ ] **Step 3: Record commands and layout in CLAUDE.md**

In the "Toolchain" section of `CLAUDE.md`, replace the "intended, once scaffolded" note with the actual commands:

```markdown
## Toolchain

Python 3.11+ managed with uv. Commands:

- Install: `uv sync`
- Run: `uv run rainmaker run` (Phase 1: NYC highest-temp; `--city`, `--variable`, `--date` optional)
- Test: `uv run pytest`
- Lint: `uv run ruff check .`  Format: `uv run ruff format .`
- Type check: `uv run mypy src`

Runtime deps: httpx, pydantic. (numpy/scipy/pandas arrive with the Phase 2 probability engine.)
API clients are tested against saved JSON fixtures in `tests/fixtures/`, never live endpoints.
```

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: record uv toolchain commands after Phase 1 scaffold"
```

- [ ] **Step 5: Push and mark PR #N ready**

```bash
git push -u origin feat/4-forecast-fetch-normalize
# open or update the draft PR (Closes #4); mark ready once checks are green
```

---

## Self-Review

**Spec coverage (issue #4 + spec "Forecast aggregation"/"Phase 1"):**
- ForecastSource protocol -> Task 3. `nws.py` -> Tasks 4-5. `openmeteo.py` multi-model + ensemble -> Tasks 6-8. Resolve settlement target (station/variable/date) -> `config.py` Task 2 + CLI Task 10. Normalize units/timezones -> NWS parse asserts F; Open-Meteo requests Fahrenheit + IANA timezone; local-date keying in both parses. Tests against fixtures, never live -> all parse tests use committed JSON; I/O tests use pytest-httpx. Initial scaffold -> Task 1. Error handling (source down -> proceed, record coverage; stale dropped) -> Task 9.
- Deferred and called out: TMIN, extra cities, Polymarket client, probability/ranking, persistence.

**Placeholder scan:** No TBD/TODO. Every code and test step shows complete code. The one inline correction (Task 4 unused `NWS_USER_AGENT` import) is explicit with the fix.

**Type consistency:** `ForecastSample` fields (source, model, member, station, variable, target_date, lead_time_days, value_f, issued_at) are used identically in Tasks 3,4,6,7,9,10. `SourceCoverage` (source, ok, n_samples, error) consistent in Tasks 3,9,10. Source classes expose `name` + `fetch(target)` (Tasks 4,8) matching the `ForecastSource` protocol (Task 3) and `aggregate` usage (Task 9). `parse`/`parse_multimodel`/`parse_ensemble` signatures match their tests.
