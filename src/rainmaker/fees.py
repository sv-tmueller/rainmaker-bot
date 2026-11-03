"""The one per-share taker fee formula, shared by ranking and paper trading.

No imports on purpose: `trading/` must not pull in `ranking/`, whose import
chain reaches the NCEI fetchers.
"""


def taker_fee_per_share(price: float, rate: float) -> float:
    """Taker fee per share bought at `price`: rate * price * (1 - price).

    Same units as `edge = p_win - price` (per share). The ROI-unit fee per USDC
    staked is rate * (1 - price), which is this value divided by price; do not
    subtract that from an edge.
    """
    return rate * price * (1 - price)
