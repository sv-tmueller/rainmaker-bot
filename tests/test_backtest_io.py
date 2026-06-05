import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from rainmaker.backfill import HISTORICAL_FORECAST_URL, NCEI_URL
from rainmaker.backtest import backtest_synthetic, standard_buckets
from rainmaker.config import STATIONS

FIXTURES = Path(__file__).parent / "fixtures"
KLGA = STATIONS["NYC"]


def _actuals_fixture() -> list[dict[str, Any]]:
    return json.loads((FIXTURES / "ncei_actuals_klga.json").read_text())


def _hist_fixture() -> dict[str, Any]:
    return json.loads((FIXTURES / "openmeteo_hist_multimodel_klga.json").read_text())


def test_backtest_synthetic_scores_the_overlapping_window(httpx_mock):
    httpx_mock.add_response(url=re.compile(re.escape(HISTORICAL_FORECAST_URL)), json=_hist_fixture())
    httpx_mock.add_response(url=re.compile(re.escape(NCEI_URL)), json=_actuals_fixture())
    with httpx.Client() as client:
        result = backtest_synthetic(KLGA, "TMAX", date(2026, 3, 1), date(2026, 3, 5), client)

    assert result is not None
    assert result.n == 5  # five days have both a forecast and an actual
    assert 0.0 <= result.modal_hit_rate <= 1.0
    assert 0.0 < result.mean_modal_p <= 1.0
    assert result.mean_brier >= 0.0
    for q in (0.5, 0.8, 0.9):
        assert 0.0 <= result.coverage[q] <= 1.0
    # every day contributes one (p, won) pair per bucket
    n_buckets = len(standard_buckets(40.0))
    assert sum(b.count for b in result.reliability) == result.n * n_buckets


def test_backtest_synthetic_returns_none_without_overlap(httpx_mock):
    httpx_mock.add_response(url=re.compile(re.escape(HISTORICAL_FORECAST_URL)), json=_hist_fixture())
    httpx_mock.add_response(url=re.compile(re.escape(NCEI_URL)), json=[])  # no actuals
    with httpx.Client() as client:
        result = backtest_synthetic(KLGA, "TMAX", date(2026, 3, 1), date(2026, 3, 5), client)
    assert result is None
