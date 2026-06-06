# TDD Implementation Plan - Issue #36: TMIN / "Lowest temperature" markets

Scope confirmation: the CLI gate is SUPPORTED_VARIABLES = {"TMAX"} at src/rainmaker/cli.py:38. Open-Meteo (src/rainmaker/forecasts/openmeteo.py) is variable-parametric except _daily_field (hardcodes temperature_2m_max). NWS (src/rainmaker/forecasts/nws.py) rejects non-TMAX and reads only the isDaytime:true period; the same /forecast response also contains the night/low periods. The probability/ranking/render stack is variable-agnostic; calibration cells key on (station, variable, lead) and fall back to UNCALIBRATED_WIDEN when no cell exists, so a TMIN run with no TMIN calibration is safe.

Design decisions:
- D1 (NWS daily low): the calendar-day minimum that GHCND TMIN settles to is the dawn-of-D low, which NWS reports in the NIGHT period that STARTS the evening of D-1. For TMIN, select the night period (isDaytime == False) whose local start date == target.local_date - 1 day. Keep lead_time_days = (target.local_date - issued_local).days keyed to the target date. For a same-day TMIN target NWS has no prior-night period and returns [] (Open-Meteo still supplies it; aggregate degrades gracefully).
- D2 (Open-Meteo): select the single field via _daily_field; do not request both min and max.
- D3 (backfill bug, Task 5, INCLUDE): fetch_historical_forecasts in src/rainmaker/backfill.py takes no variable and hardcodes temperature_2m_max (and model_keys temperature_2m_max_<m>). run_backfill threads variable into fetch_actuals but NOT into fetch_historical_forecasts, so backfill --variable TMIN would fit TMAX-forecast vs TMIN-actual. Fix: thread variable into fetch_historical_forecasts and select temperature_2m_min for TMIN.
- D4: TMIN markets live for NYC + Miami only today; both already in config.STATIONS; no registry change.

Task 1 - Open-Meteo MIN vs MAX by target.variable.
Test first (tests/test_openmeteo.py): add TMIN variants of test_parse_multimodel_returns_one_sample_per_model, test_parse_ensemble_returns_one_sample_per_member, test_open_meteo_source_pools_multimodel_and_ensemble (assert variable=="TMIN", correct min values, 5 multimodel + 90 ensemble), and test_common_params_requests_min_field_for_tmin (assert _common_params(tmin_target)["daily"]=="temperature_2m_min" and =="temperature_2m_max" for TMAX). New fixtures mirroring the max fixtures with temperature_2m_min* fields, same time array (2026-05-30..06-05 so idx=1 for 2026-05-31): tests/fixtures/openmeteo_multimodel_min_klga.json and tests/fixtures/openmeteo_ensemble_gfs_min_klga.json. Run red: uv run pytest tests/test_openmeteo.py -q (NotImplementedError).
Implement: in openmeteo.py replace _daily_field with a map:
  _DAILY_FIELD = {"TMAX": "temperature_2m_max", "TMIN": "temperature_2m_min"}
  def _daily_field(variable: str) -> str:
      try: return _DAILY_FIELD[variable]
      except KeyError: raise NotImplementedError(f"unsupported variable {variable}") from None
No other edit (parse_multimodel, parse_ensemble incl member_re, _common_params already route through _daily_field; _check_fahrenheit startswith(field) isolates temperature_2m_min*). Run green.

Task 2 - NWS daily low (night-before period) for TMIN.
Test first (tests/test_nws.py, reuse existing nws_forecast_klga.json which already has night periods): test_parse_returns_overnight_low_for_target_date with target build_target("NYC","TMIN",date(2026,5,31)) asserting one sample, variable=="TMIN", value_f==52.0 (the "Tonight" period starting 2026-05-30T18:00 isDaytime:false), lead_time_days==1, issued_at==datetime(2026,5,30,14,23,35,tzinfo=UTC); test_parse_tmin_empty_when_night_before_absent (date 2030-01-01 -> []); test_fetch_returns_tmin (httpx_mock, same points->forecast wiring, assert value_f==52.0). Run red (NotImplementedError "Phase 1 supports TMAX only").
Implement: in nws.py replace the guard + daytime-only loop in parse (add timedelta to the datetime import):
  def parse(forecast_json, target):
      props = forecast_json["properties"]
      issued_at = datetime.fromisoformat(props["updateTime"])
      tz = ZoneInfo(target.station.timezone)
      issued_local = issued_at.astimezone(tz).date()
      want_daytime = target.variable == "TMAX"
      match_date = target.local_date if want_daytime else target.local_date - timedelta(days=1)
      for period in props["periods"]:
          start_local = datetime.fromisoformat(period["startTime"]).astimezone(tz)
          if period["isDaytime"] == want_daytime and start_local.date() == match_date:
              if period["temperatureUnit"] != "F": raise ValueError(...)
              return [ForecastSample(source="nws", model="nws", member=None, station=target.station.icao, variable=target.variable, target_date=target.local_date, lead_time_days=(target.local_date - issued_local).days, value_f=float(period["temperature"]), issued_at=issued_at)]
      return []
fetch_raw and NwsSource.fetch unchanged. Existing TMAX tests must still pass. Run green.

Task 3 - Lift the CLI gate.
Test first (tests/test_cli.py): the existing test_run_skips_unsupported_variable uses TMIN as the unsupported example and will break; replace with test_run_processes_tmin_market (feed discover_markets->[_market("TMIN")] + a TMIN forecast set; assert predictions count==1 and "70-71F"/"KLGA" in stdout; update the _forecast_set() helper to accept a variable arg default "TMAX" so it emits variable="TMIN" samples on a TMIN target). Optionally add test_run_skips_when_variable_unsupported that monkeypatches cli.SUPPORTED_VARIABLES={"TMAX"}, feeds _market("TMIN"), asserts "skipped" + "TMIN". Run red.
Implement: src/rainmaker/cli.py:38 -> SUPPORTED_VARIABLES = {"TMAX", "TMIN"}. Run green.

Task 4 - Golden e2e: keep TMAX green, add a TMIN golden.
The existing test_golden_pipeline_on_fixture_market calls evaluate_market directly and stays green (confirm, do not edit). Add test_golden_pipeline_on_tmin_market in tests/test_golden_e2e.py: build a TMIN Market inline for Miami (build_target("Miami","TMIN",...), KMIA) with a small bucket partition + a TMIN ForecastSet (samples variable="TMIN"); assert excluded_no_ask==[] (if every bucket has an ask), YES partition sum(p_win)~1.0, edges sorted desc, render_markdown contains "KMIA" and the settlement date. Run green.

Task 5 - Fix backfill historical-forecast field for TMIN (closes D3).
Test first (tests/test_backfill.py): test_fetch_historical_forecasts_requests_min_field_for_tmin (httpx_mock, assert daily=temperature_2m_min when variable="TMIN", parses temperature_2m_min_<model> keys; small TMIN hist fixture or inline JSON); test_run_backfill_tmin_pairs_min_forecast_with_tmin_actual (mocked NCEI TMIN rows + hist min responses; assert cal.variable=="TMIN"). 
Implement: in src/rainmaker/backfill.py add variable: str = "TMAX" param to fetch_historical_forecasts, derive field = "temperature_2m_min" if variable=="TMIN" else "temperature_2m_max" (you may reuse openmeteo._daily_field), use for the daily= param and the model_keys; pass variable from run_backfill (it already receives variable). No CLI change. Run green.

Conventions honored: free sources only; no new deps; no station-registry change; tests read saved fixtures only; dual-backend untouched (calibration table already variable-keyed).

Verification (run all):
  uv run pytest tests/test_openmeteo.py tests/test_nws.py tests/test_cli.py tests/test_golden_e2e.py tests/test_backfill.py -q
  uv run pytest -q
  uv run ruff check .
  uv run ruff format --check .
  uv run mypy src
