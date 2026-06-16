"""Read-only trades client for the Polymarket data API (data-api.polymarket.com).

Fetches historical BUY fills for a specific token. A buyer's fill price
approximates the ask at that moment, so these fills reconstruct the real
ask-touch for the betting P/L backtest.

The endpoint is filtered server-side by conditionId (the `market` param) and
`side=BUY`. Client-side filtering by `asset` (the token id) isolates the
specific YES or NO token of interest.

Fixture-tested only. Never called against the live endpoint in any test path.
"""

from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

TRADES_URL = "https://data-api.polymarket.com/trades"

# Page size for the trades endpoint. Weather bucket markets are thin;
# 500 is large enough to cover their entire history without pagination.
_LIMIT = 500


class FillPoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    t: int  # unix seconds
    p: float  # fill price (the ask paid by the buyer) in [0, 1]


def fetch_fills(condition_id: str, token_id: str, client: httpx.Client) -> list[FillPoint]:
    """BUY fills for `token_id` in the market identified by `condition_id`.

    Queries the data API for all BUY trades on the given market (conditionId),
    then filters client-side to the specific token. This gives the real fills
    for that token's YES or NO side.

    The filter param is `market` = conditionId (hex hash per sub-market). Each
    Gamma event sub-market carries its own conditionId. The `asset` field in
    each trade record is the numeric token id; it matches the `clobTokenIds`
    from the event JSON.
    """
    resp = client.get(
        TRADES_URL,
        params={
            "market": condition_id,
            "side": "BUY",
            "limit": str(_LIMIT),
        },
    )
    resp.raise_for_status()
    trades: list[dict[str, Any]] = resp.json()
    return [
        FillPoint(t=int(trade["timestamp"]), p=float(trade["price"]))
        for trade in trades
        if trade.get("asset") == token_id and trade.get("side") == "BUY"
    ]
