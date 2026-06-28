"""Read-only CLOB price-history client for the betting P/L backtest.

Polymarket's CLOB exposes a free prices-history endpoint that returns the time
series of a token's traded mid price. The backtest replays past markets at
several lead times, so it snaps this series to a target timestamp per lead.
Tested against saved fixtures only, never a live endpoint.
"""

from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

CLOB_PRICES_URL = "https://clob.polymarket.com/prices-history"

# When the hourly series is empty (thin or short-lived markets), retry once at
# the daily resolution before giving up.
_COARSE_FIDELITY = 720


class PricePoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    t: int  # unix seconds
    p: float  # token mid price in [0, 1]


def fetch_price_history(
    market: str, start_ts: int, end_ts: int, client: httpx.Client, *, fidelity: int = 60
) -> list[PricePoint]:
    """Token price series over [start_ts, end_ts]. Raises on HTTP error.

    `market` is the CLOB token id. An empty result at the requested fidelity
    retries once at the coarser daily fidelity before returning empty.
    """
    points = _get(market, start_ts, end_ts, client, fidelity)
    if not points and fidelity < _COARSE_FIDELITY:
        points = _get(market, start_ts, end_ts, client, _COARSE_FIDELITY)
    return points


def _get(
    market: str, start_ts: int, end_ts: int, client: httpx.Client, fidelity: int
) -> list[PricePoint]:
    resp = client.get(
        CLOB_PRICES_URL,
        params={
            "market": market,
            "startTs": str(start_ts),
            "endTs": str(end_ts),
            "fidelity": str(fidelity),
        },
    )
    resp.raise_for_status()
    history: list[dict[str, Any]] = resp.json()["history"]
    return [PricePoint(t=int(point["t"]), p=float(point["p"])) for point in history]


def last_before(points: list[PricePoint], target_ts: int) -> float | None:
    """Price of the latest point with t strictly before target_ts, or None.

    Use this instead of snap_price when you must not land at or after the
    target (e.g. a settlement timestamp): snap_price returns the nearest point
    regardless of direction, so it can land after the deadline.
    """
    candidates = [pt for pt in points if pt.t < target_ts]
    if not candidates:
        return None
    return max(candidates, key=lambda pt: pt.t).p


def snap_price(points: list[PricePoint], target_ts: int, *, tolerance_s: int) -> float | None:
    """The price of the point nearest target_ts, or None if none is within tolerance.

    Ties break on the earlier timestamp for a deterministic pick.
    """
    if not points:
        return None
    nearest = min(points, key=lambda pt: (abs(pt.t - target_ts), pt.t))
    if abs(nearest.t - target_ts) > tolerance_s:
        return None
    return nearest.p
