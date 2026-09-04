"""Decompose the Kalshi-vs-Polymarket edge gap (read-only diagnostic).

Answers two questions the dashboard's headline ROI hides:

1. Does Kalshi's higher ROI concentrate in the cities where Kalshi settles on a
   different station than Polymarket (NYC: Central Park vs LaGuardia, Chicago:
   Midway vs O'Hare), or is it uniform across every Kalshi city?
2. Is Kalshi's edge real or a price-discovery artifact? Kalshi's thinner books
   mean the stored ask is often a stale last-trade or a loose bid/ask mid, not a
   firm quote. Reprojecting P&L at the stored implied_prob (the market's own
   probability estimate, derived from last-trade or mid) versus the ask isolates
   how much of the edge survives when you stop assuming the ask is fillable.

Both sections are pure functions over settled_rows()-shaped dicts, so no DB
access is needed to test them (mirrors gate_sweep.py's contract). The CLI
wrapper fetches settled_rows() once and passes it in.

Since (an ISO "YYYY-MM-DD" string) restricts to rows whose run started on or
after that date, applied post-dedup exactly as gate_sweep.run_sweep does: rows
is already settled_rows()'s latest-run-per-(market, UTC day) result, and since
is a monotone on-or-after threshold.
"""

from typing import Any

from rainmaker.tracking import (
    _best_per_market_run,
    _bet_won,
    _filter_venue,
    _wilson_interval,
)

_VENUES: tuple[str, ...] = ("polymarket", "kalshi")


def _score(bets: list[dict[str, Any]], *, price_key: str = "ask") -> dict[str, Any]:
    """Grade an already-picked bet list at the price named by price_key.

    Wins pay (1 - price); losses cost price. Rows whose price_key is None or
    whose bucket is ungradable are skipped and counted, never raised.
    """
    n = 0
    wins = 0
    pnl = 0.0
    staked = 0.0
    skipped = 0
    for r in bets:
        price = r.get(price_key)
        if price is None:
            skipped += 1
            continue
        try:
            won = _bet_won(r)
        except (ValueError, KeyError):
            skipped += 1
            continue
        if won is None:
            skipped += 1
            continue
        n += 1
        staked += price
        if won:
            wins += 1
            pnl += 1 - price
        else:
            pnl -= price
    lo, hi = _wilson_interval(wins, n)
    return {
        "n_bets": n,
        "wins": wins,
        "losses": n - wins,
        "hit_rate": wins / n if n else None,
        "wilson_lo": lo,
        "wilson_hi": hi,
        "pnl": pnl,
        "staked": staked,
        "roi": pnl / staked if staked else 0.0,
        "skipped": skipped,
    }


def decompose_by_city_venue(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Score the best-edge bet per (market, run), grouped by (city, venue).

    Uses tracking's _best_per_market_run for the collapse (same tie-break as
    compute_pnl), then partitions the survivors by (city, venue) and scores
    each group at the stored ask. Sorted by venue then city for stable output.
    """
    bets = _best_per_market_run(rows)
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for b in bets:
        city = b.get("city") or "?"
        venue = b.get("venue") or "polymarket"
        groups.setdefault((city, venue), []).append(b)
    result: list[dict[str, Any]] = []
    for (city, venue), group in sorted(groups.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        scored = _score(group)
        result.append({"city": city, "venue": venue, **scored})
    return result


def reproject_at_implied(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Score each venue (and the aggregate) at both the stored ask and the
    stored implied_prob, to isolate price-discovery inflation.

    implied_prob is the market's own probability (last-trade or bid/ask mid on
    Kalshi, the CLOB yes_price on Polymarket), recorded alongside the ask at
    run time. Scoring at implied_prob answers: "if the fillable price were the
    market's midpoint rather than the best ask, how much edge survives?"

    Returns {venue: {"at_ask": score, "at_implied": score}} for polymarket,
    kalshi, and all (aggregate).
    """
    bets = _best_per_market_run(rows)
    venue_specs: list[tuple[str, str | None]] = [("all", None)]
    venue_specs += [(v, v) for v in _VENUES]
    out: dict[str, dict[str, Any]] = {}
    for venue_label, venue in venue_specs:
        subset = bets if venue is None else _filter_venue(bets, venue)
        out[venue_label] = {
            "at_ask": _score(subset, price_key="ask"),
            "at_implied": _score(subset, price_key="implied_prob"),
        }
    return out


def run_venue_decomp(rows: list[dict[str, Any]], since: str | None = None) -> dict[str, Any]:
    """Build the city x venue decomposition and the price-proxy reprojection.

    since (ISO "YYYY-MM-DD") restricts to rows whose run started on or after
    that date, applied post-dedup exactly as gate_sweep.run_sweep.
    """
    if since is not None:
        rows = [r for r in rows if r["started_at"] >= since]
    return {
        "city_venue": decompose_by_city_venue(rows),
        "price_proxy": reproject_at_implied(rows),
    }
