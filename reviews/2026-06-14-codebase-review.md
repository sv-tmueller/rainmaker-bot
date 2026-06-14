# Codebase review - 2026-06-14

**Verdict: changes-requested**

Two real defects in shipped/production code block: `settle.py` aborts the entire
settlement loop when one station's NCEI fetch hits a transient HTTP error (the
scheduled 3-hour run settles nothing if a single station is flaky), and the
dashboard silently drops every settled precipitation bet because its TypeScript
re-implementation of the settlement math does not handle inch-bracket labels.
Both are confirmed against the code.

The rest is a healthy backlog: a missing CI workflow, three stale spots in the
operations docs/README, a handful of unguarded error paths that turn a malformed
API response into an unhandled traceback instead of a skipped item, and a long
tail of test-coverage gaps on error branches and boundary conditions. None of
those block on their own.

Two worker "must-fix" claims were false positives and are dismissed below: the
`aggregate.py` naive-datetime crash (Pydantic's `AwareDatetime` rejects naive
values at construction, so the crash path is unreachable) and the
"non-existent" GitHub Action versions (the live registry confirms
`checkout@v6`, `upload-artifact@v7`, and `setup-uv@v8.2.0` all exist).

---

## Must-fix

### Settlement

**`src/rainmaker/settle.py:45-48`** (bugs) - One station's HTTP error aborts all
settlement. `fetch_actuals` and `fetch_monthly_precip` both call
`resp.raise_for_status()` (confirmed at `backfill.py:59,90`), and `run_settlement`
has no try/except inside its loop. A 503/timeout from NCEI for one market raises
straight out of the loop, so every market after the failing one is never
attempted and the caller gets an exception instead of a partial `(settled,
waiting)` result. This is the scheduled GitHub Actions path; one flaky station
must not block all others.
*Fix:* wrap the two fetch calls in `try/except httpx.HTTPError` (or the broader
`httpx.TransportError`); on error, log to stderr, increment `waiting`, and
`continue`.

### Dashboard - Next.js frontend

**`dashboard/lib/data.ts:53-69`** (bugs) - `wonBucket` silently drops or
miscounts every settled precipitation bet. Verified empirically: the six real
precip label formats (`<2"`, `>6"`, `0.5-1"`, `2.5-3"`, Kalshi `greater than 4"`,
Kalshi `2" to 3"`) all return `null` from `wonBucket`, so those rows are filtered
out of the Recent Settled list regardless of outcome. The function is a
TypeScript re-implementation of Python's `settles`/`precip_settles` and has
drifted: it uses `roundHalfEven` (integer rounding) and a closed `[lo, hi]`
comparison, while `precip_settles` uses 2-decimal rounding and a half-open
`[lo, hi)` interval, so any integer-boundary precip range that did parse would be
graded on the wrong side. The `outcomes.won` column already exists in the schema
(`store/db.py:77`) but is never written by `record_outcome` and never read.
*Fix (resolves three findings at once):* stop recomputing settlement in
TypeScript. Persist `won` (and ideally `pnl`) into the `outcomes` table at Python
settlement time, expose it from `store/query.py`, and have the dashboard read the
persisted value. This removes the cross-language duplication and makes the
dashboard correct by construction. (Dropping the column instead is the cheaper
alternative only if the dashboard is changed to read a venue-neutral Python
grading helper.)

---

## Should-fix

### Repo / CI

**`.github/workflows/` (n/a)** (architecture) - No CI workflow runs `pytest`,
`ruff check`, or `mypy` on push or pull request. The only two workflows are the
scheduled production run and a manual backfill. CLAUDE.md says checks "must pass
before requesting review," but nothing enforces it; a broken PR can merge and the
first signal is a failing production run.
*Fix:* add `.github/workflows/ci.yml` triggered on `push` and `pull_request` to
`main` running `uv sync`, `uv run ruff check .`, `uv run mypy src`,
`uv run pytest`.

### Docs, config, and repo meta

**`docs/operations/README.md:9`** (bugs) - Says the workflow runs "at 13:00 UTC
daily"; the actual cron is `0 */3 * * *` (every 3 hours). An operator would
misdiagnose run timing.
*Fix:* describe the every-3-hours schedule.

**`docs/operations/README.md:9-11`** (bugs) - The cloud-run sequence omits
`rainmaker prune`, which the workflow runs between `settle` and `snapshot`. It is
also missing from the Commands section.
*Fix:* add `prune` to the sequence and the Commands list.

**`docs/operations/README.md:68`** (bugs) - Says `CONFIDENCE_FLOOR` is "currently
0.90"; the code has `CONFIDENCE_FLOOR = 0.80` (`config.py:328`), matching the
architecture decision record. Code wins.
*Fix:* change to 0.80. (The README top-level Status section repeats the same
stale framing; correct it there too.)

**`README.md:11-12,21,52`** (bugs) - Status claims MVP 1.0 is live only for TMAX,
with TMIN and precipitation still outstanding, and does not mention Kalshi. Per
CLAUDE.md the TMIN slice, the monthly-precipitation slice, and the Kalshi venue
all shipped; only the daily-binary precipitation form remains. Materially stale
for new contributors.
*Fix:* update the Status section and the roadmap bullet.

### Forecast sources - base and aggregation

**`src/rainmaker/forecasts/aggregate.py:34` + `src/rainmaker/ranking/edge.py:58,143`**
(bugs) - A source that responds but has every sample filtered as stale is
recorded `ok=True, n_samples=0`. `edge.py` counts `n_sources = sum(1 for c in
coverage if c.ok)`, so that empty source still increments `n_sources` and can let
the min-sources gate pass with no actual forecast data behind it - it violates the
"never recommend on stale data" rule.
*Fix:* either set `ok=len(fresh) > 0` (with an "all samples stale" error string)
in `aggregate`, or count `c.ok and c.n_samples > 0` in both edge gates.

### Forecast sources - NWS and Open-Meteo

**`src/rainmaker/forecasts/nws.py:21-27`** (bugs) - `parse()` has no guard for
unsupported variables. PRCP (or any future variable) falls through the TMIN
branch and returns a night-temperature period mislabeled with the requested
variable; `openmeteo.py` raises `NotImplementedError` for the same case via
`_daily_field`, so NWS is the inconsistent one.
*Fix:* raise `NotImplementedError` at the top of `parse()` when
`target.variable not in ("TMAX", "TMIN")`.

### Forecast sources - precipitation

**`src/rainmaker/forecasts/precip.py:126`** (bugs) - `parse_nws_qpf` does
`float(entry["value"])` with no None guard. The NWS gridpoint contract allows
null QPF entries; `float(None)` raises `TypeError`, which escapes the caller's
`except (httpx.HTTPError, ValueError, KeyError)` and crashes the whole
`build_precip_forecast_set` instead of degrading to Open-Meteo-only.
*Fix:* `if entry["value"] is None: continue` before the conversion (same pattern
as the Open-Meteo None guard).

### Polymarket client and market parsing

**`src/rainmaker/polymarket/markets.py:39`** (bugs) - `parse_bucket_label` guards
inverted ranges with `lo > hi` (strict), so a zero-width label like `70-70°F`
passes and yields a `lo==hi` bucket that integrates to zero probability mass. The
precip parser at `precip_markets.py:57` correctly uses `lo >= hi`.
*Fix:* change to `if lo >= hi:` to match and reject the degenerate case loudly.

**`src/rainmaker/polymarket/client.py:84-85,110`** (bugs) - `discover_markets`
and `discover_precip_markets` catch only `ValueError`, but `parse_market` /
`parse_precip_event` index event keys (`endDate`, `slug`, `id`, `markets`)
directly. A title-matched event missing a later required key raises `KeyError`,
which escapes and aborts the whole discovery run rather than skipping one bad
market.
*Fix:* broaden both `except` clauses to `(ValueError, KeyError)`.

### Kalshi client and market parsing

**`src/rainmaker/kalshi/markets.py:65-70` + `precip_markets.py:31-35`** (bugs) -
`floor`/`cap` come from `.get()` and can be `None`; `int(None)` raises
`TypeError`, but `client.py` catches only `ValueError`, so a malformed strike
(missing `floor_strike`/`cap_strike`) propagates uncaught instead of being logged
and skipped.
*Fix:* raise `ValueError` explicitly when the needed strike is `None` before the
`int()` conversion, in both the temp and precip parsers.

**`src/rainmaker/kalshi/client.py:54-55`** (bugs) - `m["event_ticker"]` in
`_open_events` is unguarded. A missing key raises `KeyError`, caught by neither
`except httpx.HTTPError` (around the fetch) nor `except ValueError` (in the
discover functions), so one malformed market entry aborts grouping for the whole
series.
*Fix:* `m.get("event_ticker")` with a skip, or wrap the grouping loop in
`try/except (KeyError, TypeError)`.

### Data store - schema and backend abstraction

**`src/rainmaker/store/migrate.py:29-38`** (bugs) - Migrations are not crash-safe
on the SQLite backend. `connect()` opens `sqlite3.connect(dsn)` with the default
`isolation_level`, under which Python's sqlite3 implicitly commits any pending
transaction before executing DDL (`ALTER TABLE`). So a crash between an
`ALTER TABLE` and its `schema_migrations` INSERT leaves the column added but
unrecorded; the next run re-attempts the same ALTER and fails with "duplicate
column name," leaving the store stuck without manual fixup. Note the proposed
SAVEPOINT fix will not help while the implicit-commit-before-DDL behavior is in
effect; the connection mode has to change first.
*Fix:* the robust option is to treat a "duplicate column name" OperationalError
on the ALTER as already-applied and still record the migration. (A
SAVEPOINT-wrapped fix additionally requires opening the connection in a mode that
does not auto-commit before DDL, e.g. `autocommit`-style control, which is a wider
change - confirm against `db.py`'s `connect()` before relying on it.)

### Data store - persistence and queries

**`src/rainmaker/store/record.py:38,52-56`** (bugs) - `_run_coverage` takes only
`evaluated` and ignores `precip_evaluated`, so `n_markets` undercounts and
`ok_sources` can be empty on mixed or precip-only runs. The dashboard reads both
from `runs.coverage`, so the health line is wrong on every run that processes
precip markets (which is every real run).
*Fix:* pass `precip_evaluated` into `_run_coverage`, count precip markets in
`n_markets`, and union their `report.coverage` sources into `ok_sources`.

**`src/rainmaker/store/prune.py:12-14`** (bugs) - `_runs_to_prune` anchors on
`predictions` (FROM predictions JOIN runs JOIN outcomes). A settled market with
`prices` rows but zero `predictions` rows (possible when `evaluate_market`
returns no outcomes for want of forecast samples) is invisible to the query, so
its redundant intraday `prices` rows are never reclaimed - defeating prune for
those rows.
*Fix:* anchor on a table written unconditionally for every market (union the
`prices` and `predictions` sub-selects, or join `runs -> prices -> outcomes`).

### Settlement

**`src/rainmaker/settle.py:45-46`** (bugs) - The PRCP-vs-other branch silently
sends any non-PRCP variable to `fetch_actuals`. An unknown variable string is
passed straight to NCEI's `dataTypes` param, NCEI returns empty, `waiting` is
incremented, and the market stalls forever with no diagnostic.
*Fix:* add a guard - if `m["variable"]` is not in `{"TMAX","TMIN","PRCP"}`, warn
to stderr and `continue`, matching the unknown-city guard.

### Tracking and P&L

**`src/rainmaker/tracking.py:218-258`** (bugs) - `write_snapshot` is not atomic.
The `tracking_snapshot` INSERT is still uncommitted when the first `save_accuracy`
call runs, and `save_accuracy` commits internally per row, so it commits the
not-yet-final snapshot together with the first accuracy row. If `save_accuracy`
raises on row N>1, the snapshot plus N-1 accuracy rows are committed and the rest
are not, with no way to detect the partial write.
*Fix:* move the `tracking_snapshot` INSERT to after the `save_accuracy` loop so
all accuracy rows commit first and the snapshot follows in the final commit.

### Report rendering

**`src/rainmaker/report/render.py:15-18`** (bugs) - `_coverage_str` drops
`SourceCoverage.error`. A failed source renders as `FAILED(n)` with no reason,
though the error string ("timeout", "down") is on the model and is the operator's
only clue to why a source dropped.
*Fix:* include the error when `ok` is False, e.g.
`FAILED({c.error})`.

**`src/rainmaker/report/render.py:70-75`** (bugs) - The mu/sigma forecast line
renders only when both are non-None. The type allows them to diverge
independently (`float | None` each); if exactly one is set the line is silently
skipped. The `evaluate_market` path always sets both or neither, so this is a
latent invariant that the type does not enforce.
*Fix:* tighten the model to a single optional `(mu, sigma)` pair, or render a
"partial data" note when only one is present.

### Architecture (venue coupling)

**`src/rainmaker/probability/outcomes.py:1-10` + `precip_outcomes.py` +
`tracking.py:17-18`** (architecture) - The probability and tracking layers import
domain geometry (`Bucket`, `BucketKind`, `PrecipBracket`, `SETTLEMENT_DECIMALS`,
`parse_bucket_label`, `parse_precip_bracket_label`) from the Polymarket venue
package, and the Kalshi package reuses Polymarket's `Bucket`/`Market`/
`PrecipBracket` types - making Kalshi a structural subtype of Polymarket rather
than a sibling venue. `bucket_probability` takes a full `Bucket` (token ids,
prices) when it needs only geometry.
*Fix:* extract a venue-neutral domain types module
(`probability/types.py` or `src/rainmaker/domain.py`) holding `BucketKind`,
`Bucket`, `PrecipBracket`, `Market`, `PrecipMonthlyMarket`,
`SETTLEMENT_DECIMALS`, and the two label parsers; have both venue packages, the
probability engine, and tracking import from there. Defensible to defer, but it is
the structural fix that the precip-drift must-fix also points at.

### Tests (gaps that would have caught a real defect)

**`tests/test_settle.py` (n/a)** (tests) - No test for an NCEI HTTP error during
the settlement loop, nor for a TMIN-variable settlement. Add a 500-from-NCEI test
asserting `run_settlement` returns `(0, 1)` rather than raising (pins the must-fix
fix above), and a TMIN settlement test.

**`tests/test_precip_forecast.py` (n/a)** (tests) - `parse_nws_qpf` has no test
with a null entry value (the gap that hid the missing guard above), and the
both-sources-failed climatology-only fallback (the riskiest degradation) is
untested. Add both.

**`tests/test_prune.py` (n/a)** (tests) - No test for a settled market with
`prices` but zero `predictions`; pins the prune-anchor fix above.

**`src/rainmaker/store/record.py:208-233`** (tests) -
`test_record_run_persists_precip_market` asserts nothing about `run.coverage`; a
precip-only run silently records `n_markets=0, ok_sources=[]`. Add an assertion
that `run.coverage` reports `n_markets >= 1` and non-empty `ok_sources` when
`precip_evaluated` is provided.

**`tests/test_store_db.py:13-21` + `tests/test_migrate.py:64`** (tests) -
`EXPECTED_TABLES` omits `forecast_accuracy` and `tracking_snapshot` and the check
uses a subset (`<=`), so a regression dropping either table passes. And
`test_migration_count` hardcodes `assert n == 5`. Add the two table names; replace
the literal with `assert n == len(_MIGRATIONS)`.

**`tests/test_migrate.py:6-55`** (tests) - The migration tests call
`init_schema()` (which already runs `apply_migrations`), so the columns exist
before the test does anything - the upgrade-an-existing-schema path is never
exercised. Build the old schema by hand (CREATE TABLE without the new column),
then call `apply_migrations`, then assert the column was added.

**`tests/test_cli.py:38-61`** (tests) - `_forecast_set()` claims two ok sources in
coverage but only supplies `nws` samples, so every CLI integration test satisfies
the two-source gate on fabricated coverage. Either add open-meteo samples or drop
the open-meteo coverage entry.

**`tests/test_calibration.py:75-78`** (tests) - `test_apply_none_falls_back`
checks `calibrated=False` and `mu` but not the widened sigma (expected 2.5). A
change to `UNCALIBRATED_WIDEN` or the fallback formula passes silently. Assert
`out.sigma == pytest.approx(2.5)`.

**`src/rainmaker/probability/calibration.py:54,60`** (tests) - No test pins the
under-dispersive branch (`spread_scale < 1`, no clamp) or the `1e-6` floor on a
perfect-fit cell (without it, `spread_scale=0` fails `Field(gt=0)`). Add both.

**`tests/test_edge.py:260-283`** (tests) - `test_evaluate_precip_market_ranks_brackets`
exercises the NO side (all six fixture brackets carry `bestBid`) but asserts only
on YES. Assert NO outcomes exist, each NO `p_win == 1 - YES p_win`, and
`excluded_no_ask == []`.

**`tests/test_outcomes.py:29-49`** (tests) - No test pins that a full temperature
partition yields exactly one settling bucket per integer (the precip side has
this). Add a parameterized sweep over a realistic range.

**`tests/test_nws.py` + `tests/test_openmeteo.py` (n/a)** (tests) - Untested error
paths: the second NWS HTTP call (gridpoints forecast 4xx/5xx), the
`temperatureUnit != "F"` ValueError guard, and HTTP errors from
`fetch_raw_multimodel` / `fetch_raw_ensemble`. Add a test for each.

**`tests/test_kalshi_markets.py` + `tests/test_kalshi_client.py` (n/a)** (tests) -
The TMIN "Climatological Report" guard branch, the `_yes_price` mid/zero fallback
chain, the cursor-based pagination continuation, and the precip-discovery outage
path are all untested. Add a focused test for each.

**`src/rainmaker/backtest.py:186-219`** (tests) - `combine` (n-weighted metric
merge and reliability-bin merge) has no direct test; the CLI exercises it only
through a single-city result, so the weighting is never checked against a known
answer. Add a unit test with two unequal-`n` `BacktestResult` values.

**`src/rainmaker/backtest.py:176-177`** (bugs) - `backtest_synthetic` threads its
`variable` arg into `fetch_actuals` but not `fetch_historical_forecasts` (which
defaults to TMAX), so `backtest_synthetic(station, "TMIN", ...)` would score TMAX
forecasts against TMIN actuals. Latent (the CLI hardcodes TMAX) but the signature
promises behavior it does not deliver.
*Fix:* pass `variable` to the forecasts call and add a TMIN test.

**`src/rainmaker/backfill.py:115,167`** (bugs) - `resp.json()["daily"]` /
`["hourly"]` raise `KeyError` on a 200 response with an error body (Open-Meteo
returns `{"error": true, ...}` for bad param combinations); `raise_for_status`
does not help. The `KeyError` escapes the CLI's `(httpx.HTTPError, ValueError)`
catch and crashes the whole backfill run instead of skipping the station.
*Fix:* add `KeyError` to the CLI catch, or use `.get("daily")` with an explicit
ValueError.

**`tests/test_tracking.py` (n/a)** (tests) - `compute_calibration(conn,
venue=...)` is untested (only `compute_pnl` venue filtering is), though both share
`_filter_venue`. And the documented premature-commit behavior in `write_snapshot`
is not pinned (no test asserts the snapshot row is present and correct after
`save_accuracy` commits it early). Add both.

**`tests/test_pnl_backtest.py:287`** (bugs) - `assert request.url.params["market"]`
raises `KeyError` (not `AssertionError`) if the param is absent; httpx-mock wraps
that as a confusing HTTP error. Replace with `assert "market" in
request.url.params`.

**`tests/test_render.py:57-62`** (tests) - The markdown test uses a report with
`excluded_no_ask=["59 deg F or below"]` but never asserts the excluded note
renders. Add an assertion for "Excluded (no ask)" and the bucket label.

**`dashboard/lib/data.ts:53-69`** (tests) - `roundHalfEven` and `wonBucket`
re-implement Python money-grading logic with no tests; the precip drift above is
exactly the divergence a test would catch. If the recommended persist-`won` fix is
adopted this code goes away; otherwise add Vitest/Jest tests mirroring the Python
inputs.

### Dashboard - Next.js frontend

**`dashboard/lib/data.ts:80`** (bugs) - `tracking_snapshot` is fetched ascending
with no `.limit()`. Supabase's PostgREST silently caps unbounded queries at 1000
rows, so after ~2.7 years the KPI strip (`snapshots[length - 1]`) returns the
1000th-oldest row, showing stale P&L/ROI.
*Fix:* order descending with `.limit(N)`, use `snapshots[0]` for the KPI strip,
reverse for the chart if it needs ascending.

**`dashboard/lib/data.ts:78-83`** (bugs) - Supabase query `.error` is never
checked; a transient failure yields `data=null`, which `?? []` renders as the
empty "no runs yet" state, indistinguishable from a genuinely empty database.
*Fix:* check `.error` on at least `runsQ` and surface an error state.

### Backfill

**`src/rainmaker/backfill.py` (n/a)** (architecture) - `fetch_historical_samples`
hardcodes `"TMAX"` in the `_daily_field` call and the `ForecastSample`
constructor with no `variable` parameter, so the TMAX-only constraint is invisible
at the call site.
*Fix:* add `variable: str = "TMAX"` and thread it through; the default preserves
behavior. (Document the TMAX-only intent if the parameter is not added.)

---

## Nits

**`src/rainmaker/cli.py:263-265,289-291`** (bugs) - The `isinstance(exc,
ValidationError)` re-raise works only because `pydantic.ValidationError` is a
subclass of `ValueError` (confirmed for this repo's Pydantic 2.13). It is correct
today, but the dependency is implicit and Pydantic has changed this across major
versions; the bare `raise` also loses the chained context. Catch `ValidationError`
in its own `except` before `(httpx.HTTPError, ValueError)`, or add it explicitly
to the tuple. (Merges the contradictory "works by subclass" and "permanently dead
code" worker findings: the branch is reachable and not dead.)

**`src/rainmaker/cli.py:332`** (bugs) - `STATIONS[name]` in `_backtest` raises an
unhandled `KeyError` on an unknown `--city`; the inner `except` catches only
`httpx.HTTPError`, so the user sees a raw traceback. Validate the city or catch
`KeyError` with a clean `SystemExit(1)`.

**`src/rainmaker/cli.py:361-362`** (bugs) - `_parse_leads` lets `int()` raise
`ValueError` on `--leads abc` and propagates a traceback. Use a custom argparse
`type=` converter or `parser.error(...)`.

**`src/rainmaker/cli.py:489`** (bugs) - `backfill --variable` has no `choices`
constraint, so an unsupported value runs the whole pipeline before failing deep.
Add `choices=["TMAX","TMIN"]` or an early guard.

**`src/rainmaker/config.py:18,189`** (style) - The `Station.wunderground_url`
field carries NWS CLI URLs for every `KALSHI_STATIONS` row (the name is a lie
there; the DB column `resolution_source` is already neutral), and the explanatory
comment sits 28 lines above the dict it documents. Rename the attribute to
`resolution_url` and move the comment above `KALSHI_STATIONS`.

**`src/rainmaker/probability/calibration.py:4,49-54,87,43-62`** (style/tests) -
Docstring says spread is "reliably overconfident" but the code applies
under-dispersive (<1) values too; hedge the wording. No nan guard on
`CalibrationPair.actual` (a NaN surfaces as a confusing `spread_scale` validation
error); the exact-`min_samples` boundary and heterogeneous-sigma cases are
untested. Minor.

**`src/rainmaker/probability/precip_outcomes.py:16-17`** (tests) - The degenerate
dry test does not check the high-tail bracket is 0.0 or that the partition sums to
1.0. Extend it.

**`src/rainmaker/probability/outcomes.py:41`** (bugs) - `settles` uses `round()`
(banker's rounding), documented as intentional in its test. The half-to-even vs
half-up question is not confirmed against Polymarket/Kalshi published resolution
rules; a wrong choice mis-grades a market at exactly X.5F. Confirm the venue rule
and document it in `docs/architecture/`. (Real-money-relevant but currently a
documentation/verification gap, not a confirmed defect.)

**`src/rainmaker/ranking/edge.py:82-118,147-178`** (scope) - The outcome-building
loop is duplicated nearly verbatim across `evaluate_market` and
`evaluate_precip_market`; only the p_win function differs. Extract a private
`_rank_outcomes(buckets, p_win_fn, ...)` helper.

**`src/rainmaker/ranking/edge.py:87 vs 104`** (bugs) - The YES guard is
`best_ask > 0` (no ceiling) while the NO guard is `0 < no_ask < 1`. A YES ask of
exactly 1.0 slips through (harmless, edge<=0) but the asymmetry is inconsistent.
Use the same half-open interval on both.

**`src/rainmaker/forecasts/base.py:37`** (style) - `ForecastSource.fetch` has no
docstring noting it may raise; callers using the Protocol directly (not via
`aggregate`) get no exception-handling hint. Add one line.

**`src/rainmaker/forecasts/nws.py:48`** (security) - `fetch_raw` follows the
forecast URL from the `/points` response with no domain check. Theoretical
(TLS-protected, no user input), but a `startswith(NWS_BASE)` guard is cheap.

**`src/rainmaker/forecasts/precip.py:53`** (bugs) - `monthly_total_moments` drops
single-member days from the variance sum (`if len(day) >= 2`); when one source is
down, that day's variance is treated as zero. Document the per-day variance drop
in the docstring.

**`src/rainmaker/forecasts/precip.py` (n/a)** (architecture) - Both `NwsSource`
and `fetch_nws_qpf` call the same `/points/{lat},{lon}` endpoint, so a city with
both temp and precip markets makes two identical `/points/` calls per run. Share
or memoize the response.

**`src/rainmaker/polymarket/prices.py:17-18,34`** (style) - `_COARSE_FIDELITY =
720` minutes is 12 hours, but the comment and docstring call it "daily." Relabel
to "12-hour resolution."

**`src/rainmaker/polymarket/client.py:55-59`** (tests) - The
`fetch_closed_weather_events` test asserts `closed=true` but not the `order` /
`ascending` params; if dropped, the backtest gets unspecified ordering silently.
Assert them.

**`src/rainmaker/polymarket/markets.py:98-109`** (architecture) - `parse_variable`
(no production caller, test-only) and `parse_city` duplicate the `_TITLE_RE`
match already inlined in `parse_market`. Inline `parse_city`, drop or mark
`parse_variable` test-only.

**`tests/test_polymarket_markets.py` + `tests/test_kalshi_precip_markets.py`
(n/a)** (tests) - Missing the temperature inverted/zero-width-range ValueError
test (the precip path has it), the Kalshi precip bad-ticker `month token`
ValueError test, and the precip `less`-strike (`kind='below'`) branch. Add each
to mirror the existing temperature-path coverage.

**`src/rainmaker/store/db.py:164-169`** (scope) - `_translate` runs on every
Postgres execute, including zero-param DDL/SELECTs, scanning for `?` with nothing
to translate. Guard with `if params:`.

**`src/rainmaker/store/db.py:77`** (scope) - The `outcomes.won` column is dead
schema (never written, never read). Resolved as part of the dashboard must-fix:
wire it up (write `won` at settlement, dashboard reads it) or drop it with a
migration.

**`src/rainmaker/settle.py:21-28,46`** (style/bugs) - `_legacy_ghcnd` types
`market` as `dict[str, str]` though `unsettled_markets` returns `dict[str, Any]`
with float columns; and the PRCP-month assumption (settlement date is the month's
last day) is implicit. Fix the hint to `dict[str, Any]` and add an assertion or
comment on the month invariant.

**`src/rainmaker/tracking.py:3,161-169`** (style/tests) - Docstring "One one-unit
bet" doubles "one." And `compute_live_accuracy`'s `SELECT DISTINCT` relies on
every bucket row of one (run, market) carrying an identical `dist_params`; no test
pins that two divergent `dist_params` rows would inflate the sample count. Fix the
typo; add the pinning test.

**`src/rainmaker/backtest.py:244,176-183`** (style/tests) - `render_report`'s
header has a trailing-space empty cell for the real-market section, and the TMIN
path through `backtest_synthetic` is untested. Cosmetic / coverage.

**`src/rainmaker/backfill.py:178,197`** (bugs/style) - `zip(times, values,
strict=True)` raises `ValueError` and aborts all leads/models if one model's list
is short; consider non-strict or per-model skip. And `fetch_historical_samples`
hardcodes TMAX without documenting it. Both minor.

**`src/rainmaker/backfill.py` (n/a)** (architecture) - `backfill.py` imports the
name-mangled private `_daily_field` from `openmeteo.py`. Rename to public
`daily_field` or expose a small public helper.

**`tests/test_backfill.py` (n/a)** (tests) - No test for `fetch_actuals` with a
`None` (vs `""`) value, nor for `run_backfill_accuracy` returning an empty dict
(the silent exit-1 path). Add both.

**`src/rainmaker/report/render.py:10-12,37-41`** (style) - `Report` is not frozen
though sibling models are; `_bet_label`'s fallback is best-effort but its comment
overstates the city-drop. Cosmetic.

**`tests/test_render.py:44-55`** (tests) - The terminal test asserts REC appears
somewhere but not that the non-recommended row lacks it; a "mark everything REC"
bug would pass. Assert the non-recommended row has no REC.

**`tests/test_calibration.py:72` + `tests/test_outcomes.py:74-79` +
`tests/test_precip_outcomes.py:36` + `tests/test_edge.py:281` /
`tests/test_golden_e2e.py`** (tests/style) - Directional-only sigma assertion
(use `approx(2.5)`); half-integer rounding untested for `below`/`above`; a
`# noqa: E731` lambda that could be a `def`; and manual `abs(sum-1.0)<1e-6`
partition checks that should use `pytest.approx` to match the unit-level style.

**`tests/test_openmeteo.py:122,138` + `tests/test_aggregate.py:57-65` +
`tests/test_precip_forecast.py:35-45` + `tests/test_nws.py:66-69,85-88`** (tests/
style) - Hardcoded `30 * 3` ensemble count instead of deriving from
`OPENMETEO_ENSEMBLE_MODELS`; the freshness `<=` boundary, the single-member
variance branch, and an exact precip-variance value are untested; two NWS fetch
tests create a client without a `with` block (leaks on raise). Minor.

**`tests/test_backtest_io.py:67-78` + `tests/test_pnl_backtest.py:137-146`**
(tests) - `test_backtest_real_...` asserts only `n == 2`, no scoring metric; the
spread-haircut test never reaches the `min(..., 1.0)` cap. Add a numeric bound and
a near-1.0-mid cap case.

**`dashboard/components/PnlChart.tsx:5` + `dashboard/components/BetsTable.tsx:32,
116` + `dashboard/lib/data.ts:198,53-57` + `dashboard/lib/format.ts:10-21` +
`dashboard/lib/supabase.ts:1-11`** (bugs/style/security) - `PnlChart` returns null
at exactly one snapshot while the parent only guards `=== 0` (panel shows a bare
header); the Kalshi-URL split and unconditional `text-pos` Edge styling are
undocumented/sign-blind; a missing price falls back to `0` (reads as a real
`0.00`); `withCelsius` does not handle Kalshi `"X to Y"` range labels; and
`lib/supabase.ts` uses the service-role key with no `import 'server-only'` guard
(currently safe - all Server Components - but no build-time enforcement). Each is
a small, isolated cleanup.

**`docs/superpowers/specs/2026-05-29-mvp1-advisory-design.md:106,108`** (bugs) -
The frozen spec lists `pandas` as a dependency (never added) and references
`docs/architecture/decisions.md` (never created; the real files are
`recommendation-gate.md` and `polymarket-weather-markets.md`). Low priority since
the spec is frozen.

---

## Dismissed (false positives / out of scope)

- **`src/rainmaker/forecasts/aggregate.py:12` (worker must-fix)** - Claimed a
  naive `issued_at` crashes `_is_fresh` with a `TypeError` that escapes the
  per-source `except`. Verified false: `ForecastSample.issued_at` is typed
  `AwareDatetime`, and Pydantic v2 rejects a naive datetime *at construction*
  (`[type=timezone_aware]`, confirmed by running it) - you cannot build such a
  sample. Even a buggy parser producing a naive value fails construction and is
  caught by `aggregate`'s `except Exception` at line 27. The "swallowed and
  crashes the whole run" premise does not hold. (The separate stale-source
  `n_sources` inflation in the same file is real and kept as should-fix.)

- **`.github/workflows/daily-run.yml:14,59` + `backfill.yml:9` (worker should-fix)**
  - Claimed `actions/checkout@v6`, `actions/upload-artifact@v7`, and
  `setup-uv@v8.2.0` are "non-existent major versions." Verified false against the
  live release registry: `checkout` latest is v6.0.3, `upload-artifact` latest is
  v7.0.1, `setup-uv` latest is v8.2.0. All three pins exist and are current; the
  claim reflects a stale knowledge cutoff, not the repo.

- **`src/rainmaker/cli.py:264` "permanently dead code" (worker should-fix)** -
  Claimed the `isinstance(exc, ValidationError)` branch is unreachable because
  `ValidationError` is a subclass of neither caught type. Verified false:
  `issubclass(ValidationError, ValueError)` is `True` in this repo's Pydantic
  2.13, so the branch is reachable and the re-raise fires. The contradictory CLI
  finding (works by subclass) is correct; both are merged into a single nit about
  the implicit dependency, downgraded because no schema error is ever silently
  swallowed.

- **`src/rainmaker/cli.py:265` bare-raise context loss (worker nit)** - Folded
  into the merged ValidationError nit rather than listed separately; same code,
  same fix.

- **Three architecture findings recommending opposite fixes for `outcomes.won`
  (remove vs wire up)** - Not dismissed but resolved: consolidated into the
  dashboard must-fix with one coherent path (persist `won`, dashboard reads it),
  so they are not double-counted as separate blocking items.

---

## Coverage

All 24 areas were reviewed; coverage is complete.

**Areas reviewed:** CLI and entry points; Configuration and city registry;
Probability engine - distributions and calibration; Probability engine - outcome
integration; Edge ranking; Forecast sources - base and aggregation; Forecast
sources - NWS and Open-Meteo; Forecast sources - precipitation; Polymarket client
and market parsing; Kalshi client and market parsing; Data store - schema and
backend abstraction; Data store - persistence and queries; Settlement; Tracking
and P&L; Backfill; Backtesting; Report rendering; Tests - core math and golden
e2e; Tests - forecast sources; Tests - market clients; Tests - store, CLI, and
operations; Tests - tracking and backtesting; Dashboard - Next.js frontend; Docs,
config, and repo meta.

**Paths not covered:** none.

**Workers that failed:** none.
