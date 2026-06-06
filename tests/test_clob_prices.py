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
    snap_price,
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


def test_snap_price_returns_nearest_within_tolerance():
    points = [PricePoint(t=1000, p=0.1), PricePoint(t=2000, p=0.2), PricePoint(t=3000, p=0.3)]
    assert snap_price(points, 2100, tolerance_s=200) == pytest.approx(0.2)


def test_snap_price_rejects_beyond_tolerance():
    points = [PricePoint(t=1000, p=0.1), PricePoint(t=2000, p=0.2), PricePoint(t=3000, p=0.3)]
    assert snap_price(points, 2600, tolerance_s=200) is None


def test_snap_price_tie_breaks_on_earlier_timestamp():
    points = [PricePoint(t=1000, p=0.1), PricePoint(t=2000, p=0.2)]
    # 1500 is equidistant from both; the earlier timestamp wins deterministically.
    assert snap_price(points, 1500, tolerance_s=600) == pytest.approx(0.1)


def test_snap_price_empty_is_none():
    assert snap_price([], 1500, tolerance_s=600) is None


def test_fetch_price_history_raises_on_server_error(httpx_mock):
    httpx_mock.add_response(url=re.compile(re.escape(CLOB_PRICES_URL)), status_code=500)
    with httpx.Client() as client:
        with pytest.raises(httpx.HTTPStatusError):
            fetch_price_history("a0", 100, 200, client)
