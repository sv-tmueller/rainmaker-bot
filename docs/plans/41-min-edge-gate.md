# Minimum-edge recommendation gate - implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop recommending near-worthless bets by requiring a minimum edge (default 0.05) in the recommendation gate.

**Architecture:** One new constant in `config.py`, one new required keyword on `evaluate_market` in `ranking/edge.py`, wired through `cli.py`. The gate `edge > 0` becomes `edge >= min_edge`. Because the keyword is required, every call site (one in cli, ten in tests) is updated in the same change.

**Tech Stack:** Python 3.11, pytest, ruff, mypy. No new dependencies.

Spec: `docs/superpowers/specs/2026-06-04-accuracy-visibility-and-display-design.md`
Issue: #41

---

### Task 1: The gate, TDD

**Files:**
- Modify: `src/rainmaker/config.py:159` (add `MIN_EDGE` after `MIN_SOURCES`)
- Modify: `src/rainmaker/ranking/edge.py:40-80` (signature + gate)
- Modify: `src/rainmaker/cli.py` (import + call site, lines 13-21 and 95-102)
- Test: `tests/test_edge.py` (two new tests, `min_edge=0.0` on the eight existing calls)
- Test: `tests/test_golden_e2e.py:6,49-51` (import `MIN_EDGE`, pass it)
- Test: `tests/test_store_record.py:4,62-64` (import `MIN_EDGE`, pass it)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_edge.py`:

```python
def test_recommended_requires_min_edge():
    # Near-certain bucket priced at 0.99: positive but tiny edge.
    market = _market([_bucket("69°F or below", "below", threshold=69, best_ask=0.99)])
    fs = _forecast_set([60, 60, 60, 60])  # far below threshold -> p_win ~1.0
    report = evaluate_market(market, fs, floor=0.90, min_sources=2, min_sigma=1.5, min_edge=0.05)
    o = report.outcomes[0]
    assert o.p_win > 0.99
    assert 0 < o.edge < 0.05
    assert o.recommended is False  # passes floor and sources, fails min edge


def test_recommended_passes_min_edge():
    # Same near-certain bucket priced at 0.90: edge ~0.10 clears the threshold.
    market = _market([_bucket("69°F or below", "below", threshold=69, best_ask=0.90)])
    fs = _forecast_set([60, 60, 60, 60])
    report = evaluate_market(market, fs, floor=0.90, min_sources=2, min_sigma=1.5, min_edge=0.05)
    o = report.outcomes[0]
    assert o.edge >= 0.05
    assert o.recommended is True
```

Why these numbers: samples `[60, 60, 60, 60]` have zero spread, so sigma hits
the 1.5 floor and mu is 60. The "below 69" bucket integrates the Gaussian up to
69.5 (continuity correction in `probability/outcomes.py`), z = 6.33, so p_win
is ~1.0 minus 6e-11. Edge is therefore ~0.01 at ask 0.99 and ~0.10 at ask 0.90.

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run pytest tests/test_edge.py -v -k min_edge`
Expected: both FAIL with `TypeError: evaluate_market() got an unexpected keyword argument 'min_edge'`

- [ ] **Step 3: Implement**

`src/rainmaker/config.py` - add below `MIN_SOURCES = 2` (line 159):

```python
MIN_EDGE = 0.05
```

`src/rainmaker/ranking/edge.py` - add the keyword to the signature (after
`min_sigma`):

```python
def evaluate_market(
    market: Market,
    forecast_set: ForecastSet,
    *,
    floor: float,
    min_sources: int,
    min_sigma: float,
    min_edge: float,
    calibration: Calibration | None = None,
) -> MarketReport:
```

and replace the gate (currently lines 77-80, including the stale comment about
the future tuning knob):

```python
        # recommended gates: confidence floor + min sources + minimum edge.
        # The edge threshold keeps near-worthless bets (pay 0.99 to win 0.01)
        # out of the recommendations.
        recommended = p_win >= floor and n_sources >= min_sources and edge >= min_edge
```

`src/rainmaker/cli.py` - add `MIN_EDGE` to the `rainmaker.config` import block
(alphabetical, after `DB_PATH`... it sorts between `DB_PATH` and `MIN_SIGMA_F`;
let ruff's isort fix the order) and pass it at the call site:

```python
            report = evaluate_market(
                market,
                forecast_set,
                floor=CONFIDENCE_FLOOR,
                min_sources=MIN_SOURCES,
                min_sigma=MIN_SIGMA_F,
                min_edge=MIN_EDGE,
                calibration=calibration,
            )
```

- [ ] **Step 4: Update the existing call sites in tests**

`tests/test_edge.py`: add `min_edge=0.0` to all eight pre-existing
`evaluate_market(...)` calls (the ones in tests written before this change).
0.0 rather than 0.05 keeps each of those tests focused on the single gate it
exercises. Example for the first:

```python
    report = evaluate_market(market, fs, floor=0.45, min_sources=2, min_sigma=1.5, min_edge=0.0)
```

`tests/test_golden_e2e.py`: extend the config import and the call:

```python
from rainmaker.config import CONFIDENCE_FLOOR, MIN_EDGE, MIN_SIGMA_F, MIN_SOURCES
```

```python
    report = evaluate_market(
        market,
        fs,
        floor=CONFIDENCE_FLOOR,
        min_sources=MIN_SOURCES,
        min_sigma=MIN_SIGMA_F,
        min_edge=MIN_EDGE,
    )
```

The golden assertion `all(not o.recommended for o in report.outcomes)` still
holds (the fixture market is efficiently priced; raising the bar cannot create
recommendations). No expected-output changes.

`tests/test_store_record.py`: same import extension and add
`min_edge=MIN_EDGE` to the single `evaluate_market` call at lines 62-64.

- [ ] **Step 5: Run the full test suite**

Run: `uv run pytest`
Expected: all tests PASS, including the two new ones and the golden e2e.

- [ ] **Step 6: Lint, format, type check**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy src`
Expected: no errors. If ruff format flags the edited lines, run
`uv run ruff format .` and re-check.

- [ ] **Step 7: Commit**

```bash
git add src/rainmaker/config.py src/rainmaker/ranking/edge.py src/rainmaker/cli.py tests/test_edge.py tests/test_golden_e2e.py tests/test_store_record.py
git commit -m "feat: require a minimum edge before recommending a bet

A bucket priced at 0.99 with p_win ~1.0 passed the old edge > 0 gate and
got recommended despite paying 0.01 on a 0.99 stake. Gate on
edge >= MIN_EDGE (0.05) so near-worthless bets stay out of the report."
```

---

## Verification (whole plan)

- `uv run pytest` green, including golden e2e.
- `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src` clean.
- Behavior check: the four 2026-06-04 dashboard rows (edges +0.00 to +0.01)
  would no longer be recommended by a rerun; nothing changes in stored
  historical rows.
