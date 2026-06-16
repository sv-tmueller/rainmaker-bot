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


def _trades_fixture() -> list[dict[str, Any]]:
    return json.loads((FIXTURES / "polymarket_trades_weather.json").read_text())


def test_fetch_fills_returns_buy_fills_for_token(httpx_mock):
    """Only BUY trades for the matching asset (yes_token_id) are returned.

    The fixture has:
    - 2 BUY records for asset "d0" at timestamps 1772452750 and 1772366350
    - 1 SELL record for asset "d0" (excluded: not a BUY)
    - 1 BUY record for asset "d1" (excluded: wrong token)
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
    """The request uses market= (conditionId) and side=BUY query params."""
    httpx_mock.add_response(url=re.compile(re.escape(TRADES_URL)), json=_trades_fixture())
    with httpx.Client() as client:
        fetch_fills("0xcond_d", "d0", client)
    params = httpx_mock.get_requests()[0].url.params
    assert params["market"] == "0xcond_d"
    assert params["side"] == "BUY"


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


def test_fetch_fills_returns_no_side_fills_for_no_token(httpx_mock):
    """BUY of no_token_id gives the NO ask directly - same fetch_fills call."""
    httpx_mock.add_response(url=re.compile(re.escape(TRADES_URL)), json=_trades_fixture())
    with httpx.Client() as client:
        fills = fetch_fills("0xcond_d", "d1", client)
    assert len(fills) == 1
    assert fills[0].t == 1772452600
    assert fills[0].p == pytest.approx(0.90)
