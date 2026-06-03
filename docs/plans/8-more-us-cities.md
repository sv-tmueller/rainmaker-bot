# More US cities (Phase 5 slice 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Widen the temperature-bucket pipeline from one US city to all eleven live US cities by adding verified station rows, and make discovery skip a single malformed market instead of aborting the run.

**Architecture:** Discovery is registry-driven: `discover_markets` keeps only events whose title city is a key in `STATIONS`, and `parse_market` re-verifies the resolution ICAO appears in each market's description. So adding a city is adding a correct `Station` row; the forecast engine, probability engine, ranking, and calibration generalize with no change. One robustness change: discovery skips-and-warns on an unparseable market.

**Tech Stack:** Python 3.11+, pydantic, httpx, pytest, pytest-httpx. NOAA NCEI GHCND ids (already verified against the live NCEI Access Data Service during planning).

---

## Verified station data (already confirmed against NCEI during planning)

Every `ghcnd_id` below returned TMAX data with a matching station name from
`https://www.ncei.noaa.gov/access/services/data/v1` on 2026-06-03. The three
trap stations (Dallas, Houston, Denver) were checked to resolve on the exact
station the market settles on, not the city's obvious main airport. Use these
values verbatim; do not re-guess them.

| city | icao | ghcnd_id | lat | lon | timezone |
|------|------|----------|-----|-----|----------|
| Miami | KMIA | USW00012839 | 25.7881 | -80.3169 | America/New_York |
| Chicago | KORD | USW00094846 | 41.9602 | -87.9316 | America/Chicago |
| Dallas | KDAL | USW00013960 | 32.8384 | -96.8358 | America/Chicago |
| Houston | KHOU | USW00012918 | 29.6459 | -95.2821 | America/Chicago |
| Los Angeles | KLAX | USW00023174 | 33.9382 | -118.3866 | America/Los_Angeles |
| San Francisco | KSFO | USW00023234 | 37.6196 | -122.3656 | America/Los_Angeles |
| Seattle | KSEA | USW00024233 | 47.4447 | -122.3144 | America/Los_Angeles |
| Austin | KAUS | USW00013904 | 30.1831 | -97.6799 | America/Chicago |
| Atlanta | KATL | USW00013874 | 33.6297 | -84.4422 | America/New_York |
| Denver | KBKF | USW00023036 | 39.7167 | -104.7500 | America/Denver |

---

## Task 1: Registry tests (failing first)

**Files:**
- Modify: `tests/test_config.py`

- [ ] **Step 1: Add the failing tests**

Add this import at the top of `tests/test_config.py` (below the existing `from datetime import date`):

```python
import zoneinfo
```

Append these tests to the end of `tests/test_config.py`:

```python
EXPECTED_CITIES = {
    "NYC",
    "Miami",
    "Chicago",
    "Dallas",
    "Houston",
    "Los Angeles",
    "San Francisco",
    "Seattle",
    "Austin",
    "Atlanta",
    "Denver",
}


def test_all_us_cities_present():
    assert set(STATIONS) == EXPECTED_CITIES


def test_every_station_is_valid():
    for key, s in STATIONS.items():
        assert s.city == key
        assert len(s.icao) == 4 and s.icao.startswith("K")
        assert s.name
        assert -90 <= s.lat <= 90
        assert -180 <= s.lon <= 180
        assert s.wunderground_url.startswith("https://")
        assert s.ghcnd_id.startswith("USW")
        zoneinfo.ZoneInfo(s.timezone)  # raises if the timezone is invalid


def test_trap_stations_resolve_to_the_right_airport():
    # the market settles on these stations, not the city's obvious main airport
    assert STATIONS["Dallas"].icao == "KDAL"  # Love Field, not DFW
    assert STATIONS["Houston"].icao == "KHOU"  # Hobby, not IAH
    assert STATIONS["Denver"].icao == "KBKF"  # Buckley SFB, not KDEN
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_config.py -q`
Expected: FAIL. `test_all_us_cities_present` fails because `set(STATIONS)` is `{"NYC"}`; the trap-station test raises `KeyError: 'Dallas'`.

---

## Task 2: Add the ten station rows

**Files:**
- Modify: `src/rainmaker/config.py` (the `STATIONS` dict)

- [ ] **Step 1: Add the rows**

In `src/rainmaker/config.py`, insert these ten entries into the `STATIONS` dict, immediately after the existing `"NYC": Station(...)` entry and before the closing `}`:

```python
    "Miami": Station(
        city="Miami",
        icao="KMIA",
        name="Miami Intl Airport",
        lat=25.7881,
        lon=-80.3169,
        timezone="America/New_York",
        wunderground_url="https://www.wunderground.com/history/daily/us/fl/miami/KMIA",
        ghcnd_id="USW00012839",
    ),
    "Chicago": Station(
        city="Chicago",
        icao="KORD",
        name="Chicago O'Hare Intl Airport",
        lat=41.9602,
        lon=-87.9316,
        timezone="America/Chicago",
        wunderground_url="https://www.wunderground.com/history/daily/us/il/chicago/KORD",
        ghcnd_id="USW00094846",
    ),
    "Dallas": Station(
        city="Dallas",
        icao="KDAL",
        name="Dallas Love Field",
        lat=32.8384,
        lon=-96.8358,
        timezone="America/Chicago",
        wunderground_url="https://www.wunderground.com/history/daily/us/tx/dallas/KDAL",
        ghcnd_id="USW00013960",
    ),
    "Houston": Station(
        city="Houston",
        icao="KHOU",
        name="Houston William P. Hobby Airport",
        lat=29.6459,
        lon=-95.2821,
        timezone="America/Chicago",
        wunderground_url="https://www.wunderground.com/history/daily/us/tx/houston/KHOU",
        ghcnd_id="USW00012918",
    ),
    "Los Angeles": Station(
        city="Los Angeles",
        icao="KLAX",
        name="Los Angeles Intl Airport",
        lat=33.9382,
        lon=-118.3866,
        timezone="America/Los_Angeles",
        wunderground_url="https://www.wunderground.com/history/daily/us/ca/los-angeles/KLAX",
        ghcnd_id="USW00023174",
    ),
    "San Francisco": Station(
        city="San Francisco",
        icao="KSFO",
        name="San Francisco Intl Airport",
        lat=37.6196,
        lon=-122.3656,
        timezone="America/Los_Angeles",
        wunderground_url="https://www.wunderground.com/history/daily/us/ca/san-francisco/KSFO",
        ghcnd_id="USW00023234",
    ),
    "Seattle": Station(
        city="Seattle",
        icao="KSEA",
        name="Seattle-Tacoma Intl Airport",
        lat=47.4447,
        lon=-122.3144,
        timezone="America/Los_Angeles",
        wunderground_url="https://www.wunderground.com/history/daily/us/wa/seatac/KSEA",
        ghcnd_id="USW00024233",
    ),
    "Austin": Station(
        city="Austin",
        icao="KAUS",
        name="Austin-Bergstrom Intl Airport",
        lat=30.1831,
        lon=-97.6799,
        timezone="America/Chicago",
        wunderground_url="https://www.wunderground.com/history/daily/us/tx/austin/KAUS",
        ghcnd_id="USW00013904",
    ),
    "Atlanta": Station(
        city="Atlanta",
        icao="KATL",
        name="Hartsfield-Jackson Atlanta Intl Airport",
        lat=33.6297,
        lon=-84.4422,
        timezone="America/New_York",
        wunderground_url="https://www.wunderground.com/history/daily/us/ga/atlanta/KATL",
        ghcnd_id="USW00013874",
    ),
    "Denver": Station(
        city="Denver",
        icao="KBKF",
        name="Buckley Space Force Base",
        lat=39.7167,
        lon=-104.7500,
        timezone="America/Denver",
        wunderground_url="https://www.wunderground.com/history/daily/us/co/aurora/KBKF",
        ghcnd_id="USW00023036",
    ),
```

- [ ] **Step 2: Run the registry tests to verify they pass**

Run: `uv run pytest tests/test_config.py -q`
Expected: PASS (all config tests, including the three from Task 1 and the two pre-existing NYC tests).

- [ ] **Step 3: Commit**

```bash
git add src/rainmaker/config.py tests/test_config.py
git commit -m "feat: add the ten live US cities to the station registry"
```

---

## Task 3: Discovery skips a malformed market instead of aborting

**Files:**
- Create: `tests/fixtures/polymarket_weather_multicity.json`
- Modify: `tests/test_polymarket_client.py`
- Modify: `src/rainmaker/polymarket/client.py`

- [ ] **Step 1: Create the fixture**

Create `tests/fixtures/polymarket_weather_multicity.json` with exactly this content. It holds a valid multi-word city (Los Angeles), a valid trap city (Dallas/KDAL), a registry city whose description omits its ICAO (NYC, must be skipped), and an international city (London, must be filtered):

```json
[
  {
    "id": "900001",
    "slug": "highest-temperature-in-los-angeles",
    "title": "Highest temperature in Los Angeles on June 3?",
    "endDate": "2026-06-03T12:00:00Z",
    "description": "Resolves to the highest temperature recorded at the Los Angeles International Airport Station (KLAX) in whole degrees Fahrenheit, per Wunderground KLAX.",
    "markets": [
      {"groupItemTitle": "70-71°F", "outcomePrices": "[\"0.5\", \"0.5\"]", "clobTokenIds": "[\"la-yes\", \"la-no\"]", "bestAsk": 0.5, "bestBid": 0.4}
    ]
  },
  {
    "id": "900002",
    "slug": "highest-temperature-in-dallas",
    "title": "Highest temperature in Dallas on June 3?",
    "endDate": "2026-06-03T12:00:00Z",
    "description": "Resolves to the highest temperature recorded at Dallas Love Field (KDAL) in whole degrees Fahrenheit, per Wunderground KDAL.",
    "markets": [
      {"groupItemTitle": "88-89°F", "outcomePrices": "[\"0.3\", \"0.7\"]", "clobTokenIds": "[\"dal-yes\", \"dal-no\"]", "bestAsk": 0.31, "bestBid": 0.29}
    ]
  },
  {
    "id": "900003",
    "slug": "highest-temperature-in-nyc-broken",
    "title": "Highest temperature in NYC on June 3?",
    "endDate": "2026-06-03T12:00:00Z",
    "description": "This description is missing the resolution station id entirely.",
    "markets": [
      {"groupItemTitle": "70-71°F", "outcomePrices": "[\"0.5\", \"0.5\"]", "clobTokenIds": "[\"nyc-yes\", \"nyc-no\"]", "bestAsk": 0.5, "bestBid": 0.4}
    ]
  },
  {
    "id": "900004",
    "slug": "highest-temperature-in-london",
    "title": "Highest temperature in London on June 3?",
    "endDate": "2026-06-03T12:00:00Z",
    "description": "London resolves at EGLL.",
    "markets": [
      {"groupItemTitle": "70-71°F", "outcomePrices": "[\"0.5\", \"0.5\"]", "clobTokenIds": "[\"lon-yes\", \"lon-no\"]", "bestAsk": 0.5, "bestBid": 0.4}
    ]
  }
]
```

- [ ] **Step 2: Add the failing test**

Append to `tests/test_polymarket_client.py`:

```python
def _multicity_body() -> list[dict[str, Any]]:
    return json.loads((FIXTURES / "polymarket_weather_multicity.json").read_text())


def test_discover_skips_unparseable_and_drops_international(httpx_mock, capsys):
    httpx_mock.add_response(
        url=re.compile(re.escape(GAMMA_EVENTS_URL)), json=_multicity_body()
    )
    with httpx.Client() as client:
        markets = discover_markets(client)
    # Los Angeles (multi-word) and Dallas (trap KDAL) are kept; NYC is skipped
    # because its description omits KLGA; London is filtered (not US registry).
    assert sorted(m.target.station.icao for m in markets) == ["KDAL", "KLAX"]
    err = capsys.readouterr().err
    assert "900003" in err and "skip" in err.lower()
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `uv run pytest tests/test_polymarket_client.py::test_discover_skips_unparseable_and_drops_international -q`
Expected: FAIL. `discover_markets` raises `ValueError: resolution station KLGA not named ...` (event 900003) instead of skipping it.

- [ ] **Step 4: Make discovery resilient**

In `src/rainmaker/polymarket/client.py`, add `import sys` at the top (above `from typing import Any, cast`), then replace the `discover_markets` function with:

```python
def discover_markets(client: httpx.Client) -> list[Market]:
    """Fetch live weather events and parse the US-city temperature markets.

    A market that fails to parse (for example its description does not name the
    resolution station) is skipped with a warning so one bad market does not
    abort the whole run. Polymarket being down still aborts upstream.
    """
    markets: list[Market] = []
    for ev in fetch_weather_events(client):
        if not _is_us_temp_event(ev):
            continue
        try:
            markets.append(parse_market(ev))
        except ValueError as exc:
            print(f"skipping market {ev.get('id')}: {exc}", file=sys.stderr)
    return markets
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/test_polymarket_client.py -q`
Expected: PASS, including the pre-existing `test_discover_markets_filters_to_us_temp_markets` (the old single-NYC fixture still yields exactly one market).

- [ ] **Step 6: Commit**

```bash
git add src/rainmaker/polymarket/client.py tests/test_polymarket_client.py tests/fixtures/polymarket_weather_multicity.json
git commit -m "feat: skip a malformed market in discovery instead of aborting the run"
```

---

## Task 4: Full verification and finalize the PR

**Files:** none (verification only)

- [ ] **Step 1: Run the whole check suite**

Run:
```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest -q
```
Expected: ruff clean, format clean, mypy `Success`, all tests pass including the golden end-to-end (`tests/test_golden_e2e.py`), which is unchanged and stays green.

- [ ] **Step 2: Optional live smoke (read-only)**

Run: `uv run rainmaker run --reports-dir /tmp/rm_cities --db /tmp/rm_cities.db`
Expected: the terminal report now lists multiple US cities (whichever are live today), each shown `(uncalibrated)`. This is a sanity check, not a gate; it hits live Polymarket and forecast APIs.

- [ ] **Step 3: Mark the PR ready**

```bash
git push
gh pr ready 20
```

---

## Notes

- Calibration is deferred: each new city ships `(uncalibrated)` and `run`
  already falls back to the widened raw spread. Fitting a cell per city is a
  follow-on `rainmaker backfill --city <X>` run, out of scope here.
- Out of scope (separate slices): precipitation yes/no markets, lowest-
  temperature (TMIN) markets, standalone threshold-binary markets.
