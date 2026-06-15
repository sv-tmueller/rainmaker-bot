import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from rainmaker.backfill import HISTORICAL_FORECAST_URL, NCEI_URL
from rainmaker.backtest import backtest_real, backtest_synthetic, standard_buckets
from rainmaker.config import MIN_CAL_SAMPLES, MIN_SIGMA_F, STATIONS
from rainmaker.polymarket.client import GAMMA_EVENTS_URL, fetch_closed_weather_events
from rainmaker.probability.calibration import CalibrationPair, apply_calibration, fit_calibration
from rainmaker.probability.distribution import Gaussian

FIXTURES = Path(__file__).parent / "fixtures"
KLGA = STATIONS["NYC"]


def _actuals_fixture() -> list[dict[str, Any]]:
    return json.loads((FIXTURES / "ncei_actuals_klga.json").read_text())


def _hist_fixture() -> dict[str, Any]:
    return json.loads((FIXTURES / "openmeteo_hist_multimodel_klga.json").read_text())


def _closed_events() -> list[dict[str, Any]]:
    return json.loads((FIXTURES / "polymarket_closed_weather_events.json").read_text())


def test_backtest_synthetic_scores_the_overlapping_window(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(re.escape(HISTORICAL_FORECAST_URL)), json=_hist_fixture()
    )
    httpx_mock.add_response(url=re.compile(re.escape(NCEI_URL)), json=_actuals_fixture())
    with httpx.Client() as client:
        pair = backtest_synthetic(KLGA, "TMAX", date(2026, 3, 1), date(2026, 3, 5), client)

    assert pair is not None
    for result in (pair.uncalibrated, pair.calibrated):
        assert result.n == 5  # five days have both a forecast and an actual
        assert 0.0 <= result.modal_hit_rate <= 1.0
        assert 0.0 < result.mean_modal_p <= 1.0
        assert result.mean_brier >= 0.0
        assert result.mean_crps >= 0.0
        for q in (0.5, 0.8, 0.9):
            assert 0.0 <= result.coverage[q] <= 1.0
        # every day contributes one (p, won) pair per bucket
        n_buckets = len(standard_buckets(40.0))
        assert sum(b.count for b in result.reliability) == result.n * n_buckets


def test_backtest_synthetic_tmin_fetches_min_field(httpx_mock):
    # A TMIN backtest must request the min field from the archive, not TMAX.
    # Pre-fix backtest_synthetic dropped the variable and always fetched TMAX.
    httpx_mock.add_response(
        url=re.compile(re.escape(HISTORICAL_FORECAST_URL)), json=_hist_fixture()
    )
    httpx_mock.add_response(url=re.compile(re.escape(NCEI_URL)), json=_actuals_fixture())
    with httpx.Client() as client:
        backtest_synthetic(KLGA, "TMIN", date(2026, 3, 1), date(2026, 3, 5), client)
    archive = next(r for r in httpx_mock.get_requests() if HISTORICAL_FORECAST_URL in str(r.url))
    assert archive.url.params["daily"] == "temperature_2m_min"


def test_backtest_synthetic_returns_none_without_overlap(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(re.escape(HISTORICAL_FORECAST_URL)), json=_hist_fixture()
    )
    httpx_mock.add_response(url=re.compile(re.escape(NCEI_URL)), json=[])  # no actuals
    with httpx.Client() as client:
        result = backtest_synthetic(KLGA, "TMAX", date(2026, 3, 1), date(2026, 3, 5), client)
    assert result is None


def test_fetch_closed_weather_events_queries_closed_true(httpx_mock):
    httpx_mock.add_response(url=re.compile(re.escape(GAMMA_EVENTS_URL)), json=[])
    with httpx.Client() as client:
        fetch_closed_weather_events(client)
    assert httpx_mock.get_requests()[0].url.params["closed"] == "true"


def test_backtest_real_scores_closed_markets_and_filters(httpx_mock):
    # backtest_real fetches forecasts + actuals once for the KLGA group.
    httpx_mock.add_response(
        url=re.compile(re.escape(HISTORICAL_FORECAST_URL)), json=_hist_fixture()
    )
    httpx_mock.add_response(url=re.compile(re.escape(NCEI_URL)), json=_actuals_fixture())
    with httpx.Client() as client:
        result = backtest_real(_closed_events(), client, on_or_after=date(2026, 3, 1))
    # The Feb NYC market is date-filtered; London is skipped (not in the registry);
    # the two March NYC markets score.
    assert result is not None
    assert result.n == 2


def test_backtest_real_returns_none_when_all_filtered(httpx_mock):
    with httpx.Client() as client:
        result = backtest_real(_closed_events(), client, on_or_after=date(2027, 1, 1))
    assert result is None


def test_calibration_wiring_changes_result_with_enough_pairs():
    # Verify that the calibrated arm genuinely differs from uncalibrated when
    # there are enough pairs. The IO test fixture has only 5 pairs (<MIN_CAL_SAMPLES),
    # so backtest_synthetic falls back to the widened-uncalibrated path and both
    # arms are identical. This test uses 30 synthetic pairs with a known 5F bias
    # to confirm that apply_calibration, when wired in correctly, shifts the mu.
    n = MIN_CAL_SAMPLES
    raw_g = Gaussian(mu=70.0, sigma=3.0)
    # Actuals are consistently 5F below the forecast -> bias = +5F
    pairs = [CalibrationPair(mu=70.0, sigma=3.0, ensemble_var=9.0, actual=65.0) for _ in range(n)]
    cal = fit_calibration("KLGA", "TMAX", 1, pairs)
    assert abs(cal.bias - 5.0) < 0.01
    g_cal, was_calibrated = apply_calibration(raw_g, cal, min_sigma=MIN_SIGMA_F, min_samples=n)
    assert was_calibrated is True
    # Calibrated mu must differ from raw mu by the fitted bias
    assert g_cal.mu != raw_g.mu
    assert abs(g_cal.mu - (raw_g.mu - cal.bias)) < 0.001
