"""The paper trader (MVP 3.0, #502): price the latest run's recommended Polymarket
bets against the live order book and record them. No order is ever placed.

Orchestration only; the pure pieces live in `pricing` (fill walk, limit price)
and `sizing` (flat stake, caps). `conn`, `client` and `now` are injected, so the
whole path runs on a temp store and a mock transport. This module never imports
`ranking/`: that stack reaches the NCEI fetchers. The net-edge arithmetic uses
`rainmaker.fees`, the same helper the gate uses.

The mode comes from `RAINMAKER_TRADE_MODE`, read by `trade_mode`. Nothing here
runs unless the caller found `paper`; unset, empty, `off` or an unknown value
means off, and `live` is refused until the live batch.

Paper-mode behaviour:

- Every no-trade path (no fresh run, a book error, any geoblock result) is a
  normal outcome and exits 0 at the CLI. Only unexpected exceptions, such as a
  store failure or a `record_paper_order` ValueError, propagate.
- The geoblock result is recorded in the summary, never acted on. The IP is
  never printed.
- A candidate is the recommended Polymarket outcomes of the latest `ok` run
  that started within `TRADE_MAX_RUN_AGE_MINUTES`. Its quoted ask is the run's
  price row; its token id comes from `markets.raw`. Net edge is the stored
  gross edge minus the taker fee at the quoted ask. Sizing orders by net edge.
- The limit is the highest ask at which net edge still meets the gate floor
  (`MIN_EDGE` plus the station delta; the precip gate has no delta).
- A failed book fetch records no row. It still used its planned cap room within
  the run, which is conservative.
- An order whose best ask gives a gross edge over `MAX_EDGE` is refused whole
  and counted as `implausible_edge`. A cheap fill strengthens the stale-quote
  or model-error signal the cap exists for, so the trader refuses rather than
  skipping the cheap levels for pricier ones. Its planned cap room is used, as
  for a failed book fetch.
"""

import math
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, get_args

import httpx

from rainmaker.config import (
    MAX_EDGE,
    MIN_EDGE,
    POLYMARKET_TAKER_FEE_RATE,
    STATION_EDGE_DELTA,
    TRADE_MAX_RUN_AGE_MINUTES,
)
from rainmaker.domain import Market, PrecipMonthlyMarket
from rainmaker.fees import taker_fee_per_share
from rainmaker.polymarket.book import fetch_book
from rainmaker.polymarket.geoblock import fetch_geoblock
from rainmaker.store.db import Conn
from rainmaker.store.query import (
    latest_ok_run,
    new_exposure_for_day,
    open_exposure_by_outcome,
    recommended_polymarket_outcomes,
)
from rainmaker.store.record import record_paper_order
from rainmaker.trading.pricing import limit_price, price_paper_order
from rainmaker.trading.sizing import Candidate, size_orders

TradeMode = Literal["off", "paper", "live"]

_SKIP_REASONS = ("bad_row", "no_token", "no_limit", "implausible_edge")
_BLOCK_REASONS = ("halted", "open_position", "duplicate_in_batch", "open_cap", "daily_cap")
_STATUSES = ("filled", "partial", "no_fill")

# A book fetch or parse failure on one token must not stop the other orders.
_BOOK_ERRORS = (httpx.HTTPError, ValueError, KeyError, TypeError)


def trade_mode(raw: str | None) -> TradeMode:
    """Parse `RAINMAKER_TRADE_MODE`. Exact match only: `PAPER` is unknown.

    Unset and empty are off, silently. An unknown value is off with a stderr
    warning, so a typo does not look like a deliberate switch-off.
    """
    if raw is None or raw == "":
        return "off"
    if raw in get_args(TradeMode):
        return raw  # type: ignore[return-value]
    print(
        f"warning: unknown RAINMAKER_TRADE_MODE {raw!r}, treating it as off "
        "(expected off, paper or live)",
        file=sys.stderr,
    )
    return "off"


def gate_floor(variable: str, icao: str | None) -> float:
    """The recommendation gate's net-edge floor for one market.

    Temperature adds the per-(station, variable) delta; precipitation has none.
    """
    if variable == "PRCP":
        return MIN_EDGE
    return MIN_EDGE + STATION_EDGE_DELTA.get((icao or "", variable), 0.0)


def check_geoblock(client: httpx.Client) -> str:
    """One summary line for the host's geoblock status. Never raises on a bad check.

    In paper mode the result is recorded only. The IP is never included, and a
    parse error shows only its type, because a pydantic message can echo the body.
    """
    suffix = "recorded only in paper"
    try:
        status = fetch_geoblock(client)
    except httpx.HTTPError as exc:
        detail = " ".join(str(exc).split())
        return f"check failed ({type(exc).__name__}: {detail}), {suffix}"
    except ValueError as exc:
        return f"check failed ({type(exc).__name__}), {suffix}"
    place = f"country {status.country or 'unknown'}, region {status.region or 'unknown'}"
    if status.blocked:
        return f"BLOCKED ({place}), {suffix}"
    return f"allowed ({place})"


@dataclass(frozen=True)
class TradeContext:
    """What a sized candidate needs later, joined back by `Candidate`."""

    token_id: str
    p_win: float
    gross_edge: float
    quoted_ask: float
    floor: float
    limit: float


@dataclass(frozen=True)
class Loaded:
    candidates: dict[Candidate, TradeContext]
    skipped: dict[str, int]


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _parse_market(raw: str, variable: str) -> Market | PrecipMonthlyMarket | None:
    try:
        if variable == "PRCP":
            return PrecipMonthlyMarket.model_validate_json(raw)
        if variable in ("TMAX", "TMIN"):
            return Market.model_validate_json(raw)
    except ValueError:
        return None
    return None


def load_candidates(conn: Conn, run_id: str) -> Loaded:
    """The run's recommended Polymarket outcomes as sizing candidates.

    Skips, counted by reason: `bad_row` (a missing or non-finite number, a
    probability outside [0, 1], an ask outside (0, 1), or an odd side),
    `no_token` (the market JSON does not parse, lacks the label, or has an
    empty token) and `no_limit` (no ask meets the gate floor).
    """
    skipped = dict.fromkeys(_SKIP_REASONS, 0)
    candidates: dict[Candidate, TradeContext] = {}
    parsed: dict[str, Market | PrecipMonthlyMarket | None] = {}
    for row in recommended_polymarket_outcomes(conn, run_id):
        p_win, edge, ask, side = row["p_win"], row["edge"], row["quoted_ask"], row["side"]
        if (
            side not in ("YES", "NO")
            or not (_is_number(p_win) and _is_number(edge) and _is_number(ask))
            or not 0 <= p_win <= 1
            or not 0 < ask < 1
        ):
            skipped["bad_row"] += 1
            continue

        market_id = row["market_id"]
        if market_id not in parsed:
            parsed[market_id] = _parse_market(row["raw"], row["variable"])
        market = parsed[market_id]
        bucket = (
            None
            if market is None
            else next((b for b in market.buckets if b.label == row["outcome"]), None)
        )
        token = (
            None
            if bucket is None
            else (bucket.yes_token_id if side == "YES" else bucket.no_token_id)
        )
        if market is None or not token:
            skipped["no_token"] += 1
            continue

        icao = market.target.station.icao if isinstance(market, Market) else None
        floor = gate_floor(row["variable"], icao)
        limit = limit_price(p_win=p_win, floor=floor, fee_rate=POLYMARKET_TAKER_FEE_RATE)
        if limit is None:
            skipped["no_limit"] += 1
            continue

        net_edge = edge - taker_fee_per_share(ask, POLYMARKET_TAKER_FEE_RATE)
        candidate = Candidate(market_id, row["outcome"], side, net_edge)
        candidates[candidate] = TradeContext(token, p_win, edge, ask, floor, limit)
    return Loaded(candidates, skipped)


@dataclass
class TradeSummary:
    mode: str = "paper"
    geoblock: str = ""
    run_id: str | None = None
    run_started_at: str | None = None
    run_age_minutes: float | None = None
    no_trade_reason: str | None = None
    loaded: int = 0
    skipped: dict[str, int] = field(default_factory=lambda: dict.fromkeys(_SKIP_REASONS, 0))
    planned: int = 0
    blocked: dict[str, int] = field(default_factory=lambda: dict.fromkeys(_BLOCK_REASONS, 0))
    recorded: dict[str, int] = field(default_factory=lambda: dict.fromkeys(_STATUSES, 0))
    book_errors: int = 0
    staked_fill_usdc: float = 0.0
    staked_fee_usdc: float = 0.0
    planned_stake_usdc: float = 0.0
    order_lines: list[str] = field(default_factory=list)

    @property
    def staked_usdc(self) -> float:
        return self.staked_fill_usdc + self.staked_fee_usdc


def _run_gate(summary: TradeSummary, run: dict[str, Any] | None, now: datetime) -> bool:
    """Fill the run fields; False (with the reason set) when there is no fresh run."""
    if run is None:
        summary.no_trade_reason = "no ok run in the store"
        return False
    summary.run_id = run["id"]
    summary.run_started_at = run["started_at"]
    try:
        started = datetime.fromisoformat(run["started_at"])
    except (TypeError, ValueError):
        started = None
    if started is None or started.tzinfo is None:
        summary.no_trade_reason = (
            f"run {run['id']} has an unparseable or timezone-less start time "
            f"({run['started_at']!r})"
        )
        return False
    age = now - started
    summary.run_age_minutes = age.total_seconds() / 60
    if age < timedelta(0):
        summary.no_trade_reason = f"run {run['id']} starts in the future ({run['started_at']})"
        return False
    if age > timedelta(minutes=TRADE_MAX_RUN_AGE_MINUTES):
        summary.no_trade_reason = (
            f"run {run['id']} started {summary.run_age_minutes:.0f} min ago, "
            f"over the {TRADE_MAX_RUN_AGE_MINUTES} min limit"
        )
        return False
    return True


def run_paper_trade(
    conn: Conn, client: httpx.Client, *, now: datetime, geoblock: str
) -> TradeSummary:
    """Price and record paper orders for the latest fresh run. Places nothing.

    `now` must be timezone-aware; it drives the age check, the UTC day for the
    daily cap, and every order's `created_at`. `geoblock` is the line from
    `check_geoblock`, carried into the summary.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(UTC)
    summary = TradeSummary(geoblock=geoblock)
    if not _run_gate(summary, latest_ok_run(conn), now):
        return summary
    assert summary.run_id is not None

    loaded = load_candidates(conn, summary.run_id)
    summary.loaded = len(loaded.candidates)
    summary.skipped = loaded.skipped

    by_outcome = open_exposure_by_outcome(conn)
    sized = size_orders(
        loaded.candidates,
        open_positions=set(by_outcome),
        open_exposure_usdc=float(sum(by_outcome.values())),
        new_exposure_today_usdc=new_exposure_for_day(conn, now.date()),
        halted=False,
    )
    summary.planned = len(sized.orders)
    for blocked in sized.blocked:
        summary.blocked[blocked.reason] += 1

    for order in sized.orders:
        c = order.candidate
        ctx = loaded.candidates[c]
        summary.planned_stake_usdc += order.stake_usdc
        try:
            book = fetch_book(ctx.token_id, client)
        except _BOOK_ERRORS as exc:
            summary.book_errors += 1
            summary.order_lines.append(
                f"book error: {c.market_id} {c.outcome} {c.side}: {type(exc).__name__}"
            )
            continue
        # asks are sorted best-first, so asks[0] is the best ask. Refuse the
        # whole order: a marketable limit takes the cheapest asks first, so
        # skipping the cheap levels would not mirror a live order.
        if book.asks and ctx.p_win - book.asks[0].price > MAX_EDGE:
            summary.skipped["implausible_edge"] += 1
            summary.order_lines.append(
                f"skipped: {c.market_id} {c.outcome} {c.side}: implausible edge "
                "(p_win - best_ask over MAX_EDGE)"
            )
            continue
        fill = price_paper_order(
            book,
            stake_usdc=order.stake_usdc,
            limit_price=ctx.limit,
            fee_rate=POLYMARKET_TAKER_FEE_RATE,
        )
        record_paper_order(
            conn,
            run_id=summary.run_id,
            market_id=c.market_id,
            outcome=c.outcome,
            side=c.side,
            token_id=ctx.token_id,
            status=fill.status,
            stake_usdc=order.stake_usdc,
            fill_price=fill.avg_price,
            shares=fill.shares,
            fee_usdc=fill.fee_usdc,
            p_win=ctx.p_win,
            edge=c.edge,
            quoted_ask=ctx.quoted_ask,
            limit_price=ctx.limit,
            book_snapshot=book.model_dump_json(),
            created_at=now,
        )
        summary.recorded[fill.status] += 1
        if fill.status != "no_fill":
            summary.staked_fill_usdc += fill.spent_usdc
            summary.staked_fee_usdc += fill.fee_usdc
        summary.order_lines.append(
            f"order: {c.market_id} {c.outcome} {c.side}: {fill.status}, "
            f"spent {fill.spent_usdc + fill.fee_usdc:.2f} of {order.stake_usdc:.2f} USDC "
            f"(limit {ctx.limit:.3f})"
        )
    return summary


def render_trade_summary(summary: TradeSummary) -> str:
    """The stdout text: one line per order, then the summary block."""
    head = [f"trade: mode {summary.mode}", f"geoblock: {summary.geoblock}"]
    if summary.no_trade_reason is not None:
        return "\n".join([*head, f"no trade: {summary.no_trade_reason}"])
    n_skipped = sum(summary.skipped.values())
    skip_detail = ", ".join(f"{k} {v}" for k, v in summary.skipped.items())
    block_detail = ", ".join(f"{k} {v}" for k, v in summary.blocked.items())
    n_recorded = sum(summary.recorded.values())
    status_detail = ", ".join(f"{k} {v}" for k, v in summary.recorded.items())
    n_staked = summary.recorded["filled"] + summary.recorded["partial"]
    errors = summary.book_errors
    body = [
        f"run: {summary.run_id} started {summary.run_started_at}, "
        f"{summary.run_age_minutes or 0:.0f} min ago",
        f"candidates: {summary.loaded} loaded, {n_skipped} skipped ({skip_detail})",
        f"sizing: {summary.planned} orders, {sum(summary.blocked.values())} blocked "
        f"({block_detail})",
        f"orders: {n_recorded} recorded ({status_detail}), "
        f"{errors} book error{'' if errors == 1 else 's'}",
        f"staked: {summary.staked_usdc:.2f} USDC (fill {summary.staked_fill_usdc:.2f} + "
        f"fee {summary.staked_fee_usdc:.2f}) on {n_staked} filled/partial; "
        f"planned {summary.planned_stake_usdc:.2f} USDC",
    ]
    return "\n".join([*summary.order_lines, *head, *body])
