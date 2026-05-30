# Phase 2a: Polymarket read-only client Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Discover live US-city weather temperature markets on Polymarket (read-only) and parse each into a typed `Market` carrying the settlement `Target` (city -> station -> variable -> local date) and the list of outcome buckets with current prices.

**Architecture:** A new `src/rainmaker/polymarket` package. `markets.py` holds the `Bucket`/`Market` pydantic models and pure parsers (label -> bucket edges, event JSON -> Market). `client.py` does Gamma HTTP I/O (paginated fetch) and `discover_markets` (fetch + filter to US-city temp markets + parse). Prices come from Gamma's per-market `bestAsk`/`bestBid`/`outcomePrices` (the CLOB best ask surfaced through Gamma), so no separate CLOB integration is needed. Pure parsers are tested against a saved fixture; I/O is mocked with pytest-httpx.

**Tech Stack:** Python 3.11+, httpx, pydantic v2; pytest + pytest-httpx. No new dependencies.

**Scope:** This is PR-A of Phase 2 (issue #5). It produces the `Market` objects the engine (PR-B) consumes. No probability/edge/report and no persistence here. The forecast layer currently supports `TMAX` only; the client still models `TMIN` ("Lowest temperature") markets so PR-B can skip them explicitly, but discovery filters to cities present in the `STATIONS` registry (NYC today).

**Decisions carried from brainstorming (record here, not in a separate spec):**
- Implied price for edge is the CLOB best ask for the YES token, which Gamma exposes as `bestAsk` per market. A bucket with `bestAsk` null/0 has no executable ask (thin book); PR-B excludes it.
- Resolution station is sacred: the parser verifies the registry station's ICAO appears in the market's resolution rule text and raises if not.
- Settlement local date is derived from the event `endDate` converted into the station timezone (avoids fragile title-date parsing).

**Fixture:** `tests/fixtures/polymarket_weather_events.json` (committed) is a trimmed real Gamma `/events?tag_slug=weather` response with three events: NYC highest-temp (id `533147`, US/TMAX, 11 buckets), London highest-temp (id `533140`, non-US -> filtered out), and a hurricane market (id `97125`, non-temp -> filtered out). Known NYC values: settlement date `2026-05-30`, station `KLGA`, description contains `KLGA`. Buckets (label, kind, best_ask, best_bid, yes_price):
- `"59°F or below"` -> below, thr 59, ask 0.004, bid 0.001, yes 0.0025
- `"70-71°F"` -> range, 70-71, ask 0.999, bid 0.996, yes 0.9975
- `"78°F or higher"` -> above, thr 78, ask 0.001, bid None, yes 0.0005

---

### Task 1: Package skeleton

**Files:**
- Create: `src/rainmaker/polymarket/__init__.py` (empty)

- [ ] **Step 1: Create the package directory**

Create an empty `src/rainmaker/polymarket/__init__.py`. No dependency changes (httpx, pydantic already present).

- [ ] **Step 2: Verify the package imports**

Run: `uv run python -c "import rainmaker.polymarket"`
Expected: no output, exit 0.

- [ ] **Step 3: Commit**

```bash
git add src/rainmaker/polymarket/__init__.py
git commit -m "build: add polymarket package skeleton"
```

---

### Task 2: Bucket label parser (pure)

**Files:**
- Create: `src/rainmaker/polymarket/markets.py`
- Test: `tests/test_polymarket_markets.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_polymarket_markets.py
from rainmaker.polymarket.markets import parse_bucket_label


def test_parse_bucket_label_below():
    assert parse_bucket_label("59°F or below") == ("below", None, None, 59)


def test_parse_bucket_label_range():
    assert parse_bucket_label("70-71°F") == ("range", 70, 71, None)


def test_parse_bucket_label_above():
    assert parse_bucket_label("78°F or higher") == ("above", None, None, 78)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_polymarket_markets.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'rainmaker.polymarket.markets'`.

- [ ] **Step 3: Write the parser in `src/rainmaker/polymarket/markets.py`**

```python
import re
from typing import Literal

BucketKind = Literal["below", "range", "above"]

_RANGE_RE = re.compile(r"(\d+)\s*-\s*(\d+)")
_THRESHOLD_RE = re.compile(r"(\d+)")


def parse_bucket_label(label: str) -> tuple[BucketKind, int | None, int | None, int | None]:
    """Parse a Polymarket bucket title into (kind, lo, hi, threshold).

    "59°F or below" -> ("below", None, None, 59)
    "70-71°F"       -> ("range", 70, 71, None)
    "78°F or higher" -> ("above", None, None, 78)
    """
    lowered = label.lower()
    if "below" in lowered:
        match = _THRESHOLD_RE.search(label)
        if match is None:
            raise ValueError(f"no threshold in below-bucket label: {label!r}")
        return ("below", None, None, int(match.group(1)))
    if "higher" in lowered or "above" in lowered:
        match = _THRESHOLD_RE.search(label)
        if match is None:
            raise ValueError(f"no threshold in above-bucket label: {label!r}")
        return ("above", None, None, int(match.group(1)))
    match = _RANGE_RE.search(label)
    if match is None:
        raise ValueError(f"unrecognized bucket label: {label!r}")
    return ("range", int(match.group(1)), int(match.group(2)), None)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_polymarket_markets.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/rainmaker/polymarket/markets.py tests/test_polymarket_markets.py
git commit -m "feat: parse Polymarket bucket labels into edge specs"
```

---

### Task 3: Bucket model and parse_bucket (against fixture)

**Files:**
- Modify: `src/rainmaker/polymarket/markets.py`
- Modify: `tests/test_polymarket_markets.py`
- Uses: `tests/fixtures/polymarket_weather_events.json` (committed)

- [ ] **Step 1: Write the failing test (append to the test file; add imports at top)**

```python
import json
from pathlib import Path

from rainmaker.polymarket.markets import Bucket, parse_bucket

FIXTURES = Path(__file__).parent / "fixtures"


def _nyc_event() -> dict:
    events = json.loads((FIXTURES / "polymarket_weather_events.json").read_text())
    return next(e for e in events if e["id"] == "533147")


def test_parse_bucket_below():
    market = _nyc_event()["markets"][0]
    b = parse_bucket(market)
    assert b.label == "59°F or below"
    assert b.kind == "below"
    assert b.threshold == 59
    assert b.lo is None and b.hi is None
    assert b.best_ask == 0.004
    assert b.best_bid == 0.001
    assert b.yes_price == 0.0025
    assert b.yes_token_id == (
        "63103732622160665189154558165913165656167238975108887912070417445520275819404"
    )


def test_parse_bucket_range_and_above():
    markets = _nyc_event()["markets"]
    rng = parse_bucket(markets[6])
    assert (rng.label, rng.kind, rng.lo, rng.hi) == ("70-71°F", "range", 70, 71)
    assert rng.best_ask == 0.999

    above = parse_bucket(markets[10])
    assert (above.label, above.kind, above.threshold) == ("78°F or higher", "above", 78)
    assert above.best_bid is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_polymarket_markets.py -v`
Expected: FAIL with `ImportError: cannot import name 'Bucket'`.

- [ ] **Step 3: Add the Bucket model and parse_bucket to `markets.py`**

Add these imports at the top of `markets.py`:

```python
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
```

Add after `parse_bucket_label`:

```python
class Bucket(BaseModel):
    model_config = ConfigDict(frozen=True)

    label: str
    kind: BucketKind
    lo: int | None
    hi: int | None
    threshold: int | None
    yes_token_id: str
    best_ask: float | None
    best_bid: float | None
    yes_price: float


def parse_bucket(market: dict[str, Any]) -> Bucket:
    label = market["groupItemTitle"]
    kind, lo, hi, threshold = parse_bucket_label(label)
    token_ids = json.loads(market["clobTokenIds"])
    yes_price = float(json.loads(market["outcomePrices"])[0])
    return Bucket(
        label=label,
        kind=kind,
        lo=lo,
        hi=hi,
        threshold=threshold,
        yes_token_id=token_ids[0],
        best_ask=market.get("bestAsk"),
        best_bid=market.get("bestBid"),
        yes_price=yes_price,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_polymarket_markets.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add src/rainmaker/polymarket/markets.py tests/test_polymarket_markets.py
git commit -m "feat: parse a Polymarket bucket market into a typed Bucket"
```

---

### Task 4: Market model and parse_market (target resolution + station check)

**Files:**
- Modify: `src/rainmaker/polymarket/markets.py`
- Modify: `tests/test_polymarket_markets.py`

- [ ] **Step 1: Write the failing test (append; add imports at top)**

```python
from datetime import date

import pytest

from rainmaker.polymarket.markets import Market, parse_market, parse_variable


def test_parse_variable():
    assert parse_variable("Highest temperature in NYC on May 30?") == "TMAX"
    assert parse_variable("Lowest temperature in Miami on May 29?") == "TMIN"


def test_parse_market_nyc():
    m = parse_market(_nyc_event())
    assert isinstance(m, Market)
    assert m.id == "533147"
    assert m.target.station.icao == "KLGA"
    assert m.target.variable == "TMAX"
    assert m.target.local_date == date(2026, 5, 30)
    assert len(m.buckets) == 11
    assert m.buckets[0].kind == "below"


def test_parse_market_unknown_city_raises():
    event = dict(_nyc_event())
    event["title"] = "Highest temperature in Atlantis on May 30?"
    with pytest.raises(KeyError):
        parse_market(event)


def test_parse_market_station_mismatch_raises():
    event = dict(_nyc_event())
    event["description"] = "resolves at some other station, no icao here"
    with pytest.raises(ValueError, match="resolution station"):
        parse_market(event)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_polymarket_markets.py -v`
Expected: FAIL with `ImportError: cannot import name 'Market'`.

- [ ] **Step 3: Add to `markets.py`**

Add imports at the top:

```python
from datetime import datetime
from zoneinfo import ZoneInfo

from rainmaker.config import STATIONS, Target, Variable, build_target
```

Add after `parse_bucket`:

```python
_TITLE_RE = re.compile(r"(highest|lowest) temperature in (.+?) on .+", re.IGNORECASE)


class Market(BaseModel):
    id: str
    slug: str
    title: str
    target: Target
    buckets: list[Bucket]


def parse_variable(title: str) -> Variable:
    match = _TITLE_RE.match(title)
    if match is None:
        raise ValueError(f"not a temperature market title: {title!r}")
    return "TMAX" if match.group(1).lower() == "highest" else "TMIN"


def parse_city(title: str) -> str:
    match = _TITLE_RE.match(title)
    if match is None:
        raise ValueError(f"not a temperature market title: {title!r}")
    return match.group(2).strip()


def parse_market(event: dict[str, Any]) -> Market:
    title = event["title"]
    city = parse_city(title)
    variable = parse_variable(title)
    station = STATIONS[city]  # KeyError for an unknown city is intended
    if station.icao not in event["description"]:
        raise ValueError(
            f"resolution station {station.icao} not named in market {event['id']} rules"
        )
    end = datetime.fromisoformat(event["endDate"])
    local_date = end.astimezone(ZoneInfo(station.timezone)).date()
    target = build_target(city, variable, local_date)
    buckets = [parse_bucket(m) for m in event["markets"]]
    return Market(id=event["id"], slug=event["slug"], title=title, target=target, buckets=buckets)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_polymarket_markets.py -v`
Expected: PASS (9 passed).

- [ ] **Step 5: Commit**

```bash
git add src/rainmaker/polymarket/markets.py tests/test_polymarket_markets.py
git commit -m "feat: parse a Polymarket event into a Market with resolved target"
```

---

### Task 5: Gamma client fetch and discover_markets (mocked I/O)

**Files:**
- Create: `src/rainmaker/polymarket/client.py`
- Test: `tests/test_polymarket_client.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_polymarket_client.py
import json
import re
from pathlib import Path

import httpx
import pytest

from rainmaker.polymarket.client import GAMMA_EVENTS_URL, discover_markets, fetch_weather_events

FIXTURES = Path(__file__).parent / "fixtures"


def _events_body() -> list[dict]:
    return json.loads((FIXTURES / "polymarket_weather_events.json").read_text())


def test_discover_markets_filters_to_us_temp_markets(httpx_mock):
    httpx_mock.add_response(url=re.compile(re.escape(GAMMA_EVENTS_URL)), json=_events_body())
    with httpx.Client() as client:
        markets = discover_markets(client)
    assert len(markets) == 1
    assert markets[0].id == "533147"
    assert markets[0].target.station.icao == "KLGA"
    assert len(markets[0].buckets) == 11


def test_fetch_weather_events_raises_when_gamma_down(httpx_mock):
    httpx_mock.add_response(url=re.compile(re.escape(GAMMA_EVENTS_URL)), status_code=500)
    with httpx.Client() as client:
        with pytest.raises(httpx.HTTPStatusError):
            fetch_weather_events(client)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_polymarket_client.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'rainmaker.polymarket.client'`.

- [ ] **Step 3: Write `src/rainmaker/polymarket/client.py`**

```python
from typing import Any

import httpx

from rainmaker.config import STATIONS
from rainmaker.polymarket.markets import Market, parse_market

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"


def fetch_weather_events(
    client: httpx.Client, *, page_size: int = 100, max_pages: int = 6
) -> list[dict[str, Any]]:
    """Page through Gamma's active weather events. Raises on any HTTP error."""
    events: list[dict[str, Any]] = []
    for page in range(max_pages):
        resp = client.get(
            GAMMA_EVENTS_URL,
            params={
                "closed": "false",
                "active": "true",
                "tag_slug": "weather",
                "limit": str(page_size),
                "offset": str(page * page_size),
            },
        )
        resp.raise_for_status()
        batch = resp.json()
        events.extend(batch)
        if len(batch) < page_size:
            break
    return events


def _is_us_temp_event(event: dict[str, Any]) -> bool:
    from rainmaker.polymarket.markets import _TITLE_RE

    match = _TITLE_RE.match(event.get("title", ""))
    if match is None:
        return False
    return match.group(2).strip() in STATIONS


def discover_markets(client: httpx.Client) -> list[Market]:
    """Fetch live weather events and parse the US-city temperature markets."""
    return [parse_market(ev) for ev in fetch_weather_events(client) if _is_us_temp_event(ev)]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_polymarket_client.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/rainmaker/polymarket/client.py tests/test_polymarket_client.py
git commit -m "feat: discover US-city temperature markets from Gamma"
```

---

### Task 6: Full check suite, live smoke

**Files:** none (verification + smoke only)

- [ ] **Step 1: Run the full check suite**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest -q`
Expected: all pass. Fix any ruff/mypy issues inline and re-run. If `ruff format --check` fails, run `uv run ruff format .` and include the changes.

Note on `_is_us_temp_event` importing the private `_TITLE_RE`: if ruff or review prefers, expose a small public helper `is_temperature_title(title) -> bool` in `markets.py` and call that instead. Either is acceptable; keep it consistent and lint-clean.

- [ ] **Step 2: Live smoke (manual, not a test)**

Run:
```bash
uv run python -c "import httpx; from rainmaker.polymarket.client import discover_markets; \
c=httpx.Client(timeout=30); ms=discover_markets(c); c.close(); \
print(len(ms), 'markets'); [print(m.id, m.target.station.icao, m.target.variable, m.target.local_date, len(m.buckets), 'buckets') for m in ms]"
```
Expected: prints the live US-city temperature markets currently in the registry (NYC/KLGA today), each with its settlement date and 11 buckets. If Gamma is unreachable it raises (the abort path); report what you saw. (Live market set changes daily; the exact ids will differ from the fixture.)

- [ ] **Step 3: Push and open/confirm the draft PR**

```bash
git push -u origin feat/5a-polymarket-client
# draft PR with "Closes #5"; mark ready once checks are green and review passes
```

---

## Self-Review

**Spec coverage (issue #5 "outcome mapping" inputs + spec "Discover markets"/"Resolve settlement target"/data model `markets`):**
- Discover markets via Gamma -> Task 5 (`discover_markets`, paginated `fetch_weather_events`). Read-only, no CLOB integration needed (prices via Gamma `bestAsk`/`bestBid`). Resolve settlement target (station id, variable, local date) -> Task 4 (`parse_market` builds `Target`, verifies station). Outcome spec (buckets) -> Tasks 2-3 (`parse_bucket_label`, `parse_bucket`, `Bucket`). Abort when Polymarket down -> Task 5 (`fetch_weather_events` raises; test covers 500). Tested against a saved fixture, never live -> all parser tests use the committed fixture; client I/O mocked with pytest-httpx.
- Deferred and called out: `raw` JSON audit column and persistence (Phase 3); TMIN forecast support (PR-B skips TMIN); CLOB order-book depth (not needed, Gamma surfaces best ask).

**Placeholder scan:** No TBD/TODO. Every code/test step shows complete code. The one optional note (Task 6 Step 1, exposing `is_temperature_title` instead of importing `_TITLE_RE`) is an explicit lint-cleanliness choice, not a gap.

**Type consistency:** `Bucket` fields (label, kind, lo, hi, threshold, yes_token_id, best_ask, best_bid, yes_price) are defined in Task 3 and used identically in Tasks 3 and 5 assertions. `Market` fields (id, slug, title, target, buckets) defined in Task 4, used in Tasks 4-5. `parse_bucket_label` returns `(kind, lo, hi, threshold)` consistently (Tasks 2-3). `parse_market`/`parse_variable`/`parse_city` signatures match their tests. `Target`/`Variable`/`STATIONS`/`build_target` reused from `config.py` as defined in Phase 1.
