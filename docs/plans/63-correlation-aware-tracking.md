# Correlation-aware tracking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:executing-plans or
> subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Stop correlated NO bets from inflating the tracked P&L and hit-rate by
counting one bet per (market, run): the best-edge recommended side/bucket.

**Architecture:** All in `src/rainmaker/tracking.py`. `_settled_rows` gains
`market_id`, `run_id`, `edge`. A new `_best_per_market_run` collapses recommended
settled rows to one per (market, run) (max edge). `compute_pnl` and the
`hit_rate` in `compute_calibration` score over that collapsed set. Brier and
`n_scored` stay per-YES-bucket (NO twins were already excluded, so calibration
was never inflated). The dashboard headline tiles read `tracking_snapshot`, which
this writes, so they correct for free; the dashboard settled-bets list is a
per-bet display and is out of scope.

**Tech stack:** Python 3.11, pytest, sqlite (`:memory:` in tests).

---

### Task 1: Collapse to one bet per (market, run)

**Files:**
- Modify: `src/rainmaker/tracking.py` (`_settled_rows`, `compute_pnl`, `compute_calibration`, module docstring; add `_best_per_market_run`)
- Test: `tests/test_tracking.py`

- [ ] **Step 1: Update the shared `_setup` so best-edge is unambiguous**

In `tests/test_tracking.py`, give the two buckets distinct edges (the winner
gets the higher edge). Change the prediction loop in `_setup`:

```python
    for bucket, p_win, edge in (("70-71°F", 0.93, 0.20), ("72-73°F", 0.50, 0.10)):
        conn.execute(
            "INSERT INTO predictions "
            "(run_id, market_id, bucket, p_win, edge, recommended, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("r1", "m1", bucket, p_win, edge, 1, "t"),
        )
```

- [ ] **Step 2: Rewrite the two `_setup`-based assertions and add the collapse tests**

Replace `test_compute_pnl_sums_recommended_bets` and update
`test_compute_calibration_brier_and_hit_rate`; add three new tests:

```python
def test_compute_pnl_collapses_correlated_bets_to_best_edge():
    conn = connect(":memory:")
    _setup(conn)  # m1/r1: 70-71 (edge .20, ask .40, wins) and 72-73 (edge .10, loses)
    pnl = compute_pnl(conn)
    conn.close()
    assert pnl["n_bets"] == 1  # one bet per (market, run): the best edge
    assert (pnl["wins"], pnl["losses"]) == (1, 0)
    assert pnl["total_pnl"] == pytest.approx(0.60)  # 1 - 0.40
    assert pnl["roi"] == pytest.approx(0.60 / 0.40)


def test_compute_calibration_brier_unchanged_hit_rate_collapsed():
    conn = connect(":memory:")
    _setup(conn)
    cal = compute_calibration(conn)
    conn.close()
    # Brier still over both YES bucket-predictions (calibration was never inflated).
    assert cal["n"] == 2
    assert cal["brier"] == pytest.approx(((0.93 - 1) ** 2 + (0.50 - 0) ** 2) / 2)
    # Hit rate over the single best-edge bet (70-71, which won).
    assert cal["hit_rate"] == pytest.approx(1.0)


def _add_market(conn, market_id, run_id="r1"):
    conn.execute(
        "INSERT OR IGNORE INTO runs (id, started_at, status) VALUES (?, ?, ?)",
        (run_id, "t", "ok"),
    )
    conn.execute(
        "INSERT INTO markets (id, city, variable, settlement_date) VALUES (?, ?, ?, ?)",
        (market_id, "NYC", "TMAX", "2026-05-30"),
    )
    # Three correlated NO bets; actual 71 lands in 70-71, so 60-61 and 80-81 NO win.
    no_bets = (("60-61°F", 0.10, 0.97, 0.87), ("70-71°F", 0.20, 0.90, 0.70),
               ("80-81°F", 0.05, 0.99, 0.94))
    for bucket, no_ask, p_no, edge in no_bets:
        conn.execute(
            "INSERT INTO prices (run_id, market_id, outcome, side, price, implied_prob, "
            "captured_at) VALUES (?, ?, ?, 'NO', ?, ?, 't')",
            (run_id, market_id, bucket, no_ask, 1 - no_ask),
        )
        conn.execute(
            "INSERT INTO predictions (run_id, market_id, bucket, side, p_win, edge, "
            "recommended, created_at) VALUES (?, ?, ?, 'NO', ?, ?, 1, 't')",
            (run_id, market_id, bucket, p_no, edge),
        )
    conn.execute(
        "INSERT INTO outcomes (market_id, actual_value, settled_at) VALUES (?, ?, 't')",
        (market_id, 71.0),
    )


def test_correlated_no_bets_collapse_to_one():
    conn = connect(":memory:")
    init_schema(conn)
    _add_market(conn, "m1")
    conn.commit()
    pnl = compute_pnl(conn)
    conn.close()
    assert pnl["n_bets"] == 1  # three NO bets on one market-run -> one bet
    # Best edge is 80-81 NO (edge .94, ask .05); 71 not in 80-81, so it won.
    assert (pnl["wins"], pnl["losses"]) == (1, 0)
    assert pnl["total_pnl"] == pytest.approx(1 - 0.05)


def test_each_market_run_counted_once():
    conn = connect(":memory:")
    init_schema(conn)
    _add_market(conn, "m1")
    _add_market(conn, "m2")
    conn.commit()
    pnl = compute_pnl(conn)
    conn.close()
    assert pnl["n_bets"] == 2  # one per distinct market in the same run


def test_same_market_across_runs_counts_separately():
    conn = connect(":memory:")
    init_schema(conn)
    _add_market(conn, "m1", run_id="r1")
    _add_market(conn, "m1", run_id="r2")
    conn.commit()
    pnl = compute_pnl(conn)
    conn.close()
    assert pnl["n_bets"] == 2  # re-recommendation across runs stays separate
```

Also update `test_write_snapshot_persists_metrics` (uses `_setup`): change
`assert row["n_bets"] == 2` to `== 1` and `row["total_pnl"]` approx to `0.60`
(`n_scored` stays `2`).

- [ ] **Step 3: Run the new/updated tests, verify they FAIL**

Run: `uv run pytest tests/test_tracking.py -v`
Expected: the collapse tests FAIL (current code counts every row: `n_bets` 2/3, not 1).

- [ ] **Step 4: Implement the collapse in `tracking.py`**

Update the module docstring (lines 1-6) second sentence to:

```
One one-unit bet per (market, run): the best-edge recommended side/bucket. Buckets
on one market describe the same temperature, so correlated same-market bets
collapse to one; a market re-recommended across daily runs still counts once per
run. Tracking only covers rows with a bucket recorded.
```

Extend the `_settled_rows` SELECT to also return market_id, run_id, edge:

```python
    rows = conn.execute(
        "SELECT p.market_id AS market_id, p.run_id AS run_id, p.bucket AS bucket, "
        "p.side AS side, p.p_win AS p_win, p.edge AS edge, "
        "p.recommended AS recommended, pr.price AS ask, o.actual_value AS actual_value "
        "FROM predictions p "
        "JOIN outcomes o ON o.market_id = p.market_id "
        "JOIN prices pr ON pr.run_id = p.run_id AND pr.market_id = p.market_id "
        "AND pr.outcome = p.bucket "
        "AND COALESCE(pr.side, 'YES') = COALESCE(p.side, 'YES') "
        "WHERE p.bucket IS NOT NULL AND pr.price IS NOT NULL"
    ).fetchall()
```

Add the helper (after `_settled_rows`):

```python
def _best_per_market_run(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse recommended bets to one per (market, run): the highest-edge bet.

    Buckets on one market all describe the same temperature, so NO bets across
    buckets win or lose together. Counting each separately would inflate P&L and
    hit rate, so keep only the best-edge bet per (market, run). Tie-break on
    (edge, p_win, bucket, side) for a deterministic pick.
    """
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        if not r["recommended"]:
            continue
        key = (r["market_id"], r["run_id"])
        if key not in best or _edge_key(r) > _edge_key(best[key]):
            best[key] = r
    return list(best.values())


def _edge_key(r: dict[str, Any]) -> tuple[float, float, str, str]:
    edge = r["edge"] if r["edge"] is not None else float("-inf")
    return (edge, r["p_win"], r["bucket"], r.get("side") or "YES")
```

In `compute_pnl`, replace the loop header and drop the per-row recommended check:

```python
    for r in _best_per_market_run(_settled_rows(conn)):
        n += 1
        ask = r["ask"]
        ...
```

In `compute_calibration`, replace the `recommended`/`hit_rate` block:

```python
    bets = _best_per_market_run(rows)
    hit_rate = sum(1 for r in bets if _bet_won(r)) / len(bets) if bets else None
```

- [ ] **Step 5: Run the full tracking suite, verify PASS**

Run: `uv run pytest tests/test_tracking.py -v`
Expected: all PASS.

- [ ] **Step 6: Run the whole suite + lint + types**

Run: `uv run pytest && uv run ruff check . && uv run mypy src`
Expected: all green (golden e2e unaffected; it does not assert tracking metrics).

- [ ] **Step 7: Commit**

```bash
git add src/rainmaker/tracking.py tests/test_tracking.py
git commit -m "fix: count one bet per (market, run) in tracking"
```

---

## Residual (documented, out of scope)

The same market re-recommended across several daily runs settles against one
actual, so those across-run bets stay correlated. Option 1 collapses within
(market, run), not across runs. Left as-is; it is smaller and predates NO betting.

## Self-review

- Spec coverage: P&L collapse (compute_pnl), hit-rate collapse
  (compute_calibration), Brier/n_scored preserved, docstring, snapshot via
  write_snapshot (covered by the updated persists test). All mapped.
- No placeholders: every step has exact code/commands.
- Type consistency: `_best_per_market_run`/`_edge_key` signatures match call
  sites; `_settled_rows` keys (`market_id`, `run_id`, `edge`) match helper use.
