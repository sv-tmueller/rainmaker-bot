"""Tests for the Polymarket trades client (data-api.polymarket.com/trades).

Fixture-tested only. Never hits the live endpoint.
"""

import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from rainmaker.polymarket.trades import (
    TRADES_URL,
    FillPoint,
    fetch_fills,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _trades_fixture() -> dict[str, Any]:
    return json.loads((FIXTURES / "polymarket_trades_weather.json").read_text())


def test_fetch_fills_returns_buy_fills_for_token(httpx_mock):
    """Only BUY trades for the matching token_id (yes_token_id) are returned.

    The fixture is a v2 envelope ({"data": [...]}) with:
    - 2 BUY records for token_id "d0" at timestamps 1772452750 and 1772366350
    - 1 SELL record for token_id "d0" (excluded: not a BUY)
    - 1 BUY record for token_id "d1" (excluded: wrong token)
    """
    httpx_mock.add_response(url=re.compile(re.escape(TRADES_URL)), json=_trades_fixture())
    with httpx.Client() as client:
        fills = fetch_fills("0xcond_d", "d0", client)
    assert len(fills) == 2
    assert all(isinstance(f, FillPoint) for f in fills)
    # timestamps and prices match the BUY records for d0
    by_ts = {f.t: f.p for f in fills}
    assert by_ts[1772452750] == pytest.approx(0.11)
    assert by_ts[1772366350] == pytest.approx(0.12)


def test_fetch_fills_uses_correct_query_params(httpx_mock):
    """v2 filters by condition= (v1's market= is rejected with a 400)."""
    httpx_mock.add_response(url=re.compile(re.escape(TRADES_URL)), json=_trades_fixture())
    with httpx.Client() as client:
        fetch_fills("0xcond_d", "d0", client)
    url = httpx_mock.get_requests()[0].url
    assert url.path.endswith("/v2/trades")
    params = url.params
    assert params["condition"] == "0xcond_d"
    assert params["side"] == "BUY"
    assert params["limit"] == "500"
    assert "market" not in params


def test_fetch_fills_no_asset_returns_empty(httpx_mock):
    """If no BUY records match the requested asset, return empty."""
    httpx_mock.add_response(url=re.compile(re.escape(TRADES_URL)), json=_trades_fixture())
    with httpx.Client() as client:
        fills = fetch_fills("0xcond_d", "zzz_nonexistent", client)
    assert fills == []


def test_fetch_fills_raises_on_http_error(httpx_mock):
    httpx_mock.add_response(url=re.compile(re.escape(TRADES_URL)), status_code=500)
    with httpx.Client() as client:
        with pytest.raises(httpx.HTTPStatusError):
            fetch_fills("0xcond_d", "d0", client)


def test_fetch_fills_410_retired_body_surfaces_in_error(httpx_mock):
    """A retired endpoint must fail loudly and say why, not read as no fills."""
    httpx_mock.add_response(
        url=re.compile(re.escape(TRADES_URL)),
        status_code=410,
        json={"error": "Data API v1 has been retired", "migration": "https://x/v2/"},
    )
    with httpx.Client() as client:
        with pytest.raises(httpx.HTTPStatusError, match="retired"):
            fetch_fills("0xcond_d", "d0", client)


def test_fetch_fills_returns_no_side_fills_for_no_token(httpx_mock):
    """BUY of no_token_id gives the NO ask directly - same fetch_fills call."""
    httpx_mock.add_response(url=re.compile(re.escape(TRADES_URL)), json=_trades_fixture())
    with httpx.Client() as client:
        fills = fetch_fills("0xcond_d", "d1", client)
    assert len(fills) == 1
    assert fills[0].t == 1772452600
    assert fills[0].p == pytest.approx(0.90)
