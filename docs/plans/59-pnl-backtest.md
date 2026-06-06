# TDD Implementation Plan - Issue #59 Part 2: Betting P/L Backtest

Reused pieces: src/rainmaker/backtest.py (backtest_real is the template: parse closed Gamma events, keep TMAX markets settling on/after a cutoff, group by station, fetch fetch_historical_forecasts + fetch_actuals per group, score each; NO P/L). src/rainmaker/ranking/edge.py evaluate_market(market, forecast_set, *, floor, min_sources, min_sigma, min_edge, calibration=None) -> MarketReport with RankedOutcome per bucket per side; gate p_win>=floor and n_sources>=min_sources and edge>=min_edge; YES priced off bucket.best_ask, NO off bucket.no_ask. src/rainmaker/backfill.py fetch_historical_forecasts(station,start,end,client)->dict[date,Gaussian] and fetch_actuals(ghcnd_id,start,end,client,variable)->dict[date,float]. src/rainmaker/polymarket/client.py fetch_closed_weather_events(client); markets.py parse_market(event)->Market; Bucket carries yes_token_id,no_token_id,best_ask,no_ask. src/rainmaker/probability/outcomes.py settles(kind,lo,hi,threshold,actual)->bool. src/rainmaker/tracking.py P/L semantics to mirror: one-unit stake, win +(1-ask), lose -ask, roi=total_pnl/total_staked, _best_per_market_run collapsing correlated same-market buckets to one best-edge bet. Config: CONFIDENCE_FLOOR=0.90, MIN_SOURCES=2, MIN_SIGMA_F=1.5, MIN_EDGE=0.05.

Design decisions:
- D1: discover the universe with fetch_closed_weather_events + parse_market (as backtest_real does); token ids come from clobTokenIds via parse_market. Do NOT read markets.raw. Keeps the feature entirely DB-free (no SQL, no migration).
- D2: NEW modules - src/rainmaker/polymarket/prices.py (CLOB prices-history client) and src/rainmaker/pnl_backtest.py (replay + scoring + report). Do not bloat backtest.py.
- D3: capture ev["endDate"] into a side map market.id -> settlement_dt (tz-aware ~12:00 UTC) while iterating raw events; NO change to the Market model. For lead N in 0..3: target_ts = int(settlement_dt.timestamp()) - N*86400. Snap = history point minimizing |t-target_ts|, rejected if nearest is beyond tolerance (default 12h at fidelity=60). Fetch the whole series once per YES token over [settlement_dt-(maxLead+1)d, settlement_dt+1h]. Forecast is keyed to the settlement date, identical across leads (archive ~lead 1) - only price varies by lead; document this.
- D4: best_ask = p (the YES-token mid; label P/L mid-based, mildly optimistic vs the ask); no_ask = 1 - p.
- D5: min_sources cannot be replayed faithfully (archive = one source). Build a synthetic ForecastSet: one ForecastSample per archive model (source="open-meteo", distinct model), coverage=[SourceCoverage(source="open-meteo", ok=True, n=...)] -> n_sources=1. Pass gates as params (default floor=CONFIDENCE_FLOOR, min_edge=MIN_EDGE, min_sigma=MIN_SIGMA_F) but default min_sources=1; state in the report that recommended is a superset of live's 2-source gate. Efficiency: only fetch CLOB history for candidate buckets where p_win>=floor or 1-p_win>=floor.
- D6: NEW CLI subcommand backtest-pnl (not a flag on backtest); writes pnl-backtest-<date>.md/json; wire like _backtest (own httpx.Client, print md, write files, SystemExit(1) on no data).
- D7: P/L mirrors tracking.py: collapse to one bet per (market, lead) best-edge recommended outcome; win/lose via settles vs NCEI actual; +(1-ask)/-ask; roi; win_rate; aggregate per lead and overall.

TDD plan:
Phase A - CLOB client (polymarket/prices.py).
A0 fixtures: tests/fixtures/clob_prices_history.json = {"history":[{"t":<unix>,"p":<0..1>},...]} hourly ~2026-02-28..03-03 for a candidate YES token; tests/fixtures/clob_prices_history_empty.json = {"history":[]}.
A1: test fetch_price_history parses {"history":[...]} into list[PricePoint] (frozen pydantic t:int,p:float) and GETs CLOB_PRICES_URL with params market,startTs,endTs,fidelity=60 (assert via httpx_mock.get_requests()[0].url.params). Implement module + CLOB_PRICES_URL="https://clob.polymarket.com/prices-history" + parsing.
A2: test fidelity fallback - queue empty then populated; fetch_price_history(...,fidelity=60) returns the second batch and get_requests()[1].url.params["fidelity"]=="720". Implement: if history empty and fidelity<720, retry once at 720.
A3: test snap_price(points, target_ts, *, tolerance_s) returns nearest p; None beyond tolerance; deterministic tie-break (earlier t). Pure. Implement.
A4: test 5xx -> raise_for_status raises httpx.HTTPStatusError. Implement/confirm.
Phase B - forecast-set + per-lead mapping (pure).
B1: test fetch_historical_samples(station,start,end,client)->dict[date,list[ForecastSample]] against openmeteo_hist_multimodel_klga.json (one sample per model per date, source="open-meteo", variable="TMAX"). Implement in backfill.py next to fetch_historical_forecasts; optionally extract a shared private helper both call - keep fetch_historical_forecasts byte-identical so test_backfill.py stays green.
B2: test forecast_set_from_samples(target,samples)->ForecastSet (coverage Open-Meteo ok; fit_gaussian reproduces the archive Gaussian within tolerance). Implement (thin).
B3: test market_at_lead(market, mids: dict[label->float|None])->Market rebuilds buckets with best_ask=mid, no_ask=1-mid; mids None -> best_ask=None/no_ask=None so evaluate_market excludes them. Pure. Implement.
Phase C - replay + scoring (pnl_backtest.py).
C1: test replay_market(market, forecast_set, actual, histories, settlement_dt, *, leads, floor, min_sources, min_sigma, min_edge)->list[Bet] with in-memory histories (dict token->list[PricePoint], no HTTP). Controlled numbers so one cheap candidate bucket clears floor=0.90+min_edge and the actual makes it win at some leads, lose at others. Assert per-lead Bet(lead,bucket_label,side,p_win,ask,edge,won) collapsed to best-edge per (market,lead). Implement calling evaluate_market on market_at_lead(...), filter recommended, collapse like _best_per_market_run, settle via settles.
C2: test score(bets)->{LeadPnl per lead, overall}: win +(1-ask), lose -ask, roi, win_rate, mean_edge, n_bets. Pure; hand-built bet list; cross-check vs tracking.compute_pnl. Implement LeadPnl/PnlBacktestResult (frozen pydantic) + aggregation.
C3: test backtest_pnl(events, client, *, on_or_after, leads=(0,1,2,3), gates...)->PnlBacktestResult|None end-to-end on fixtures: events passed in (like backtest_real); mock HISTORICAL_FORECAST_URL, NCEI_URL, CLOB_PRICES_URL (CLOB via add_callback keyed on request.url.params["market"]). Use existing polymarket_closed_weather_events.json (KLGA 03-02/03-03) + openmeteo_hist_multimodel_klga.json + ncei_actuals_klga.json + a CLOB fixture spanning [settlement-3d,settlement]. Assert n_markets, Feb market date-filtered + London dropped, per-lead bet counts, total-P/L sign; add a None-when-all-filtered test (on_or_after far future). Implement mirroring backtest_real parse/filter/group-by-station, adding per-market: forecast set, prune to candidate buckets, fetch one CLOB series per candidate YES token over the lead window, build per-lead mids via snap_price, replay_market, accumulate, score.
Phase D - report.
D1: test render_pnl_report(result)->(str,dict): markdown has per-lead table (lead, n bets, W-L, win rate, total P/L, ROI, mean edge), the "mid-based (optimistic vs the ask)" label, and the min_sources relaxation disclosure; JSON round-trips model_dump(mode="json"). Mirror backtest.render_report shape. Implement.
Phase E - CLI (cli.py).
E1: test backtest-pnl writes pnl-backtest-<date>.md/json and prints a summary (monkeypatch cli.backtest_pnl -> canned result, cli.fetch_closed_weather_events -> [], cli.httpx.Client -> dummy, cli._today -> fixed date; mirror test_backtest_command_writes_report). Add _backtest_pnl worker + subparser (--city,--days,--leads,--reports-dir), route in main alongside backtest (no DB).
E2: test backtest-pnl exits non-zero when backtest_pnl returns None (mirror test_backtest_exits_when_no_data). raise SystemExit(1).
E3: update CLAUDE.md Toolchain/layout bullets to mention backtest-pnl (docs-in-same-change). Not test-gated.

Constraints honored: free sources only; API clients fixture-tested only (re.compile(re.escape(...)) + add_callback per-token); surgical (two new files + thin backfill.py/cli.py additions, no Market model change); no DB writes -> no SQL/migration -> dual-backend untouched; golden e2e unaffected (evaluate_market reused read-only).

Verification:
  uv run pytest tests/test_clob_prices.py tests/test_pnl_backtest.py -q
  uv run pytest tests/test_cli.py tests/test_golden_e2e.py -q
  uv run pytest
  uv run ruff check .
  uv run ruff format --check .
  uv run mypy src
New files: src/rainmaker/polymarket/prices.py, src/rainmaker/pnl_backtest.py, tests/test_clob_prices.py, tests/test_pnl_backtest.py, tests/fixtures/clob_prices_history*.json.
