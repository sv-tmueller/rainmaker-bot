"""Entry-delay replay (read-only diagnostic, #526).

Tracking scores each recommended bet at the ask recorded at run time. A human
who reads the dashboard bets some hours later, so this replays every settled
recommended Polymarket bet at run time +1h, +3h, +6h and +12h from the CLOB
prices-history series and reports hypothetical ROI per delay and per lead,
beside the ROI at the recorded ask over the same priced bets.

A +0h row (the last trade before the run) sits beside the delays. Trade prices
usually sit below the ask, so without it the gap between a delay row and the
ask would mix the delay effect with the ask-versus-trade gap. Read +1h to +12h
against +0h for the drift on one price basis.

Prices are trade prices from prices-history, never asks. A NO bet is priced at
1 - YES. Scoring is the flat-stake formula of venue_decomp._score, gross of
fees, and each bet is taken at the delayed price with no re-check of its edge.

A bet with no price at a delay is counted by reason and never dropped:
no_token, fetch_error, past_settlement_day, no_price. For every (lead, delay)
cell, priced + missing equals the gradable bet count.
"""

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from pydantic import ValidationError

from rainmaker.domain import Market, PrecipMonthlyMarket, _stored_timestamp
from rainmaker.polymarket.prices import PricePoint, fetch_price_history, last_before
from rainmaker.store.db import Conn
from rainmaker.tracking import (
    _best_per_market_run,
    _bet_won,
    _filter_venue,
    _lead_bucket,
    _raw_by_market_id,
    settled_rows,
)
from rainmaker.venue_decomp import _score

DELAYS_H: tuple[int, ...] = (0, 1, 3, 6, 12)
# The history is hourly, so two hours allows one missing point. It rejects the
# 12h-spaced points of the coarse fallback, so a stale price never counts.
DELAY_SNAP_TOLERANCE_S = 2 * 3600
_FETCH_LEAD_S = 2 * 3600  # window start before the run
_FETCH_TRAIL_S = max(DELAYS_H) * 3600  # window end after the run

MISS_REASONS: tuple[str, ...] = ("no_token", "fetch_error", "past_settlement_day", "no_price")
_LEAD_ORDER: tuple[str, ...] = ("0", "1", "2", "3+", "<0 (catch-up)")


@dataclass(frozen=True)
class ReplayBet:
    """One gradable bet with what the replay needs: its YES price series, or
    the reason there is none (no_token, fetch_error)."""

    row: dict[str, Any]
    run_ts: int
    cutoff_ts: int | None
    points: list[PricePoint] | None
    miss: str | None = None


def select_bets(rows: list[dict[str, Any]], since: str | None) -> tuple[list[dict[str, Any]], int]:
    """The Polymarket bets `track` scores, split into gradable bets and a count
    of ungradable ones.

    since (ISO "YYYY-MM-DD") restricts to runs started on or after that date,
    applied after the settled_rows dedup as venue_decomp does.
    """
    if since is not None:
        rows = [r for r in rows if r["started_at"] >= since]
    bets = _best_per_market_run(_filter_venue(rows, "polymarket"))
    gradable: list[dict[str, Any]] = []
    n_ungradable = 0
    for b in bets:
        try:
            won = _bet_won(b)
        except (ValueError, KeyError):
            won = None
        if won is None:
            n_ungradable += 1
        else:
            gradable.append(b)
    return gradable, n_ungradable


def settlement_cutoff_ts(settlement_date: str, timezone: str) -> int:
    """Unix time of the local midnight that ends the settlement date.

    From then on the day's outcome is fully observed, so a later price is not a
    forecast bet anyone could still take.
    """
    next_day = date.fromisoformat(settlement_date) + timedelta(days=1)
    return int(datetime.combine(next_day, time(0), tzinfo=ZoneInfo(timezone)).timestamp())


def _token_and_timezone(raw: str | None, bucket_label: str) -> tuple[str | None, str | None]:
    """YES token and station timezone from markets.raw, for temperature and
    monthly-precip markets alike. (None, None) when raw does not parse."""
    if not raw:
        return None, None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None, None
    market: Market | PrecipMonthlyMarket
    try:
        market = Market.model_validate(payload)
    except ValidationError:
        try:
            market = PrecipMonthlyMarket.model_validate(payload)
        except ValidationError:
            return None, None
    tz = market.target.station.timezone
    for b in market.buckets:
        if b.label == bucket_label:
            return b.yes_token_id, tz
    return None, tz


def load_replay_bets(
    conn: Conn, client: httpx.Client, bets: list[dict[str, Any]]
) -> list[ReplayBet]:
    """Attach each bet's price series: one prices-history call per bet."""
    raw_by_market = _raw_by_market_id(conn, [b["market_id"] for b in bets])
    items: list[ReplayBet] = []
    for b in bets:
        run_ts = int(_stored_timestamp(b["started_at"]).timestamp())
        token, tz = _token_and_timezone(raw_by_market.get(b["market_id"]), b["bucket"])
        if token is None or tz is None:
            items.append(ReplayBet(b, run_ts, None, None, "no_token"))
            continue
        cutoff = settlement_cutoff_ts(b["settlement_date"], tz)
        try:
            points = fetch_price_history(
                token, run_ts - _FETCH_LEAD_S, run_ts + _FETCH_TRAIL_S, client
            )
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            items.append(ReplayBet(b, run_ts, cutoff, None, "fetch_error"))
            continue
        items.append(ReplayBet(b, run_ts, cutoff, points))
    return items


def _delay_price(item: ReplayBet, delay_h: int) -> tuple[float | None, str | None]:
    """(price in the bet's own side, None) or (None, miss reason)."""
    if item.points is None:
        return None, item.miss or "no_token"
    target = item.run_ts + delay_h * 3600
    if item.cutoff_ts is not None and target >= item.cutoff_ts:
        return None, "past_settlement_day"
    yes = last_before(item.points, target, max_age_s=DELAY_SNAP_TOLERANCE_S)
    if yes is None:
        return None, "no_price"
    return (1.0 - yes if (item.row.get("side") or "YES") == "NO" else yes), None


def _cell(items: list[ReplayBet], delay_h: int) -> dict[str, Any]:
    priced: list[dict[str, Any]] = []
    missing = dict.fromkeys(MISS_REASONS, 0)
    for item in items:
        price, reason = _delay_price(item, delay_h)
        if price is None:
            missing[reason or "no_price"] += 1
        else:
            priced.append({**item.row, "delay_price": price})
    return {
        "priced": len(priced),
        "missing": missing,
        "at_delay": _score(priced, price_key="delay_price"),
        "at_ask": _score(priced, price_key="ask"),
    }


def _block(items: list[ReplayBet]) -> dict[str, Any]:
    return {"n": len(items), "delays": {d: _cell(items, d) for d in DELAYS_H}}


def summarize(items: list[ReplayBet], n_ungradable: int) -> dict[str, Any]:
    """Pure core: gradable bets with their series in, per-lead delay table out."""
    by_lead: dict[str, list[ReplayBet]] = {}
    for item in items:
        lead = _lead_bucket(item.row["settlement_date"], item.row["started_at"])
        by_lead.setdefault(lead, []).append(item)
    leads = {lead: _block(by_lead[lead]) for lead in _LEAD_ORDER if lead in by_lead}
    leads["all"] = _block(items)
    return {
        "n_bets": len(items) + n_ungradable,
        "n_gradable": len(items),
        "n_ungradable": n_ungradable,
        "headline": _score([i.row for i in items], price_key="ask"),
        "leads": leads,
    }


def run_delay_replay(conn: Conn, client: httpx.Client, since: str | None = None) -> dict[str, Any]:
    bets, n_ungradable = select_bets(settled_rows(conn), since)
    return summarize(load_replay_bets(conn, client, bets), n_ungradable)


def _pct(value: float) -> str:
    return f"{value:+.1%}"


def render_delay_replay(result: dict[str, Any], since: str | None = None) -> list[str]:
    lines: list[str] = []
    if since is not None:
        lines.append(f"since: {since}")
    lines.append(
        f"Polymarket bets: {result['n_bets']} settled, {result['n_gradable']} gradable, "
        f"{result['n_ungradable']} ungradable (excluded from every row)"
    )
    if result["n_gradable"] == 0:
        lines.append("no settled Polymarket bets to replay")
    else:
        head = result["headline"]
        lines.append(
            f"ROI at the recorded ask, all gradable bets: {_pct(head['roi'])} (n={head['n_bets']})"
        )
        for lead, block in result["leads"].items():
            label = "all leads" if lead == "all" else f"lead {lead}"
            lines.append("")
            lines.append(f"{label} (n={block['n']})")
            lines.append(
                f"  {'delay':<6} {'priced':>6} {'missing':>7}  "
                f"{'ROI at delay':>12}  {'ROI at ask, same bets':>21}"
            )
            for delay in DELAYS_H:
                cell = block["delays"][delay]
                n_missing = block["n"] - cell["priced"]
                by_reason = "/".join(str(cell["missing"][r]) for r in MISS_REASONS)
                if cell["priced"]:
                    roi_delay = _pct(cell["at_delay"]["roi"])
                    roi_ask = _pct(cell["at_ask"]["roi"])
                else:
                    roi_delay = roi_ask = "n/a"
                lines.append(
                    f"  {f'+{delay}h':<6} {cell['priced']:>6} {n_missing:>7}  "
                    f"{roi_delay:>12}  {roi_ask:>21}   missing {by_reason}"
                )
    lines.append("")
    lines.append(f"missing counts are {' / '.join(MISS_REASONS)}.")
    lines.append(
        "delay prices are CLOB prices-history trade prices, not asks; "
        "the run-time column is the recorded ask."
    )
    lines.append("+0h is the last trade before the run: compare +1h..+12h against it for drift.")
    lines.append("NO bets are priced at 1 - YES. Results are gross of fees. Polymarket only.")
    lines.append(
        "each bet is taken at the delayed price with no re-check of edge (no re-gating); "
        "a delay at or after local midnight ending the settlement date counts as missing."
    )
    return lines
