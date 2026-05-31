# Phase 2b: Probability engine, outcome mapping, edge ranking, report

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn pooled forecasts plus discovered markets into a daily report of temperature-bucket bets ranked by edge, with a confidence floor and minimum-source gate, printed to the terminal and written to dated markdown/JSON.

**Architecture:** Four small layers stacked into a DAG. `probability/distribution.py` fits a Gaussian from the Phase 1 `ForecastSet`. `probability/outcomes.py` integrates that Gaussian over each bucket with continuity correction for whole-degree-F settlement. `ranking/edge.py` composes those into a per-market evaluation (`evaluate_market`) that computes edge against the CLOB best ask and applies the gates. `report/render.py` renders the ranked results to terminal/markdown/JSON. `cli.py` wires the live pipeline (discover markets -> forecast each -> evaluate -> render -> write), aborting if Polymarket is down and skipping markets whose variable the forecast layer does not support.

**Tech Stack:** Python 3.11+, numpy + scipy (new), pydantic v2, httpx; pytest + pytest-httpx, ruff, mypy.

**Scope:** PR-B of Phase 2 (issue #5). Uncalibrated: the Gaussian uses the raw pooled mean/std with a sigma floor; per-(station,variable,lead) bias/spread correction is Phase 4. No persistence (Phase 3) beyond writing the report files. The forecast layer supports `TMAX` only; `TMIN` markets are discovered (PR-A) but skipped here with a note.

**Decisions carried from brainstorming:**
- Distribution: equal-weight pooled mean/std with a configurable `MIN_SIGMA_F` floor (overconfidence is knowingly uncorrected until Phase 4; low coverage/spread shows as low confidence and can fail the gate).
- Outcome mapping: continuity-corrected for whole-degree-F settlement. `"A-B"` -> `[A-0.5, B+0.5)`; `"X or below"` -> `(-inf, X+0.5]`; `"Y or higher"` -> `[Y-0.5, +inf)`.
- Implied price: the YES `best_ask` from the `Bucket` (CLOB best ask via Gamma). A bucket with `best_ask` None or <= 0 has no executable ask and is excluded from ranking but listed as excluded.
- Gates: `recommended` requires `p_win >= CONFIDENCE_FLOOR` and `n_sources >= MIN_SOURCES` and `edge > 0`. `n_sources` = count of `ok` entries in the forecast coverage (max 2: nws, open-meteo).

**Existing types reused (do not redefine):**
- `rainmaker.forecasts.base`: `ForecastSample(source, model, member, station, variable, target_date, lead_time_days, value_f, issued_at)`, `ForecastSet(target, samples, coverage)`, `SourceCoverage(source, ok, n_samples, error)`.
- `rainmaker.forecasts.aggregate.aggregate(target, sources, now=None, freshness_limit_hours=...)`.
- `rainmaker.forecasts.nws.NwsSource`, `rainmaker.forecasts.openmeteo.OpenMeteoSource`.
- `rainmaker.polymarket.markets`: `Bucket(label, kind, lo, hi, threshold, yes_token_id, best_ask, best_bid, yes_price)`, `Market(id, slug, title, target, buckets)`. `kind in {"below","range","above"}`.
- `rainmaker.polymarket.client`: `discover_markets(client)`.
- `rainmaker.config`: `Target(station, variable, local_date)`, `STATIONS`, `NWS_USER_AGENT`, `build_target`.

---

### Task 1: Dependencies and config thresholds

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/rainmaker/config.py`
- Modify: `.gitignore`

- [ ] **Step 1: Add numpy + scipy to runtime deps in `pyproject.toml`**

Change the `dependencies` array to:

```toml
dependencies = [
    "httpx>=0.27",
    "pydantic>=2.7",
    "numpy>=2.0",
    "scipy>=1.13",
]
```

- [ ] **Step 2: Sync and confirm imports resolve**

Run: `uv sync`
Expected: installs numpy and scipy, updates `uv.lock`, no error.

Run: `uv run python -c "import numpy, scipy.stats"`
Expected: no output, exit 0.

- [ ] **Step 3: Add engine thresholds to `src/rainmaker/config.py`**

Append after the existing source-config constants (after `FRESHNESS_LIMIT_HOURS = 24`):

```python
# Probability engine + ranking thresholds (uncalibrated; tune in Phase 4)
MIN_SIGMA_F = 1.5
CONFIDENCE_FLOOR = 0.90
MIN_SOURCES = 2
REPORTS_DIR = "reports"
```

- [ ] **Step 4: Ignore the reports output directory in `.gitignore`**

Append a line:

```gitignore
reports/
```

- [ ] **Step 5: Verify nothing broke**

Run: `uv run ruff check . && uv run mypy src && uv run pytest -q`
Expected: all pass (39 tests still pass).

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock src/rainmaker/config.py .gitignore
git commit -m "build: add numpy/scipy and engine thresholds for Phase 2b"
```

---

### Task 2: Predictive distribution

**Files:**
- Create: `src/rainmaker/probability/__init__.py` (empty)
- Create: `src/rainmaker/probability/distribution.py`
- Test: `tests/test_distribution.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_distribution.py
from datetime import date

import pytest

from rainmaker.forecasts.base import ForecastSample
from rainmaker.probability.distribution import Gaussian, fit_gaussian


def _sample(value_f: float) -> ForecastSample:
    return ForecastSample(
        source="x", model="m", member=None, station="KLGA", variable="TMAX",
        target_date=date(2026, 5, 31), lead_time_days=1, value_f=value_f, issued_at=None,
    )


def test_fit_gaussian_mean_and_std():
    g = fit_gaussian([_sample(68), _sample(70), _sample(72)], min_sigma=0.5)
    assert g.mu == pytest.approx(70.0)
    assert g.sigma == pytest.approx(2.0)  # sample std (ddof=1) of 68,70,72


def test_fit_gaussian_applies_sigma_floor():
    g = fit_gaussian([_sample(70.0), _sample(70.1)], min_sigma=1.5)
    assert g.mu == pytest.approx(70.05)
    assert g.sigma == 1.5  # raw std ~0.07 floored to 1.5


def test_fit_gaussian_single_sample_uses_floor():
    g = fit_gaussian([_sample(70.0)], min_sigma=1.5)
    assert g.mu == 70.0
    assert g.sigma == 1.5


def test_fit_gaussian_empty_raises():
    with pytest.raises(ValueError, match="no samples"):
        fit_gaussian([], min_sigma=1.5)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_distribution.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'rainmaker.probability.distribution'`.

- [ ] **Step 3: Write `src/rainmaker/probability/distribution.py`**

```python
import numpy as np
from pydantic import BaseModel

from rainmaker.config import MIN_SIGMA_F
from rainmaker.forecasts.base import ForecastSample


class Gaussian(BaseModel):
    mu: float
    sigma: float


def fit_gaussian(samples: list[ForecastSample], min_sigma: float = MIN_SIGMA_F) -> Gaussian:
    """Fit an uncalibrated Gaussian to the pooled sample values.

    Equal-weight mean and sample std (ddof=1), with sigma floored at min_sigma so a
    low-variance pool cannot produce false certainty. Spread is knowingly
    overconfident here; the bias/spread correction is Phase 4.
    """
    if not samples:
        raise ValueError("cannot fit a distribution with no samples")
    values = np.array([s.value_f for s in samples], dtype=float)
    mu = float(values.mean())
    sigma = float(values.std(ddof=1)) if values.size >= 2 else 0.0
    return Gaussian(mu=mu, sigma=max(sigma, min_sigma))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_distribution.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/rainmaker/probability/__init__.py src/rainmaker/probability/distribution.py tests/test_distribution.py
git commit -m "feat: fit uncalibrated Gaussian from pooled forecast samples"
```

---

### Task 3: Outcome probability (continuity-corrected)

**Files:**
- Create: `src/rainmaker/probability/outcomes.py`
- Test: `tests/test_outcomes.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_outcomes.py
import pytest

from rainmaker.polymarket.markets import Bucket
from rainmaker.probability.distribution import Gaussian
from rainmaker.probability.outcomes import bucket_probability


def _bucket(kind, lo=None, hi=None, threshold=None) -> Bucket:
    return Bucket(
        label="x", kind=kind, lo=lo, hi=hi, threshold=threshold,
        yes_token_id="t", best_ask=None, best_bid=None, yes_price=0.0,
    )


def test_range_probability_continuity_corrected():
    g = Gaussian(mu=70.0, sigma=2.0)
    # [69.5, 71.5): CDF(71.5) - CDF(69.5) for N(70,2)
    p = bucket_probability(g, _bucket("range", lo=70, hi=71))
    assert p == pytest.approx(0.37208, abs=1e-4)


def test_below_and_above_are_complementary_at_shared_edge():
    g = Gaussian(mu=70.0, sigma=2.0)
    # "70 or below" -> CDF(70.5); "71 or higher" -> 1 - CDF(70.5); they share edge 70.5
    p_below = bucket_probability(g, _bucket("below", threshold=70))
    p_above = bucket_probability(g, _bucket("above", threshold=71))
    assert p_below + p_above == pytest.approx(1.0)


def test_full_bucket_partition_sums_to_one():
    g = Gaussian(mu=70.5, sigma=3.0)
    buckets = [
        _bucket("below", threshold=59),
        *[_bucket("range", lo=lo, hi=lo + 1) for lo in range(60, 78, 2)],
        _bucket("above", threshold=78),
    ]
    total = sum(bucket_probability(g, b) for b in buckets)
    assert total == pytest.approx(1.0, abs=1e-9)


def test_mode_bucket_has_highest_probability():
    g = Gaussian(mu=70.5, sigma=2.0)
    buckets = [
        _bucket("below", threshold=59),
        *[_bucket("range", lo=lo, hi=lo + 1) for lo in range(60, 78, 2)],
        _bucket("above", threshold=78),
    ]
    probs = {(b.lo, b.hi): bucket_probability(g, b) for b in buckets}
    assert max(probs, key=probs.get) == (70, 71)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_outcomes.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'rainmaker.probability.outcomes'`.

- [ ] **Step 3: Write `src/rainmaker/probability/outcomes.py`**

```python
from scipy.stats import norm

from rainmaker.polymarket.markets import Bucket
from rainmaker.probability.distribution import Gaussian


def bucket_probability(g: Gaussian, bucket: Bucket) -> float:
    """P(settled value falls in this bucket), continuity-corrected.

    Settlement rounds to whole degrees F, so bucket "A-B" captures true temperatures
    in [A-0.5, B+0.5); "X or below" is (-inf, X+0.5]; "Y or higher" is [Y-0.5, +inf).
    """
    def cdf(x: float) -> float:
        return float(norm.cdf(x, loc=g.mu, scale=g.sigma))

    if bucket.kind == "below":
        assert bucket.threshold is not None
        return cdf(bucket.threshold + 0.5)
    if bucket.kind == "above":
        assert bucket.threshold is not None
        return 1.0 - cdf(bucket.threshold - 0.5)
    assert bucket.lo is not None and bucket.hi is not None
    return cdf(bucket.hi + 0.5) - cdf(bucket.lo - 0.5)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_outcomes.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/rainmaker/probability/outcomes.py tests/test_outcomes.py
git commit -m "feat: integrate Gaussian over buckets with continuity correction"
```

---

### Task 4: Edge ranking and per-market evaluation

**Files:**
- Create: `src/rainmaker/ranking/__init__.py` (empty)
- Create: `src/rainmaker/ranking/edge.py`
- Test: `tests/test_edge.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_edge.py
from datetime import date

from rainmaker.config import build_target
from rainmaker.forecasts.base import ForecastSample, ForecastSet, SourceCoverage
from rainmaker.polymarket.markets import Bucket, Market
from rainmaker.ranking.edge import MarketReport, RankedOutcome, evaluate_market


def _bucket(label, kind, *, lo=None, hi=None, threshold=None, best_ask=None) -> Bucket:
    return Bucket(
        label=label, kind=kind, lo=lo, hi=hi, threshold=threshold,
        yes_token_id="t", best_ask=best_ask, best_bid=None, yes_price=0.0,
    )


def _market(buckets) -> Market:
    return Market(
        id="m1", slug="s", title="Highest temperature in NYC on May 31?",
        target=build_target("NYC", "TMAX", date(2026, 5, 31)), buckets=buckets,
    )


def _forecast_set(values, *, ok_sources=("nws", "open-meteo")) -> ForecastSet:
    samples = [
        ForecastSample(
            source="nws", model="m", member=None, station="KLGA", variable="TMAX",
            target_date=date(2026, 5, 31), lead_time_days=1, value_f=v, issued_at=None,
        )
        for v in values
    ]
    coverage = [SourceCoverage(source=s, ok=True, n_samples=len(values)) for s in ok_sources]
    return ForecastSet(target=samples[0].target if samples else None, samples=samples, coverage=coverage)


def test_evaluate_market_ranks_by_edge_and_flags_recommended():
    # Forecast centered at 70.5 -> mode bucket 70-71 has high P(win).
    market = _market([
        _bucket("70-71°F", "range", lo=70, hi=71, best_ask=0.40),  # cheap mode -> big edge
        _bucket("72-73°F", "range", lo=72, hi=73, best_ask=0.30),
    ])
    fs = _forecast_set([69, 70, 71, 72])  # mean 70.5
    report = evaluate_market(market, fs, floor=0.50, min_sources=2, min_sigma=1.5)
    assert isinstance(report, MarketReport)
    assert report.n_sources == 2
    assert [o.bucket_label for o in report.outcomes] == ["70-71°F", "72-73°F"]  # sorted by edge desc
    top = report.outcomes[0]
    assert top.edge > 0
    assert top.recommended is True


def test_recommended_requires_confidence_floor():
    market = _market([_bucket("70-71°F", "range", lo=70, hi=71, best_ask=0.05)])
    fs = _forecast_set([60, 80])  # wide spread -> low P on any single 2-degree bucket
    report = evaluate_market(market, fs, floor=0.90, min_sources=2, min_sigma=1.5)
    o = report.outcomes[0]
    assert o.edge > 0          # cheap ask, positive edge
    assert o.p_win < 0.90
    assert o.recommended is False  # fails the confidence floor


def test_recommended_requires_min_sources():
    market = _market([_bucket("70-71°F", "range", lo=70, hi=71, best_ask=0.05)])
    fs = _forecast_set([70, 70, 71, 71], ok_sources=("nws",))  # only 1 source
    report = evaluate_market(market, fs, floor=0.50, min_sources=2, min_sigma=1.5)
    assert report.n_sources == 1
    assert report.outcomes[0].recommended is False


def test_bucket_without_ask_is_excluded_not_ranked():
    market = _market([
        _bucket("70-71°F", "range", lo=70, hi=71, best_ask=None),
        _bucket("72-73°F", "range", lo=72, hi=73, best_ask=0.20),
    ])
    fs = _forecast_set([70, 71, 72])
    report = evaluate_market(market, fs, floor=0.50, min_sources=2, min_sigma=1.5)
    assert [o.bucket_label for o in report.outcomes] == ["72-73°F"]
    assert report.excluded_no_ask == ["70-71°F"]


def test_evaluate_market_no_samples_yields_empty_outcomes():
    market = _market([_bucket("70-71°F", "range", lo=70, hi=71, best_ask=0.20)])
    fs = ForecastSet(
        target=build_target("NYC", "TMAX", date(2026, 5, 31)),
        samples=[],
        coverage=[SourceCoverage(source="nws", ok=False, n_samples=0, error="down")],
    )
    report = evaluate_market(market, fs, floor=0.50, min_sources=2, min_sigma=1.5)
    assert report.outcomes == []
    assert report.mu is None and report.sigma is None
    assert report.n_sources == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_edge.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'rainmaker.ranking.edge'`.

- [ ] **Step 3: Write `src/rainmaker/ranking/edge.py`**

```python
from datetime import date

from pydantic import BaseModel

from rainmaker.forecasts.base import ForecastSet, SourceCoverage
from rainmaker.polymarket.markets import Market
from rainmaker.probability.distribution import fit_gaussian
from rainmaker.probability.outcomes import bucket_probability


class RankedOutcome(BaseModel):
    bucket_label: str
    p_win: float
    best_ask: float
    edge: float
    recommended: bool


class MarketReport(BaseModel):
    market_id: str
    title: str
    station: str
    variable: str
    settlement_date: date
    mu: float | None
    sigma: float | None
    n_sources: int
    coverage: list[SourceCoverage]
    outcomes: list[RankedOutcome]
    excluded_no_ask: list[str]


def evaluate_market(
    market: Market,
    forecast_set: ForecastSet,
    *,
    floor: float,
    min_sources: int,
    min_sigma: float,
) -> MarketReport:
    n_sources = sum(1 for c in forecast_set.coverage if c.ok)
    base = MarketReport(
        market_id=market.id,
        title=market.title,
        station=market.target.station.icao,
        variable=market.target.variable,
        settlement_date=market.target.local_date,
        mu=None,
        sigma=None,
        n_sources=n_sources,
        coverage=forecast_set.coverage,
        outcomes=[],
        excluded_no_ask=[],
    )
    if not forecast_set.samples:
        return base

    gaussian = fit_gaussian(forecast_set.samples, min_sigma=min_sigma)
    outcomes: list[RankedOutcome] = []
    excluded: list[str] = []
    for bucket in market.buckets:
        if bucket.best_ask is None or bucket.best_ask <= 0:
            excluded.append(bucket.label)
            continue
        p_win = bucket_probability(gaussian, bucket)
        edge = p_win - bucket.best_ask
        recommended = p_win >= floor and n_sources >= min_sources and edge > 0
        outcomes.append(
            RankedOutcome(
                bucket_label=bucket.label,
                p_win=p_win,
                best_ask=bucket.best_ask,
                edge=edge,
                recommended=recommended,
            )
        )
    outcomes.sort(key=lambda o: o.edge, reverse=True)
    return base.model_copy(
        update={"mu": gaussian.mu, "sigma": gaussian.sigma, "outcomes": outcomes, "excluded_no_ask": excluded}
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_edge.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add src/rainmaker/ranking/__init__.py src/rainmaker/ranking/edge.py tests/test_edge.py
git commit -m "feat: evaluate a market into edge-ranked outcomes with gates"
```

---

### Task 5: Report rendering

**Files:**
- Create: `src/rainmaker/report/__init__.py` (empty)
- Create: `src/rainmaker/report/render.py`
- Test: `tests/test_render.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_render.py
from datetime import date

from rainmaker.forecasts.base import SourceCoverage
from rainmaker.ranking.edge import MarketReport, RankedOutcome
from rainmaker.report.render import Report, render_markdown, render_terminal


def _market_report() -> MarketReport:
    return MarketReport(
        market_id="m1",
        title="Highest temperature in NYC on May 31?",
        station="KLGA",
        variable="TMAX",
        settlement_date=date(2026, 5, 31),
        mu=70.5,
        sigma=2.0,
        n_sources=2,
        coverage=[SourceCoverage(source="nws", ok=True, n_samples=1),
                  SourceCoverage(source="open-meteo", ok=True, n_samples=124)],
        outcomes=[
            RankedOutcome(bucket_label="70-71°F", p_win=0.93, best_ask=0.40, edge=0.53, recommended=True),
            RankedOutcome(bucket_label="72-73°F", p_win=0.04, best_ask=0.30, edge=-0.26, recommended=False),
        ],
        excluded_no_ask=["59°F or below"],
    )


def test_report_json_round_trips():
    report = Report(run_date=date(2026, 5, 31), markets=[_market_report()])
    data = report.model_dump(mode="json")
    assert data["run_date"] == "2026-05-31"
    assert data["markets"][0]["outcomes"][0]["bucket_label"] == "70-71°F"
    assert data["markets"][0]["outcomes"][0]["recommended"] is True


def test_render_terminal_shows_key_columns_and_recommended_marker():
    report = Report(run_date=date(2026, 5, 31), markets=[_market_report()])
    text = render_terminal(report)
    assert "KLGA" in text
    assert "70-71°F" in text
    assert "0.93" in text  # p_win
    assert "0.40" in text  # best ask
    assert "0.53" in text  # edge
    assert "REC" in text   # recommended marker on the recommended row
    assert "59°F or below" in text  # excluded note


def test_render_markdown_has_table_and_settlement_date():
    report = Report(run_date=date(2026, 5, 31), markets=[_market_report()])
    md = render_markdown(report)
    assert md.startswith("# Rainmaker report 2026-05-31")
    assert "| bucket | P(win) | ask | edge | rec |" in md
    assert "2026-05-31" in md
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_render.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'rainmaker.report.render'`.

- [ ] **Step 3: Write `src/rainmaker/report/render.py`**

```python
from datetime import date

from pydantic import BaseModel

from rainmaker.ranking.edge import MarketReport


class Report(BaseModel):
    run_date: date
    markets: list[MarketReport]


def _coverage_str(report: MarketReport) -> str:
    return ", ".join(
        f"{c.source}={'ok' if c.ok else 'FAILED'}({c.n_samples})" for c in report.coverage
    )


def render_terminal(report: Report) -> str:
    lines: list[str] = [f"Rainmaker report {report.run_date.isoformat()}", ""]
    for m in report.markets:
        lines.append(f"{m.title}  [{m.station} {m.variable} {m.settlement_date.isoformat()}]")
        if m.mu is not None and m.sigma is not None:
            lines.append(f"  forecast: mu={m.mu:.1f}F sigma={m.sigma:.1f}F  sources={m.n_sources}")
        lines.append(f"  coverage: {_coverage_str(m)}")
        if not m.outcomes:
            lines.append("  no tradeable outcomes (insufficient forecast data)")
        else:
            lines.append(f"  {'bucket':16} {'P(win)':>7} {'ask':>6} {'edge':>7}  rec")
            for o in m.outcomes:
                marker = "REC" if o.recommended else ""
                lines.append(
                    f"  {o.bucket_label:16} {o.p_win:>7.2f} {o.best_ask:>6.2f} {o.edge:>+7.2f}  {marker}"
                )
        if m.excluded_no_ask:
            lines.append(f"  excluded (no ask): {', '.join(m.excluded_no_ask)}")
        lines.append("")
    return "\n".join(lines)


def render_markdown(report: Report) -> str:
    lines: list[str] = [f"# Rainmaker report {report.run_date.isoformat()}", ""]
    for m in report.markets:
        lines.append(f"## {m.title}")
        lines.append("")
        lines.append(f"- station: {m.station}  variable: {m.variable}  settlement: {m.settlement_date.isoformat()}")
        if m.mu is not None and m.sigma is not None:
            lines.append(f"- forecast: mu={m.mu:.1f}F sigma={m.sigma:.1f}F  sources: {m.n_sources}")
        lines.append(f"- coverage: {_coverage_str(m)}")
        lines.append("")
        if m.outcomes:
            lines.append("| bucket | P(win) | ask | edge | rec |")
            lines.append("|--------|--------|-----|------|-----|")
            for o in m.outcomes:
                rec = "yes" if o.recommended else ""
                lines.append(
                    f"| {o.bucket_label} | {o.p_win:.2f} | {o.best_ask:.2f} | {o.edge:+.2f} | {rec} |"
                )
        else:
            lines.append("_no tradeable outcomes (insufficient forecast data)_")
        if m.excluded_no_ask:
            lines.append("")
            lines.append(f"Excluded (no ask): {', '.join(m.excluded_no_ask)}")
        lines.append("")
    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_render.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/rainmaker/report/__init__.py src/rainmaker/report/render.py tests/test_render.py
git commit -m "feat: render edge-ranked report to terminal and markdown"
```

---

### Task 6: CLI pipeline rewrite

**Files:**
- Modify: `src/rainmaker/cli.py` (replace the Phase 1 forecast-dump `run` with the advisory pipeline)
- Test: `tests/test_cli.py` (replace)

- [ ] **Step 1: Write the failing test (replace `tests/test_cli.py` entirely)**

```python
# tests/test_cli.py
from datetime import date

import httpx
import pytest

from rainmaker import cli
from rainmaker.config import build_target
from rainmaker.forecasts.base import ForecastSample, ForecastSet, SourceCoverage
from rainmaker.polymarket.markets import Bucket, Market


def _market(variable: str) -> Market:
    return Market(
        id="m1", slug="s",
        title=f"{'Highest' if variable == 'TMAX' else 'Lowest'} temperature in NYC on May 31?",
        target=build_target("NYC", variable, date(2026, 5, 31)),
        buckets=[Bucket(label="70-71°F", kind="range", lo=70, hi=71, threshold=None,
                        yes_token_id="t", best_ask=0.40, best_bid=None, yes_price=0.0)],
    )


def _forecast_set() -> ForecastSet:
    target = build_target("NYC", "TMAX", date(2026, 5, 31))
    samples = [
        ForecastSample(source="nws", model="m", member=None, station="KLGA", variable="TMAX",
                       target_date=date(2026, 5, 31), lead_time_days=1, value_f=v, issued_at=None)
        for v in (69, 70, 71, 72)
    ]
    return ForecastSet(target=target, samples=samples,
                       coverage=[SourceCoverage(source="nws", ok=True, n_samples=4),
                                 SourceCoverage(source="open-meteo", ok=True, n_samples=4)])


def test_run_builds_report_and_writes_files(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "discover_markets", lambda client: [_market("TMAX")])
    monkeypatch.setattr(cli, "_forecast_for", lambda target, client: _forecast_set())
    monkeypatch.setattr(cli.httpx, "Client", lambda **kw: _DummyClient())

    cli.main(["run", "--reports-dir", str(tmp_path)])

    out = capsys.readouterr().out
    assert "70-71°F" in out
    assert "KLGA" in out
    written = sorted(p.name for p in tmp_path.iterdir())
    assert written == ["2026-05-31.json", "2026-05-31.md"]


def test_run_skips_unsupported_variable(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "discover_markets", lambda client: [_market("TMIN")])
    monkeypatch.setattr(cli.httpx, "Client", lambda **kw: _DummyClient())

    cli.main(["run", "--reports-dir", str(tmp_path)])

    out = capsys.readouterr().out
    assert "skipped" in out.lower()
    assert "TMIN" in out


def test_run_aborts_when_polymarket_down(monkeypatch, tmp_path):
    def _boom(client):
        raise httpx.HTTPStatusError("down", request=httpx.Request("GET", "x"), response=httpx.Response(500))

    monkeypatch.setattr(cli, "discover_markets", _boom)
    monkeypatch.setattr(cli.httpx, "Client", lambda **kw: _DummyClient())

    with pytest.raises(SystemExit) as exc:
        cli.main(["run", "--reports-dir", str(tmp_path)])
    assert exc.value.code != 0


class _DummyClient:
    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL (the new symbols `discover_markets`, `_forecast_for`, `--reports-dir` do not exist yet).

- [ ] **Step 3: Replace `src/rainmaker/cli.py`**

```python
import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

from rainmaker.config import (
    CONFIDENCE_FLOOR,
    MIN_SIGMA_F,
    MIN_SOURCES,
    NWS_USER_AGENT,
    REPORTS_DIR,
    Target,
)
from rainmaker.forecasts.aggregate import aggregate
from rainmaker.forecasts.base import ForecastSet
from rainmaker.forecasts.nws import NwsSource
from rainmaker.forecasts.openmeteo import OpenMeteoSource
from rainmaker.polymarket.client import discover_markets
from rainmaker.ranking.edge import evaluate_market
from rainmaker.report.render import Report, render_markdown, render_terminal

SUPPORTED_VARIABLES = {"TMAX"}


def _forecast_for(target: Target, client: httpx.Client) -> ForecastSet:
    return aggregate(target, [NwsSource(client), OpenMeteoSource(client)])


def _write_reports(report: Report, reports_dir: str) -> list[Path]:
    out = Path(reports_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = report.run_date.isoformat()
    md_path = out / f"{stamp}.md"
    json_path = out / f"{stamp}.json"
    md_path.write_text(render_markdown(report))
    json_path.write_text(json.dumps(report.model_dump(mode="json"), indent=2))
    return [md_path, json_path]


def _run(reports_dir: str) -> None:
    client = httpx.Client(headers={"User-Agent": NWS_USER_AGENT}, timeout=30.0)
    try:
        try:
            markets = discover_markets(client)
        except httpx.HTTPError as exc:
            print(f"Polymarket unavailable, aborting: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc

        market_reports = []
        for market in markets:
            if market.target.variable not in SUPPORTED_VARIABLES:
                print(f"skipped {market.id}: unsupported variable {market.target.variable}")
                continue
            forecast_set = _forecast_for(market.target, client)
            market_reports.append(
                evaluate_market(
                    market,
                    forecast_set,
                    floor=CONFIDENCE_FLOOR,
                    min_sources=MIN_SOURCES,
                    min_sigma=MIN_SIGMA_F,
                )
            )
    finally:
        client.close()

    run_date = datetime.now(UTC).date()
    report = Report(run_date=run_date, markets=market_reports)
    print(render_terminal(report))
    paths = _write_reports(report, reports_dir)
    print(f"wrote {paths[0]} and {paths[1]}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="rainmaker")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="produce the daily edge-ranked report")
    run.add_argument("--reports-dir", default=REPORTS_DIR, help="directory for dated md/json output")
    args = parser.parse_args(argv)

    if args.command == "run":
        _run(args.reports_dir)
```

Note: the test monkeypatches `cli.discover_markets` and `cli._forecast_for`, so they must be module-level names in `cli.py` (they are, via the import and the helper). The `run_date` uses the real clock; the test asserts files named by the market settlement date is wrong - it is named by `run_date`. To keep the test deterministic, ALSO monkeypatch the run date: see Step 3a.

- [ ] **Step 3a: Make the run date injectable for tests**

Adjust `_run` and `main` so the report date is testable. Change `_run(reports_dir)` to `_run(reports_dir, run_date)` and in `main` compute `run_date = datetime.now(UTC).date()` and pass it. Then update the three CLI tests to monkeypatch the clock by passing through `main`; simplest is to set the run date from the market when only one settlement date is present. To avoid clock-coupling in tests, change the run-date source to: `run_date = market_reports[0].settlement_date if market_reports else datetime.now(UTC).date()`. This makes `test_run_builds_report_and_writes_files` deterministic (settlement date 2026-05-31) without monkeypatching the clock. Implement that expression in `_run` instead of always using the wall clock.

Final `_run` run-date line:

```python
    run_date = market_reports[0].settlement_date if market_reports else datetime.now(UTC).date()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cli.py -v`
Expected: PASS (3 passed). The first test sees files `2026-05-31.md/.json` because the single market's settlement date drives `run_date`.

- [ ] **Step 5: Commit**

```bash
git add src/rainmaker/cli.py tests/test_cli.py
git commit -m "feat: rewrite run as the advisory pipeline (discover, forecast, rank, report)"
```

---

### Task 7: Golden end-to-end test, full suite, live smoke, docs

**Files:**
- Create: `tests/test_golden_e2e.py`
- Modify: `CLAUDE.md` (note the golden test now exists and the run output)

- [ ] **Step 1: Write the golden end-to-end test**

This chains distribution -> outcomes -> edge -> render on the committed fixture market plus a controlled synthetic forecast, with a hand-checkable expected ranking. It uses the real NYC fixture market (efficient: mode bucket priced ~0.999, so nothing is recommended) to prove the pipeline runs and the ranking/partition hold.

```python
# tests/test_golden_e2e.py
import json
from datetime import date
from pathlib import Path

from rainmaker.config import CONFIDENCE_FLOOR, MIN_SIGMA_F, MIN_SOURCES
from rainmaker.forecasts.base import ForecastSample, ForecastSet, SourceCoverage
from rainmaker.polymarket.markets import parse_market
from rainmaker.ranking.edge import evaluate_market
from rainmaker.report.render import Report, render_markdown

FIXTURES = Path(__file__).parent / "fixtures"


def _nyc_market():
    events = json.loads((FIXTURES / "polymarket_weather_events.json").read_text())
    return parse_market(next(e for e in events if e["id"] == "533147"))


def _forecast_set(target):
    # Controlled pool centered at 70.5F: mode is the 70-71 bucket.
    samples = [
        ForecastSample(source="nws", model="m", member=None, station="KLGA", variable="TMAX",
                       target_date=target.local_date, lead_time_days=1, value_f=v, issued_at=None)
        for v in (68, 69, 70, 71, 72, 73)
    ]
    return ForecastSet(target=target, samples=samples,
                       coverage=[SourceCoverage(source="nws", ok=True, n_samples=6),
                                 SourceCoverage(source="open-meteo", ok=True, n_samples=6)])


def test_golden_pipeline_on_fixture_market():
    market = _nyc_market()
    fs = _forecast_set(market.target)
    report = evaluate_market(market, fs, floor=CONFIDENCE_FLOOR, min_sources=MIN_SOURCES, min_sigma=MIN_SIGMA_F)

    # All 11 buckets had an ask in the fixture, so none are excluded.
    assert report.excluded_no_ask == []
    assert len(report.outcomes) == 11
    # P(win) over the full partition sums to ~1.
    assert abs(sum(o.p_win for o in report.outcomes) - 1.0) < 1e-6
    # The mode bucket 70-71 is priced ~0.999 in the fixture, so no positive-edge
    # recommendation survives: an efficient market yields nothing.
    assert all(not o.recommended for o in report.outcomes)
    # Ranking is sorted by edge descending.
    edges = [o.edge for o in report.outcomes]
    assert edges == sorted(edges, reverse=True)

    # The report renders without error and names the station + settlement date.
    md = render_markdown(Report(run_date=date(2026, 5, 30), markets=[report]))
    assert "KLGA" in md
    assert "2026-05-30" in md
```

- [ ] **Step 2: Run the golden test**

Run: `uv run pytest tests/test_golden_e2e.py -v`
Expected: PASS (1 passed).

- [ ] **Step 3: Run the full check suite**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest -q`
Expected: all pass. Fix any ruff/mypy issues inline (e.g. scipy has no stubs; if mypy reports `import-untyped` for scipy, add to `pyproject.toml` under `[[tool.mypy.overrides]]` a `module = "scipy.*"` with `ignore_missing_imports = true`). Re-run until green.

- [ ] **Step 4: Live smoke (manual, not a test)**

Run: `uv run rainmaker run --reports-dir /tmp/rainmaker-smoke`
Expected: prints the dated report header, one block per live NYC market (TMAX markets evaluated; TMIN markets reported as skipped), each with forecast mu/sigma, coverage, an edge-ranked bucket table, and writes `/tmp/rainmaker-smoke/<date>.md` and `.json`. If Polymarket is unreachable it prints the abort message and exits non-zero. Report exactly what you saw (paste the terminal output).

- [ ] **Step 5: Update CLAUDE.md**

In the "Repo layout" section, add the new packages under `src/rainmaker/`:

```
  probability/
    distribution.py   pooled samples -> Gaussian (uncalibrated, sigma floor)
    outcomes.py       integrate Gaussian over buckets (continuity-corrected)
  ranking/
    edge.py           evaluate_market -> edge-ranked outcomes + gates
  report/
    render.py         terminal + markdown/JSON report
  polymarket/
    client.py         Gamma discovery (read-only)
    markets.py        event JSON -> Market (target + buckets)
```

In the "What this repo is" status line, change "Phase 2 (probability engine) is next." to "Phase 2 complete: the pipeline produces a daily edge-ranked report. Phase 3 (SQLite persistence) is next."

Confirm the golden-test sentence in CLAUDE.md ("The golden end-to-end test ... keep it green") now points at a real test: it does (`tests/test_golden_e2e.py`).

- [ ] **Step 6: Commit**

```bash
git add tests/test_golden_e2e.py CLAUDE.md
git commit -m "test: add golden end-to-end pipeline test; update docs for Phase 2"
```

- [ ] **Step 7: Push and mark the PR ready**

```bash
git push -u origin feat/5b-probability-engine
# mark the draft PR ready once the full suite is green and review passes
```

---

## Self-Review

**Spec coverage (issue #5 + spec "The brains"):**
- distribution.py predictive distribution -> Task 2 (`fit_gaussian`, sigma floor). outcomes.py bucket/threshold integration -> Task 3 (`bucket_probability`, continuity-corrected, below/range/above). edge.py edge + confidence floor + min-source gate + rank -> Task 4 (`evaluate_market`, `RankedOutcome`). render.py terminal + dated markdown/JSON -> Task 5 + Task 6 (`_write_reports`). Polymarket-down abort -> Task 6 (`_run` catches `httpx.HTTPError`, exits non-zero). TMIN skip -> Task 6. Math TDD against synthetic known answers -> Tasks 2-4. One golden e2e -> Task 7.
- Deferred and called out: calibration / bias-spread correction (Phase 4); persistence to SQLite (Phase 3); binary precipitation and threshold-only markets (not present in the live temperature markets; the bucket kinds below/range/above cover them).

**Placeholder scan:** No TBD/TODO in the implementation. The Task 5 Step 1 shows a deliberately-discarded placeholder snippet immediately replaced by the real file ("Replace the whole file with"); the engineer writes only the real version. Task 6 Step 3a refines the run-date source to remove clock-coupling; the final expression is given explicitly.

**Type consistency:** `Gaussian(mu, sigma)` defined in Task 2, consumed in Tasks 3-4. `bucket_probability(g, bucket)` signature consistent (Tasks 3-4). `RankedOutcome(bucket_label, p_win, best_ask, edge, recommended)` and `MarketReport(market_id, title, station, variable, settlement_date, mu, sigma, n_sources, coverage, outcomes, excluded_no_ask)` defined in Task 4, consumed in Tasks 5-7. `Report(run_date, markets)` defined in Task 5, consumed in Tasks 6-7. `evaluate_market(market, forecast_set, *, floor, min_sources, min_sigma)` consistent across Tasks 4, 6, 7. Reused Phase 1/2a types (`ForecastSet`, `SourceCoverage`, `Market`, `Bucket`, `Target`, `build_target`, `aggregate`, `NwsSource`, `OpenMeteoSource`, `discover_markets`) match their shipped signatures.
