# TDD Implementation Plan - Issue #35: Monthly US Precipitation Markets

Scope: monthly US form only (Gamma 531291 NYC, 531299 Seattle). Daily binary deferred. Build as a PARALLEL precip path reusing only variable-agnostic infra; do not modify temperature parsing, the Gaussian, or whole-degree outcome integration. Keep the golden e2e untouched.

Open decisions:
1. Fixture capture is a hard gate: capture & commit tests/fixtures/polymarket_precip_monthly_nyc.json and ..._seattle.json (raw Gamma event JSON) BEFORE writing parser assertions; the bracket-label grammar (e.g. "Less than 2 inches" vs "<2 in" vs "2.00-2.99") must be reconciled against the real text - treat label grammar as provisional until confirmed.
2. Seattle precip GHCND id unconfirmed (SeaTac USW00024233 is the WRONG temperature station). Do not hardcode an unverified id; leave explicit and fail loud if unset. Confirm via NCEI GSOM PRCP for a candidate id + a published month, cross-check vs the market's named station. NYC anchor: Central Park May 2026 = 3.06 in.
3. Distribution family gamma vs lognormal (HARD PART #1) - DEFERRED, not in this build.
4. Partial-month conditioning (HARD PART #2) - DEFERRED, not in this build.
5. Type integration: parallel but structurally-compatible types - PrecipBracket/PrecipMonthlyMarket with the SAME field names as Bucket/Market so the existing recorder duck-types over them; a Market | PrecipMonthlyMarket union (or shared Protocol) where mypy needs it. Do NOT mutate Bucket/Target/ForecastSample.
6. Variable literal: extend Variable = Literal["TMAX","TMIN","PRCP"] (low risk; temperature code still only emits TMAX/TMIN). NOTE: if branch feat/36 also extends Variable to add TMIN, that is a separate branch; here add PRCP to whatever the current main has (main currently has TMAX,TMIN per config - verify and add PRCP).
7. Report units - DEFERRED (Task 6).

Architecture for the FOUNDATION (Tasks 0-2 only):
- src/rainmaker/config.py: add PrecipStation model + PRECIP_STATIONS registry; add precip tuning constants if needed by Task 1; extend Variable to include PRCP.
- src/rainmaker/polymarket/precip_markets.py (NEW): monthly title/bracket parsing -> PrecipMonthlyMarket.
- src/rainmaker/polymarket/client.py: add discover_precip_markets(...).

Task 0 - Capture fixtures (gate). Read-only Gamma probe; commit raw event JSON for 531291 (NYC) and 531299 (Seattle) to tests/fixtures/. (Open-Meteo precipitation_sum and NCEI GSOM fixtures are for the DEFERRED tasks - capture only if trivial; not required for Tasks 1-2 except the NCEI lookup used to confirm the Seattle id.) No production code yet. Confirms bracket-label grammar, title form, endDate/period encoding, description text.

Task 1 - Precip station registry.
Test (tests/test_config.py): PRECIP_STATIONS["NYC"].ghcnd_id == "USW00094728"; the registry exposes the resolution-station name used by the parser's description guard; Seattle entry exists with its confirmed id (or a sentinel that makes the parser/settle fail loud if used before confirmation).
Code (src/rainmaker/config.py): add PrecipStation (city, station label/code used where icao is used, name, lat, lon, timezone, ghcnd_id); add PRECIP_STATIONS; extend Variable; add precip constants. Include the explicit Seattle GHCND confirmation step (open decision 2) - report the outcome.

Task 2 - Monthly precip market parsing.
Tests (tests/test_precip_markets.py, against Task 0 fixtures): title regex "Precipitation in <city> in <Month>?" -> city + (year, month); station/source guard against the description (reject if the resolution station is not named, mirroring the temperature icao guard + its mismatch test); bracket-label parsing for the three forms - open-low tail (< 2), interior range (2-3; 0.5 step for Seattle), open-high tail (> 6) - with FLOAT inch bounds; the full bracket partition tiles the line with the two open tails; the value-between-brackets rounds-UP rule and 2-decimal settlement precision are encoded; period -> resolution settlement_date (last day of month / GSOM-publish date).
Code (src/rainmaker/polymarket/precip_markets.py): parse_precip_bracket_label, PrecipBracket (same field names as Bucket, float bounds), PrecipMonthlyMarket (.target-like with station/variable/month + resolution date), parse_precip_event. Reuse the YES/NO no_ask = 1 - best_bid derivation verbatim from markets.py.
Discovery (src/rainmaker/polymarket/client.py): discover_precip_markets (title predicate + PRECIP_STATIONS membership; skip-with-warning on parse failure, same as discover_markets).

Store note (for later, do not change now): no new tables; precip will reuse markets/prices/forecasts/predictions/outcomes with variable="PRCP", float bracket bounds in outcome_spec JSON.

DEFERRED (DO NOT IMPLEMENT - listed for the plan doc only): Task 3 skewed distribution + bracket integration (gamma, scipy.stats.gamma CDF, precip_settles round-up rule); Task 4 precip forecast sourcing + monthly-total moments (observed_to_date + forecastable_remaining + climatology_tail, gamma); Task 5 settlement via NCEI GSOM PRCP + tracking precip-aware _won; Task 6 evaluate_precip_market + report unit label + cli routing + precip golden e2e.

Verification (Tasks 0-2):
  uv run pytest -q
  uv run pytest -q tests/test_golden_e2e.py     # temperature path must stay green
  uv run pytest -q tests/test_precip_markets.py tests/test_config.py
  uv run ruff check .
  uv run ruff format --check .
  uv run mypy src
