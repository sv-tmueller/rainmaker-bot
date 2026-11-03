"""Fee-aware paper fill pricing (MVP 3.0, #490).

Pure: a book and three numbers in, a `PaperFill` out, no I/O. The paper trader
records what a live order would cost, so the stake caps fill spend plus taker
fee, and the walk stops at the limit price.

A share bought at price p costs `p * (1 + fee_rate * (1 - p))`: the price plus
a taker fee of `fee_rate * p * (1 - p)`. The walk spends the stake at that
all-in cost, cheapest ask first. `fee_rate` is a required argument; the rate
itself lives in config, not here.

Rules:

- The limit is inclusive: an ask at the limit is taken. A 1e-9 tolerance
  absorbs the drift of a limit computed by subtraction (`0.565 - 0.12` is one
  ulp under `0.445`); it is far below the 0.001 tick.
- `no_fill` when nothing filled or the filled shares are under the book's
  `min_order_size` (strict). A below-minimum `no_fill` keeps its walk numbers;
  only the status says the order did not happen. Otherwise `filled` when the
  stake budget is used up, else `partial`.
- Invariant: `spent_usdc + fee_usdc <= stake_usdc` up to a 1e-9 float tolerance.
- No rounding to ticks, share sizes or cents; that is live-only.
"""

import math
from dataclasses import dataclass
from typing import Literal

from rainmaker.fees import taker_fee_per_share
from rainmaker.polymarket.book import OrderBook

FillStatus = Literal["filled", "partial", "no_fill"]

# Absorbs float drift in the limit comparison (see module docstring).
_EPS_PRICE = 1e-9
# Absorbs float drift when the stake exactly uses up the book.
_EPS_USDC = 1e-9


@dataclass(frozen=True)
class PaperFill:
    status: FillStatus
    shares: float  # walked shares
    spent_usdc: float  # sum of shares x price per level, fee excluded
    fee_usdc: float  # sum of shares x fee_rate x p x (1 - p) per level
    avg_price: float | None  # spent / shares; None when shares == 0


def limit_price(*, p_win: float, floor: float, fee_rate: float) -> float | None:
    """The highest ask whose net edge still meets `floor`, or None if none exists.

    Net edge at ask a is `p_win - a - fee_rate * a * (1 - a)`, strictly
    decreasing on [0, 1] for fee_rate < 1. The limit is the smaller root of
    `fee_rate * a^2 - (1 + fee_rate) * a + (p_win - floor) = 0`, written in the
    rationalized form so there is no cancellation and fee_rate = 0 gives
    `p_win - floor` exactly. The closed form encodes the exponent-1 fee
    schedule (`p * (1 - p)`) that the discovery guard checks (#504); a market
    with another schedule must not reach this function.

    No clamping and no tick rounding (tick rounding is live-only): a root
    outside (0, 1) means no valid limit, so the caller skips the candidate.
    Raises ValueError unless every input is finite, `p_win` is in [0, 1],
    `floor` is >= 0 and `fee_rate` is in [0, 1).
    """
    for name, value in (("p_win", p_win), ("floor", floor), ("fee_rate", fee_rate)):
        if not math.isfinite(value):
            raise ValueError(f"{name} must be finite, got {value}")
    if not 0 <= p_win <= 1:
        raise ValueError(f"p_win must be in [0, 1], got {p_win}")
    if floor < 0:
        raise ValueError(f"floor must be >= 0, got {floor}")
    if not 0 <= fee_rate < 1:
        raise ValueError(f"fee_rate must be in [0, 1), got {fee_rate}")
    k = p_win - floor
    if k <= 0:
        return None
    b = 1 + fee_rate
    limit = 2 * k / (b + math.sqrt(b * b - 4 * fee_rate * k))
    if not 0 < limit < 1:
        return None
    return limit


def price_paper_order(
    book: OrderBook, *, stake_usdc: float, limit_price: float, fee_rate: float
) -> PaperFill:
    """Price one buy of `stake_usdc` (fill plus fee) against `book`'s asks.

    Raises ValueError, before any walk, for a stake that is not finite and
    positive, a limit not finite and in (0, 1), or a fee rate not finite and in
    [0, 1).
    """
    if not math.isfinite(stake_usdc) or stake_usdc <= 0:
        raise ValueError(f"stake_usdc must be a positive finite number, got {stake_usdc}")
    if not math.isfinite(limit_price) or not 0 < limit_price < 1:
        raise ValueError(f"limit_price must be finite and in (0, 1), got {limit_price}")
    if not math.isfinite(fee_rate) or not 0 <= fee_rate < 1:
        raise ValueError(f"fee_rate must be finite and in [0, 1), got {fee_rate}")

    remaining = stake_usdc
    shares = 0.0
    spent = 0.0
    fee = 0.0
    for level in book.asks:
        p = level.price
        if p > limit_price + _EPS_PRICE:
            break
        fee_ps = taker_fee_per_share(p, fee_rate)
        per_share = p + fee_ps
        if per_share * level.size >= remaining:
            take = remaining / per_share
            remaining = 0.0
        else:
            take = level.size
            remaining -= per_share * take
        shares += take
        spent += take * p
        fee += take * fee_ps
        if remaining == 0.0:
            break

    if shares == 0 or shares < book.min_order_size:
        status: FillStatus = "no_fill"
    elif remaining <= _EPS_USDC:
        status = "filled"
    else:
        status = "partial"
    return PaperFill(
        status=status,
        shares=shares,
        spent_usdc=spent,
        fee_usdc=fee,
        avg_price=spent / shares if shares > 0 else None,
    )
