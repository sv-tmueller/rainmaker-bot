import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from rainmaker.polymarket.prices import (
    CLOB_PRICES_URL,
    PricePoint,
    fetch_price_history,
    last_before,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _history() -> dict[str, Any]:
    return json.loads((FIXTURES / "clob_prices_history.json").read_text())


def _empty() -> dict[str, Any]:
    return json.loads((FIXTURES / "clob_prices_history_empty.json").read_text())


def test_fetch_price_history_parses_points_and_queries_clob(httpx_mock):
    httpx_mock.add_response(url=re.compile(re.escape(CLOB_PRICES_URL)), json=_history())
    with httpx.Client() as client:
        points = fetch_price_history("a0", 100, 200, client)
    assert points
    assert all(isinstance(p, PricePoint) for p in points)
    first = points[0]
    assert isinstance(first.t, int) and isinstance(first.p, float)
    params = httpx_mock.get_requests()[0].url.params
    assert params["market"] == "a0"
    assert params["startTs"] == "100"
    assert params["endTs"] == "200"
    assert params["fidelity"] == "60"


def test_fetch_price_history_falls_back_to_coarser_fidelity(httpx_mock):
    # An empty hourly series retries once at the daily fidelity (720 minutes).
    httpx_mock.add_response(url=re.compile(re.escape(CLOB_PRICES_URL)), json=_empty())
    httpx_mock.add_response(url=re.compile(re.escape(CLOB_PRICES_URL)), json=_history())
    with httpx.Client() as client:
        points = fetch_price_history("a0", 100, 200, client, fidelity=60)
    assert points  # the second, populated batch is returned
    requests = httpx_mock.get_requests()
    assert requests[0].url.params["fidelity"] == "60"
    assert requests[1].url.params["fidelity"] == "720"


def test_last_before_max_age_s_returns_point_within_bound():
    points = [PricePoint(t=1000, p=0.1), PricePoint(t=1900, p=0.2)]
    assert last_before(points, 2000, max_age_s=200) == pytest.approx(0.2)


def test_last_before_max_age_s_rejects_point_older_than_bound():
    points = [PricePoint(t=1000, p=0.1)]
    assert last_before(points, 2000, max_age_s=200) is None


def test_last_before_max_age_s_rejects_future_only_points():
    points = [PricePoint(t=2500, p=0.3)]
    assert last_before(points, 2000, max_age_s=1000) is None


def test_fetch_price_history_raises_on_server_error(httpx_mock):
    httpx_mock.add_response(url=re.compile(re.escape(CLOB_PRICES_URL)), status_code=500)
    with httpx.Client() as client:
        with pytest.raises(httpx.HTTPStatusError):
            fetch_price_history("a0", 100, 200, client)
