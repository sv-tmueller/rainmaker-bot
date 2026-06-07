# Kalshi Second Venue (slice 1: daily high temp, read-only/advisory) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Kalshi daily high-temperature markets as a second read-only source so the daily report edge-ranks them alongside Polymarket, using the existing forecast/calibration/edge/record pipeline unchanged.

**Architecture:** Kalshi high-temp is the same math as Polymarket temp (a Gaussian over whole-degree buckets), so we reuse `Market`, `Bucket`, `Target`, `Station`, `aggregate`, `evaluate_market`, and `record_run` as-is. The only new code is a Kalshi market parser (the API exposes one binary strike per market, grouped under an event ladder), a Kalshi station registry, and a read-only discovery client, wired into `cli._run`. A Kalshi outage is non-fatal (Polymarket stays the primary venue whose outage aborts the run).

**Tech Stack:** Python 3.11+, httpx, pydantic, pytest, pytest-httpx. Kalshi public trade API (`https://api.elections.kalshi.com/trade-api/v2`, no auth for market data).

---

## Scope

> **Status update.** This PR grew past slice 1 to cover the weather markets the
> bot already does on Polymarket: Kalshi **daily high temp** (Tasks 1-6 below),
> plus **daily low temp (TMIN)** and **monthly rain** (added on the same branch,
> reusing the temperature and precip pipelines). SpaceX was dropped by decision.
> The tasks below describe the high-temp slice; low temp and rain follow the same
> shape (a Kalshi parser + registry that feeds the existing pipeline).

In scope: discover Kalshi daily high-temp, daily low-temp, and monthly-rain
markets, parse them into the existing `Market` / `PrecipMonthlyMarket` types,
edge-rank them with the existing pipeline, render and record them in the daily
run. Temperature seeds five cities (NYC, Chicago, Miami, Los Angeles, Austin);
rain seeds NYC and Chicago (the cities with confirmed GHCND ids).

Explicitly out of scope (separate follow-up issues, noted at the end):
- Kalshi monthly snowfall (new variable; confirm the rule in winter first).
- Calibration backfill for the Kalshi stations (KNYC, KMDW). They run
  uncalibrated for now; the report flags `calibrated=False`. Acceptable for
  advisory output.
- Settlement and P&L tracking of Kalshi markets (verify `settle.py`'s station
  resolution first; confirm the KMDW GHCND id).
- A formal `venue` column. Venues are distinguished by the station code already
  shown in the report (Polymarket NYC = KLGA, Kalshi NYC = KNYC).

## Key design decisions

1. **Reuse, do not fork the math.** Kalshi high-temp markets become the existing `Market`/`Bucket`/`Target` objects. They flow through `aggregate` -> `evaluate_market` -> `record_run` with zero changes to those modules. This is unlike the precip slice, which needed a parallel gamma path; here the distribution is identical.

2. **Station registry reuses the `Station` model, no model change.** `KALSHI_STATIONS: dict[str, Station]`. Each Kalshi temp market names a real weather station with a real ICAO and GHCND id, which is exactly what the calibration key and settlement proxy already use:
   - The `name` field holds the exact station phrase Kalshi's rule text uses; the parser guards on it (`station.name in rules_primary`), mirroring how the Polymarket parser guards on `station.icao in description`.
   - The `wunderground_url` field holds the NWS Climatological Report (Daily) product URL. The recorder already stores this column as `resolution_source`; for Kalshi the resolution source is that CLI page. The field name is legacy; a rename is out of scope.

3. **Settlement station differs from rainmaker's temperature station for two cities.** Kalshi settles NYC on Central Park (KNYC) and Chicago on Midway (KMDW); rainmaker's `STATIONS` use LaGuardia (KLGA) and O'Hare (KORD). The Kalshi registry uses the Kalshi stations. Miami (KMIA), Los Angeles (KLAX), and Austin (KAUS) match rainmaker's stations.

4. **Settlement date from the event ticker.** A Kalshi high-temp event ticker is `KXHIGH<CODE>-<YY><MON><DD>` (e.g. `KXHIGHNY-26JUN08`). The date token is parsed directly; this is deterministic and avoids timezone ambiguity. Fail loud if the token does not parse.

5. **Strike -> bucket mapping.** Each Kalshi market is one binary strike:
   - `strike_type: "greater"` -> `("above", threshold=floor_strike)`
   - `strike_type: "less"` -> `("below", threshold=cap_strike)`
   - `strike_type: "between"` -> `("range", lo=floor_strike, hi=cap_strike)`
   Prices come from `yes_ask_dollars` / `yes_bid_dollars` / `no_ask_dollars` (Kalshi gives the NO ask directly, unlike Gamma where it is derived).

6. **Kalshi is the secondary venue.** A Kalshi HTTP error logs a warning and yields zero Kalshi markets; the run continues with Polymarket. Only Polymarket being down aborts the run (existing behavior, unchanged).

## File structure

- Create: `src/rainmaker/kalshi/__init__.py` - empty package marker.
- Create: `src/rainmaker/kalshi/markets.py` - parse a Kalshi strike into a `Bucket` and a Kalshi event ladder into a `Market`.
- Create: `src/rainmaker/kalshi/client.py` - read-only discovery: fetch open high-temp markets per city, group by event, parse.
- Modify: `src/rainmaker/config.py` - add `KALSHI_API_BASE`, `KALSHI_HIGH_SERIES`, `KALSHI_STATIONS`.
- Modify: `src/rainmaker/cli.py` - discover and evaluate Kalshi high-temp markets inside `_run`, appended to the existing `evaluated` list.
- Create: `tests/test_kalshi_markets.py` - parser unit tests.
- Create: `tests/test_kalshi_client.py` - discovery against a saved fixture (pytest-httpx).
- Create: `tests/fixtures/kalshi_high_temp_nyc.json` - a saved `/markets` response for one NYC event ladder.

---

### Task 1: Kalshi config (API base, series map, station registry)

**Files:**
- Modify: `src/rainmaker/config.py` (append after `PRECIP_STATIONS`, before `# Source config`)
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config.py`:

```python
from rainmaker.config import KALSHI_HIGH_SERIES, KALSHI_STATIONS


def test_kalshi_registry_aligned():
    # every city with a series ticker has a settlement station and vice versa
    assert set(KALSHI_HIGH_SERIES) == set(KALSHI_STATIONS)
    # the two cities that differ from the Polymarket stations
    assert KALSHI_STATIONS["NYC"].icao == "KNYC"
    assert KALSHI_STATIONS["Chicago"].icao == "KMDW"
    # every station carries the rule-text guard phrase and a resolution-source URL
    for city, st in KALSHI_STATIONS.items():
        assert st.name, city
        assert st.wunderground_url.startswith("https://"), city
        assert st.ghcnd_id, city
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py::test_kalshi_registry_aligned -v`
Expected: FAIL with `ImportError: cannot import name 'KALSHI_HIGH_SERIES'`

- [ ] **Step 3: Write minimal implementation**

In `src/rainmaker/config.py`, after the `PRECIP_STATIONS` block:

```python
# Kalshi (read-only second venue). Daily high-temp markets settle on the NWS
# Climatological Report (Daily) for a named station, which differs from the
# Polymarket/Wunderground station for NYC (Central Park, not LaGuardia) and
# Chicago (Midway, not O'Hare). The Station.name field holds the exact phrase the
# Kalshi rule text uses (the parser guards on it); wunderground_url holds the NWS
# CLI product URL (the recorder stores it as resolution_source).
KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"

KALSHI_HIGH_SERIES: dict[str, str] = {
    "NYC": "KXHIGHNY",
    "Chicago": "KXHIGHCHI",
    "Miami": "KXHIGHMIA",
    "Los Angeles": "KXHIGHLAX",
    "Austin": "KXHIGHAUS",
}

KALSHI_STATIONS: dict[str, Station] = {
    "NYC": Station(
        city="NYC",
        icao="KNYC",
        name="Central Park, New York",
        lat=40.7790,
        lon=-73.9692,
        timezone="America/New_York",
        wunderground_url="https://forecast.weather.gov/product.php?site=OKX&product=CLI&issuedby=NYC",
        ghcnd_id="USW00094728",  # confirmed in PRECIP_STATIONS (Central Park)
    ),
    "Chicago": Station(
        city="Chicago",
        icao="KMDW",
        name="Chicago Midway",
        lat=41.7860,
        lon=-87.7524,
        timezone="America/Chicago",
        wunderground_url="https://forecast.weather.gov/product.php?site=LOT&product=CLI&issuedby=MDW",
        ghcnd_id="USW00014819",  # TODO: confirm against NCEI GHCND before trusting settlement
    ),
    "Miami": Station(
        city="Miami",
        icao="KMIA",
        name="Miami International Airport",
        lat=25.7881,
        lon=-80.3169,
        timezone="America/New_York",
        wunderground_url="https://forecast.weather.gov/product.php?site=MFL&product=CLI&issuedby=MIA",
        ghcnd_id="USW00012839",
    ),
    "Los Angeles": Station(
        city="Los Angeles",
        icao="KLAX",
        name="Los Angeles Airport",
        lat=33.9382,
        lon=-118.3866,
        timezone="America/Los_Angeles",
        wunderground_url="https://forecast.weather.gov/product.php?site=LOX&product=CLI&issuedby=LAX",
        ghcnd_id="USW00023174",
    ),
    "Austin": Station(
        city="Austin",
        icao="KAUS",
        name="Austin Bergstrom",
        lat=30.1831,
        lon=-97.6799,
        timezone="America/Chicago",
        wunderground_url="https://forecast.weather.gov/product.php?site=EWX&product=CLI&issuedby=AUS",
        ghcnd_id="USW00013904",
    ),
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_config.py::test_kalshi_registry_aligned -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/rainmaker/config.py tests/test_config.py
git commit -m "feat: add Kalshi station registry and series map (#102)"
```

---

### Task 2: Kalshi strike -> Bucket parser

**Files:**
- Create: `src/rainmaker/kalshi/__init__.py`
- Create: `src/rainmaker/kalshi/markets.py`
- Test: `tests/test_kalshi_markets.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_kalshi_markets.py`:

```python
import pytest

from rainmaker.kalshi.markets import parse_kalshi_bucket


def _mkt(**over):
    base = {
        "ticker": "KXHIGHNY-26JUN08-T79",
        "strike_type": "greater",
        "floor_strike": 79,
        "cap_strike": None,
        "yes_bid_dollars": "0.0900",
        "yes_ask_dollars": "0.1200",
        "no_ask_dollars": "0.8900",
        "last_price_dollars": "0.1000",
    }
    base.update(over)
    return base


def test_greater_strike_is_above():
    b = parse_kalshi_bucket(_mkt())
    assert (b.kind, b.threshold) == ("above", 79)
    assert b.best_ask == 0.12 and b.best_bid == 0.09 and b.no_ask == 0.89


def test_less_strike_is_below():
    b = parse_kalshi_bucket(_mkt(strike_type="less", floor_strike=None, cap_strike=72))
    assert (b.kind, b.threshold) == ("below", 72)


def test_between_strike_is_range():
    b = parse_kalshi_bucket(
        _mkt(strike_type="between", floor_strike=78, cap_strike=79)
    )
    assert (b.kind, b.lo, b.hi) == ("range", 78, 79)


def test_unknown_strike_type_raises():
    with pytest.raises(ValueError):
        parse_kalshi_bucket(_mkt(strike_type="weird"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_kalshi_markets.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'rainmaker.kalshi'`

- [ ] **Step 3: Write minimal implementation**

Create `src/rainmaker/kalshi/__init__.py` (empty).

Create `src/rainmaker/kalshi/markets.py`:

```python
"""Parse Kalshi daily high-temp markets into the shared Market/Bucket types.

Kalshi exposes one binary strike per market (strike_type greater/less/between
with floor_strike/cap_strike), grouped under an event ladder. We map each strike
onto the existing Bucket (above/below/range) so the Polymarket evaluate/record
path is reused unchanged. A parallel of polymarket/markets.py for a different
wire format, sharing the Market/Bucket types.
"""

import re
from datetime import date, datetime

from rainmaker.config import Station, Target
from rainmaker.polymarket.markets import Bucket, Market

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
_TICKER_DATE_RE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})(?:-|$)")


def _price(market: dict, key: str) -> float | None:
    raw = market.get(key)
    if raw in (None, ""):
        return None
    val = float(raw)
    return val if val > 0 else None


def parse_kalshi_bucket(market: dict) -> Bucket:
    strike_type = market["strike_type"]
    floor = market.get("floor_strike")
    cap = market.get("cap_strike")
    if strike_type == "greater":
        kind, lo, hi, threshold = "above", None, None, int(floor)
    elif strike_type == "less":
        kind, lo, hi, threshold = "below", None, None, int(cap)
    elif strike_type == "between":
        kind, lo, hi, threshold = "range", int(floor), int(cap), None
    else:
        raise ValueError(f"unknown Kalshi strike_type: {strike_type!r}")
    best_ask = _price(market, "yes_ask_dollars")
    best_bid = _price(market, "yes_bid_dollars")
    last = _price(market, "last_price_dollars")
    mid = None if best_ask is None or best_bid is None else (best_ask + best_bid) / 2
    yes_price = last if last is not None else (mid if mid is not None else 0.0)
    return Bucket(
        label=market.get("subtitle") or market["ticker"],
        kind=kind,
        lo=lo,
        hi=hi,
        threshold=threshold,
        yes_token_id=market["ticker"],
        best_ask=best_ask,
        best_bid=best_bid,
        yes_price=yes_price,
        no_token_id="",
        no_ask=_price(market, "no_ask_dollars"),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_kalshi_markets.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/rainmaker/kalshi/__init__.py src/rainmaker/kalshi/markets.py tests/test_kalshi_markets.py
git commit -m "feat: parse Kalshi strikes into the shared Bucket type (#102)"
```

---

### Task 3: Kalshi event ladder -> Market parser

**Files:**
- Modify: `src/rainmaker/kalshi/markets.py`
- Test: `tests/test_kalshi_markets.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_kalshi_markets.py`:

```python
from datetime import date

from rainmaker.config import KALSHI_STATIONS
from rainmaker.kalshi.markets import parse_kalshi_event


def _event_markets():
    rule = (
        "If the highest temperature recorded in Central Park, New York for "
        "June 08, 2026 as reported by the National Weather Service's "
        "Climatological Report (Daily), is greater than 79, then Yes."
    )
    return [
        {
            "event_ticker": "KXHIGHNY-26JUN08",
            "ticker": "KXHIGHNY-26JUN08-T79",
            "strike_type": "greater",
            "floor_strike": 79,
            "subtitle": "above 79",
            "yes_bid_dollars": "0.0900",
            "yes_ask_dollars": "0.1200",
            "no_ask_dollars": "0.8900",
            "last_price_dollars": "0.1000",
            "rules_primary": rule,
        },
        {
            "event_ticker": "KXHIGHNY-26JUN08",
            "ticker": "KXHIGHNY-26JUN08-B77.5",
            "strike_type": "between",
            "floor_strike": 77,
            "cap_strike": 78,
            "subtitle": "77 to 78",
            "yes_bid_dollars": "0.1500",
            "yes_ask_dollars": "0.1600",
            "no_ask_dollars": "0.8500",
            "last_price_dollars": "0.1500",
            "rules_primary": rule,
        },
    ]


def test_parse_event_builds_market():
    m = parse_kalshi_event("NYC", KALSHI_STATIONS["NYC"], _event_markets())
    assert m.id == "KXHIGHNY-26JUN08"
    assert m.target.station.icao == "KNYC"
    assert m.target.variable == "TMAX"
    assert m.target.local_date == date(2026, 6, 8)
    assert len(m.buckets) == 2


def test_parse_event_guards_station_mismatch():
    markets = _event_markets()
    for mk in markets:
        mk["rules_primary"] = mk["rules_primary"].replace("Central Park, New York", "LaGuardia")
    with pytest.raises(ValueError, match="not named"):
        parse_kalshi_event("NYC", KALSHI_STATIONS["NYC"], markets)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_kalshi_markets.py::test_parse_event_builds_market -v`
Expected: FAIL with `ImportError: cannot import name 'parse_kalshi_event'`

- [ ] **Step 3: Write minimal implementation**

Append to `src/rainmaker/kalshi/markets.py`:

```python
def _settlement_date(event_ticker: str) -> date:
    match = _TICKER_DATE_RE.search(event_ticker)
    if match is None:
        raise ValueError(f"no date token in Kalshi event ticker: {event_ticker!r}")
    yy, mon, dd = match.group(1), match.group(2), match.group(3)
    month = _MONTHS.get(mon)
    if month is None:
        raise ValueError(f"unrecognized month in event ticker: {event_ticker!r}")
    return date(2000 + int(yy), month, int(dd))


def parse_kalshi_event(city: str, station: Station, event_markets: list[dict]) -> Market:
    """Build a Market from the strikes of one Kalshi high-temp event ladder.

    Guards that the rule text names the expected settlement station, mirroring the
    Polymarket parser's ICAO guard. Raises ValueError on any inconsistency so one
    bad event is skipped upstream rather than silently mispriced.
    """
    if not event_markets:
        raise ValueError(f"empty Kalshi event for {city}")
    event_ticker = event_markets[0]["event_ticker"]
    rules = event_markets[0].get("rules_primary", "")
    if station.name not in rules:
        raise ValueError(
            f"resolution station {station.name!r} not named in event {event_ticker} rules"
        )
    local_date = _settlement_date(event_ticker)
    target = Target(station=station, variable="TMAX", local_date=local_date)
    buckets = [parse_kalshi_bucket(m) for m in event_markets]
    return Market(
        id=event_ticker,
        slug=event_ticker,
        title=f"Kalshi: highest temperature in {city} on {local_date.isoformat()}",
        target=target,
        buckets=buckets,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_kalshi_markets.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add src/rainmaker/kalshi/markets.py tests/test_kalshi_markets.py
git commit -m "feat: parse Kalshi high-temp event ladder into a Market (#102)"
```

---

### Task 4: Kalshi discovery client (fixture-tested)

**Files:**
- Create: `src/rainmaker/kalshi/client.py`
- Create: `tests/fixtures/kalshi_high_temp_nyc.json`
- Test: `tests/test_kalshi_client.py`

- [ ] **Step 1: Create the fixture**

Create `tests/fixtures/kalshi_high_temp_nyc.json` (a trimmed `/markets` response for one NYC event ladder; two strikes are enough):

```json
{
  "cursor": "",
  "markets": [
    {
      "event_ticker": "KXHIGHNY-26JUN08",
      "ticker": "KXHIGHNY-26JUN08-T79",
      "status": "active",
      "strike_type": "greater",
      "floor_strike": 79,
      "subtitle": "above 79",
      "yes_bid_dollars": "0.0900",
      "yes_ask_dollars": "0.1200",
      "no_ask_dollars": "0.8900",
      "last_price_dollars": "0.1000",
      "rules_primary": "If the highest temperature recorded in Central Park, New York for June 08, 2026 as reported by the National Weather Service's Climatological Report (Daily), is greater than 79, then the market resolves to Yes."
    },
    {
      "event_ticker": "KXHIGHNY-26JUN08",
      "ticker": "KXHIGHNY-26JUN08-B77.5",
      "status": "active",
      "strike_type": "between",
      "floor_strike": 77,
      "cap_strike": 78,
      "subtitle": "77 to 78",
      "yes_bid_dollars": "0.1500",
      "yes_ask_dollars": "0.1600",
      "no_ask_dollars": "0.8500",
      "last_price_dollars": "0.1500",
      "rules_primary": "If the highest temperature recorded in Central Park, New York for June 08, 2026 as reported by the National Weather Service's Climatological Report (Daily), is between 77 and 78, then the market resolves to Yes."
    }
  ]
}
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_kalshi_client.py`:

```python
import json
from pathlib import Path

import httpx

from rainmaker.kalshi.client import discover_kalshi_markets

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "kalshi_high_temp_nyc.json").read_text()
)


def test_discover_parses_nyc_ladder(httpx_mock):
    # every configured series returns the same NYC fixture except NYC; others empty
    def handler(request):
        if "KXHIGHNY" in str(request.url):
            return httpx.Response(200, json=FIXTURE)
        return httpx.Response(200, json={"cursor": "", "markets": []})

    httpx_mock.add_callback(handler)
    with httpx.Client() as client:
        markets = discover_kalshi_markets(client)
    nyc = [m for m in markets if m.id == "KXHIGHNY-26JUN08"]
    assert len(nyc) == 1
    assert nyc[0].target.station.icao == "KNYC"
    assert len(nyc[0].buckets) == 2


def test_discover_kalshi_outage_is_non_fatal(httpx_mock):
    httpx_mock.add_exception(httpx.ConnectError("kalshi down"))
    with httpx.Client() as client:
        markets = discover_kalshi_markets(client)
    assert markets == []
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_kalshi_client.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'rainmaker.kalshi.client'`

- [ ] **Step 4: Write minimal implementation**

Create `src/rainmaker/kalshi/client.py`:

```python
import sys
from collections import defaultdict
from typing import Any

import httpx

from rainmaker.config import KALSHI_API_BASE, KALSHI_HIGH_SERIES, KALSHI_STATIONS
from rainmaker.kalshi.markets import parse_kalshi_event
from rainmaker.polymarket.markets import Market


def _fetch_open_markets(client: httpx.Client, series_ticker: str, *, max_pages: int = 6) -> list[dict[str, Any]]:
    """Page through one series' open markets. Raises on any HTTP error."""
    out: list[dict[str, Any]] = []
    cursor = ""
    for _ in range(max_pages):
        params = {"series_ticker": series_ticker, "status": "open", "limit": "200"}
        if cursor:
            params["cursor"] = cursor
        resp = client.get(f"{KALSHI_API_BASE}/markets", params=params)
        resp.raise_for_status()
        body = resp.json()
        out.extend(body.get("markets", []))
        cursor = body.get("cursor") or ""
        if not cursor:
            break
    return out


def discover_kalshi_markets(client: httpx.Client) -> list[Market]:
    """Discover live Kalshi daily high-temp markets for the configured cities.

    Read-only. Kalshi is the secondary venue: any HTTP error logs a warning and
    yields no Kalshi markets so the run continues on Polymarket. A single event
    that fails to parse is skipped with a warning.
    """
    markets: list[Market] = []
    for city, series in KALSHI_HIGH_SERIES.items():
        station = KALSHI_STATIONS[city]
        try:
            raw = _fetch_open_markets(client, series)
        except httpx.HTTPError as exc:
            print(f"Kalshi unavailable for {series}, skipping: {exc}", file=sys.stderr)
            continue
        by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for m in raw:
            by_event[m["event_ticker"]].append(m)
        for event_ticker, event_markets in by_event.items():
            try:
                markets.append(parse_kalshi_event(city, station, event_markets))
            except ValueError as exc:
                print(f"skipping Kalshi event {event_ticker}: {exc}", file=sys.stderr)
    return markets
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_kalshi_client.py -v`
Expected: PASS (2 tests)

- [ ] **Step 6: Commit**

```bash
git add src/rainmaker/kalshi/client.py tests/test_kalshi_client.py tests/fixtures/kalshi_high_temp_nyc.json
git commit -m "feat: read-only Kalshi high-temp discovery client (#102)"
```

---

### Task 5: Wire Kalshi into the daily run

**Files:**
- Modify: `src/rainmaker/cli.py:106-181` (the `_run` function) and the imports near line 33.
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cli.py` (follow the existing mocking style in that file for Polymarket/forecasts; the assertion is that a Kalshi market reaches the rendered report). If `test_cli.py` already stubs `discover_markets`/forecasts, add a parallel stub for `discover_kalshi_markets`:

```python
def test_run_includes_kalshi_markets(monkeypatch, tmp_path, capsys):
    import rainmaker.cli as cli
    from rainmaker.config import KALSHI_STATIONS, Target
    from rainmaker.polymarket.markets import Bucket, Market

    today = cli._today()
    market = Market(
        id="KXHIGHNY-TEST",
        slug="KXHIGHNY-TEST",
        title="Kalshi: highest temperature in NYC",
        target=Target(station=KALSHI_STATIONS["NYC"], variable="TMAX", local_date=today),
        buckets=[
            Bucket(
                label="above 79", kind="above", lo=None, hi=None, threshold=79,
                yes_token_id="t", best_ask=0.12, best_bid=0.09, yes_price=0.10,
                no_token_id="", no_ask=0.89,
            )
        ],
    )
    monkeypatch.setattr(cli, "discover_markets", lambda c: [])
    monkeypatch.setattr(cli, "discover_precip_markets", lambda c: [])
    monkeypatch.setattr(cli, "discover_kalshi_markets", lambda c: [market])
    # stub forecasts so evaluate runs without network (mirror the existing helper stub
    # used by the other cli tests; returns a ForecastSet with >=2 samples for NYC TMAX)
    monkeypatch.setattr(cli, "_forecast_for", _stub_forecast_set)

    cli._run(reports_dir=str(tmp_path), db_path=str(tmp_path / "t.db"))
    out = capsys.readouterr().out
    assert "KNYC" in out  # the Kalshi NYC station code reached the report
```

(Use the same `_stub_forecast_set` helper the other `test_cli.py` tests use; if none exists, build a `ForecastSet` with two `ForecastSample` values around 80F. Match the existing file's pattern rather than inventing a new one.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli.py::test_run_includes_kalshi_markets -v`
Expected: FAIL with `AttributeError: module 'rainmaker.cli' has no attribute 'discover_kalshi_markets'`

- [ ] **Step 3: Write minimal implementation**

In `src/rainmaker/cli.py`, add the import near the existing Polymarket client import (around line 33):

```python
from rainmaker.kalshi.client import discover_kalshi_markets
```

In `_run`, after the Polymarket temperature loop builds `evaluated` and before the precip loop (after line 143), add the Kalshi loop. It reuses every helper already in scope:

```python
        try:
            kalshi_markets = discover_kalshi_markets(client)
        except httpx.HTTPError as exc:
            # Kalshi is the secondary venue: never abort the run on its outage.
            print(f"Kalshi discovery failed, continuing: {exc}", file=sys.stderr)
            kalshi_markets = []
        for market in kalshi_markets:
            if market.target.variable not in SUPPORTED_VARIABLES:
                print(f"skipped {market.id}: unsupported variable {market.target.variable}")
                continue
            lead_time = (market.target.local_date - today).days
            if lead_time < 0:
                print(f"skipped {market.id}: settled ({market.target.local_date})")
                continue
            forecast_set = _forecast_for(market.target, client)
            calibration = load_calibration(
                conn, market.target.station.icao, market.target.variable, lead_time
            )
            report = evaluate_market(
                market,
                forecast_set,
                floor=CONFIDENCE_FLOOR,
                min_sources=MIN_SOURCES,
                min_sigma=MIN_SIGMA_F,
                min_edge=MIN_EDGE,
                calibration=calibration,
            )
            evaluated.append((market, forecast_set, report))
```

(`discover_kalshi_markets` already swallows per-series HTTP errors and returns a list; the outer try/except is belt-and-suspenders for an unexpected error and keeps the project rule "Kalshi down must not abort" explicit at the call site.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cli.py::test_run_includes_kalshi_markets -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/rainmaker/cli.py tests/test_cli.py
git commit -m "feat: include Kalshi high-temp markets in the daily run (#102)"
```

---

### Task 6: Full check suite and docs

**Files:**
- Modify: `CLAUDE.md` (repo-root) repo-layout block: add the `kalshi/` package line.
- Modify: `docs/architecture/` if a data-source policy doc exists; otherwise skip.

- [ ] **Step 1: Run the whole suite green**

Run: `uv run pytest`
Expected: PASS, including `tests/test_golden_e2e.py` (the Kalshi path is additive and must not change the golden output, which uses Polymarket fixtures only).

- [ ] **Step 2: Lint, format, type-check**

Run:
```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```
Expected: all clean. (`mypy` must be happy that `parse_kalshi_event` returns the `Market` type the reused pipeline expects.)

- [ ] **Step 3: One guarded live smoke check (manual, not a test)**

Confirm the real API still matches the parser shape (the only thing fixtures cannot catch is a wire-format drift). Run a throwaway:
```bash
uv run python -c "import httpx; from rainmaker.kalshi.client import discover_kalshi_markets; \
c=httpx.Client(timeout=30); ms=discover_kalshi_markets(c); c.close(); \
print(len(ms), [ (m.target.station.icao, len(m.buckets)) for m in ms[:3] ])"
```
Expected: a non-zero count during US daytime hours with plausible station codes. If it 429s, retry once after a pause. This is a smoke check, not part of `pytest`.

- [ ] **Step 4: Update the repo-layout doc**

In `CLAUDE.md`, add under `src/rainmaker/` near the `polymarket/` block:

```
  kalshi/
    client.py         Kalshi discovery (read-only): daily high-temp markets
    markets.py        Kalshi event ladder -> Market (reuses Bucket/Target; station guard)
```

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: note the kalshi package in the repo layout (#102)"
```

---

## Self-review

- **Spec coverage.** The #102 spike sub-plan item 1 (Kalshi read-only discovery client + temp parser + station registry, reuse the pipeline, station guard) maps to Tasks 1-5. Items 2-4 (rain, snow, SpaceX) are explicitly deferred in Scope. The spike's station-trap finding (NYC Central Park, Chicago Midway) is encoded in Task 1 and guarded in Task 3.
- **Placeholders.** One intentional `TODO` remains in Task 1 (confirm the KMDW GHCND id against NCEI). It does not block slice 1, which does not settle Kalshi markets; it is a settlement-time prerequisite and is called out in Scope.
- **Type consistency.** `parse_kalshi_bucket` returns `Bucket`; `parse_kalshi_event` returns `Market`; `discover_kalshi_markets` returns `list[Market]`; `_run` consumes them as the existing `EvaluatedMarket = (Market, ForecastSet, MarketReport)` tuple. Names match across tasks (`KALSHI_HIGH_SERIES`, `KALSHI_STATIONS`, `KALSHI_API_BASE`).

## Follow-up issues (out of scope here)

1. Calibration backfill for the Kalshi stations (KNYC, KMDW, and the rain
   stations) so the markets run calibrated; extend `backfill` to accept the
   Kalshi registries.
2. Settlement + tracking for Kalshi markets: verify `settle.py` resolves the
   station/GHCND for Kalshi rows; confirm the KMDW GHCND id.
3. Kalshi monthly snowfall (new variable); confirm the exact rule and station in
   winter when markets are live.
4. More rain cities (Denver `CLIDEN` and others) once their GHCND ids are
   confirmed against NCEI.
5. A `venue` column if per-venue reporting/filtering is wanted beyond the station
   code.

Delivered in this PR (was follow-up, now done): daily low temp (TMIN) and monthly
rain. Dropped by decision: SpaceX launch counts.
