"""Read-only CLOB order book client and the stake fill-price walk.

The paper trader prices a would-be bet at what the live book fills, not at the
Gamma `bestAsk`. Findings from the live endpoint that shape this module:

- `GET /book?token_id=<id>` returns prices, sizes and the timestamp (epoch
  milliseconds) as strings, and an empty side as `[]`.
- Levels arrive worst first: bids ascending, asks descending. The best price is
  the LAST element on both sides, so the models sort best first on construction.
- The NO token has its own book, a mirror of the YES book (NO asks are
  `1 - YES bids` at the same sizes). NO is priced from the NO token's book.
- `last_trade_price` on the NO book carries the YES price, so it is not parsed.
- An unknown or closed token is HTTP 404, a different case from an empty book.

Tested against saved fixtures only, never a live endpoint. No auth, no orders.
"""

from pydantic import BaseModel, ConfigDict, Field, field_validator

CLOB_BOOK_URL = "https://clob.polymarket.com/book"

# Absorbs float drift when the stake exactly uses up the book.
_EPS_USDC = 1e-9


class BookLevel(BaseModel):
    model_config = ConfigDict(frozen=True)

    price: float = Field(gt=0, lt=1)  # USDC per share
    size: float = Field(gt=0)  # shares


class OrderBook(BaseModel):
    """One token's book. `asks` ascending and `bids` descending: best first."""

    model_config = ConfigDict(frozen=True)

    asset_id: str
    market: str
    timestamp_ms: int
    bids: list[BookLevel]
    asks: list[BookLevel]

    @field_validator("asks")
    @classmethod
    def _asks_best_first(cls, levels: list[BookLevel]) -> list[BookLevel]:
        return sorted(levels, key=lambda lv: lv.price)

    @field_validator("bids")
    @classmethod
    def _bids_best_first(cls, levels: list[BookLevel]) -> list[BookLevel]:
        return sorted(levels, key=lambda lv: lv.price, reverse=True)


class Fill(BaseModel):
    model_config = ConfigDict(frozen=True)

    token_id: str
    stake_usdc: float
    spent_usdc: float
    shares: float
    avg_price: float | None  # spent / shares; None when nothing filled
    sufficient: bool  # the whole stake was filled from the book


def walk_asks(book: OrderBook, stake_usdc: float) -> Fill:
    """Fill a USDC stake against the asks, cheapest level first.

    On insufficient depth this returns the partial fill (all ask depth,
    `spent_usdc < stake_usdc`, `sufficient=False`) and does not raise; the
    caller decides what to do. Live market orders are fill-or-kill, so skipping
    is the likely choice. An empty ask side fills nothing (`avg_price=None`).
    No rounding to ticks or share sizes happens here.
    """
    if stake_usdc <= 0:
        raise ValueError(f"stake_usdc must be positive, got {stake_usdc}")
    remaining = stake_usdc
    shares = 0.0
    for level in book.asks:
        cost = level.price * level.size
        if cost >= remaining:
            shares += remaining / level.price
            remaining = 0.0
            break
        shares += level.size
        remaining -= cost
    spent = stake_usdc - remaining
    return Fill(
        token_id=book.asset_id,
        stake_usdc=stake_usdc,
        spent_usdc=spent,
        shares=shares,
        avg_price=spent / shares if shares > 0 else None,
        sufficient=remaining <= _EPS_USDC,
    )
