"""Flat-stake sizing and hard risk caps (MVP 3.0, #478).

Pure policy: plain data in, plain data out, no I/O. The caller (a later trade
step) loads recommendations and exposure, and joins its own context (token id,
quoted ask) back by `Candidate`. Sizing does not re-apply the recommendation
gate; the caller passes recommended outcomes only.

Rules, in the order they apply to each candidate (highest edge first, ties by
market id, outcome, side):

- `halted` (the kill switch) blocks every candidate with "halted".
- A position is keyed by (market_id, outcome); the side is ignored so the bot
  cannot hedge itself. A key in `open_positions` blocks with "open_position".
  A second candidate on a key already planned in this batch blocks with
  "duplicate_in_batch". `open_positions` is presence-only: a
  `max_positions_per_outcome` above 1 needs a spec decision and a count query.
- Every accepted order has the same flat stake. A cap is charged at the planned
  stake (an upper bound on outlay, since the stake caps fill plus fee), on top
  of the exposure the store already reports. An order that reaches a cap
  exactly is allowed. If the room left is less than one stake, the candidate is
  skipped, never shrunk: shrinking would turn flat stake into sizing.
  "open_cap" is checked before "daily_cap", so when both bind the block is
  attributed to the structural cap.
- A blocked candidate uses no room, so a lower-edge new outcome can still fit.

Each candidate lands exactly once in `orders` or `blocked`.
"""

import math
from collections.abc import Iterable
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from typing import Literal

from rainmaker.config import (
    TRADE_MAX_NEW_EXPOSURE_PER_DAY_USDC,
    TRADE_MAX_OPEN_EXPOSURE_USDC,
    TRADE_MAX_POSITIONS_PER_OUTCOME,
    TRADE_STAKE_USDC,
)

Side = Literal["YES", "NO"]
BlockReason = Literal["halted", "open_position", "duplicate_in_batch", "open_cap", "daily_cap"]


@dataclass(frozen=True)
class Candidate:
    market_id: str
    outcome: str  # bucket label, the same string the store keys positions on
    side: Side
    edge: float  # used only for ordering


@dataclass(frozen=True)
class PlannedOrder:
    candidate: Candidate
    stake_usdc: float


@dataclass(frozen=True)
class Blocked:
    candidate: Candidate
    reason: BlockReason


@dataclass(frozen=True)
class SizingResult:
    orders: tuple[PlannedOrder, ...]
    blocked: tuple[Blocked, ...]


def size_orders(
    candidates: Iterable[Candidate],
    *,
    open_positions: AbstractSet[tuple[str, str]],
    open_exposure_usdc: float,
    new_exposure_today_usdc: float,
    halted: bool,
    stake_usdc: float = TRADE_STAKE_USDC,
    max_new_exposure_per_day_usdc: float = TRADE_MAX_NEW_EXPOSURE_PER_DAY_USDC,
    max_open_exposure_usdc: float = TRADE_MAX_OPEN_EXPOSURE_USDC,
    max_positions_per_outcome: int = TRADE_MAX_POSITIONS_PER_OUTCOME,
) -> SizingResult:
    """Turn recommended candidates into flat-stake orders under the risk caps."""
    if stake_usdc <= 0:
        raise ValueError(f"stake_usdc must be positive, got {stake_usdc}")
    ordered = sorted(candidates, key=lambda c: (-c.edge, c.market_id, c.outcome, c.side))
    for c in ordered:
        if not math.isfinite(c.edge):
            raise ValueError(f"edge must be finite, got {c.edge} for {c.market_id}/{c.outcome}")

    if halted:
        return SizingResult(orders=(), blocked=tuple(Blocked(c, "halted") for c in ordered))

    orders: list[PlannedOrder] = []
    blocked: list[Blocked] = []
    planned: dict[tuple[str, str], int] = {}
    open_total = open_exposure_usdc
    day_total = new_exposure_today_usdc

    for c in ordered:
        key = (c.market_id, c.outcome)
        held = 1 if key in open_positions else 0
        reason: BlockReason | None
        if held >= max_positions_per_outcome:
            reason = "open_position"
        elif held + planned.get(key, 0) >= max_positions_per_outcome:
            reason = "duplicate_in_batch"
        elif open_total + stake_usdc > max_open_exposure_usdc:
            reason = "open_cap"
        elif day_total + stake_usdc > max_new_exposure_per_day_usdc:
            reason = "daily_cap"
        else:
            reason = None

        if reason is not None:
            blocked.append(Blocked(c, reason))
            continue
        planned[key] = planned.get(key, 0) + 1
        open_total += stake_usdc
        day_total += stake_usdc
        orders.append(PlannedOrder(c, stake_usdc))

    return SizingResult(orders=tuple(orders), blocked=tuple(blocked))
