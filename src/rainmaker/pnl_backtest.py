"""Betting P/L backtest: replay closed markets at the prices the bot would have paid.

The forecast backtest in backtest.py measures calibration only; it has no prices.
This module adds the money question: had the bot bet its recommendations at the
historical CLOB price, what would the P/L have been? It reuses the live
edge-ranking path (evaluate_market) so the replay and production agree on what is
a recommended bet.

Caveats baked in by design:
- The Open-Meteo archive is one source at roughly lead 1. The live min-sources
  gate (two independent sources) cannot be replayed, so recommended here is a
  superset of what the live bot would emit. min_sources defaults to 1.
- The forecast is keyed to the settlement date and is identical across leads;
  only the market price varies by lead.
- The price used is the token mid, mildly optimistic versus the ask actually paid.
  With ask_source="trades", real BUY fills from data-api.polymarket.com replace
  the mid when available; a fill IS the ask paid, so no spread is added on top.
"""

import json
from collections import defaultdict
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict

from rainmaker.backfill import fetch_actuals, fetch_historical_samples
from rainmaker.config import CONFIDENCE_FLOOR, MIN_EDGE, MIN_SIGMA_F, Target
from rainmaker.domain import Market, parse_bucket_label
from rainmaker.forecasts.base import ForecastSample, ForecastSet, SourceCoverage
from rainmaker.polymarket.markets import parse_market
from rainmaker.polymarket.prices import PricePoint, fetch_price_history, snap_price
from rainmaker.polymarket.trades import FillPoint, fetch_fills
from rainmaker.probability.distribution import fit_gaussian
from rainmaker.probability.outcomes import bucket_probability, settles
from rainmaker.ranking.edge import RankedOutcome, evaluate_market

# A lead's price is snapped from the series within this window of the target
# timestamp; an hourly series puts every midday target well inside it.
SNAP_TOLERANCE_S = 12 * 3600
SECONDS_PER_DAY = 86400


class Bet(BaseModel):
    model_config = ConfigDict(frozen=True)

    lead: int
    bucket_label: str
    side: Literal["YES", "NO"]
    p_win: float
    ask: float  # price paid: the YES ask for a YES bet, the NO ask for a NO bet
    edge: float
    won: bool


class LeadPnl(BaseModel):
    model_config = ConfigDict(frozen=True)

    lead: int  # the forecast lead in days, or -1 for the all-leads total
    n_bets: int
    wins: int
    losses: int
    total_pnl: float
    roi: float
    win_rate: float
    mean_edge: float


class FillCoverage(BaseModel):
    model_config = ConfigDict(frozen=True)

    n_leads: int  # total (market x lead) repricing opportunities
    fills_used: int  # repricing opportunities where at least one fill snapped


class PnlBacktestResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    n_markets: int
    floor: float
    floor_no: float | None = None  # per-side NO floor; None means flat (same as floor)
    min_sources: int
    min_edge: float
    spread: float = 0.0
    ask_source: Literal["mid", "trades"] = "mid"
    fill_coverage: FillCoverage | None = None
    max_edge: float | None = None  # upper edge cap applied in replay; None = no cap
    max_p_win: float | None = None  # upper p_win cap applied in replay; None = no cap
    per_lead: list[LeadPnl]
    overall: LeadPnl


def forecast_set_from_samples(target: Target, samples: list[ForecastSample]) -> ForecastSet:
    """Pool archive samples into a single-source ForecastSet for evaluate_market."""
    return ForecastSet(
        target=target,
        samples=samples,
        coverage=[SourceCoverage(source="open-meteo", ok=True, n_samples=len(samples))],
    )


def market_at_lead(
    market: Market,
    mids: dict[str, float | None],
    *,
    spread: float = 0.0,
    fills: dict[str, tuple[float | None, float | None]] | None = None,
) -> Market:
    """Reprice a market's buckets from per-bucket mids keyed by bucket label.

    A bucket's YES ask is the mid and its NO ask the complement (1 - mid). With a
    positive `spread` the ask paid is the mid plus half the spread on each side
    (ask = mid + spread/2, capped at 1), the symmetric-book approximation of the
    cost actually paid; spread=0 is the raw mid. A missing or None mid leaves both
    sides unpriced so evaluate_market drops it.

    When `fills` is provided (trades mode), each entry is a (yes_fill, no_fill)
    pair keyed by bucket label. A non-None fill is used directly as the ask for
    that side (a fill IS the ask paid; no spread added). A None fill falls back to
    the mid+spread path for that side.
    """
    half = spread / 2
    buckets = []
    for bucket in market.buckets:
        mid = mids.get(bucket.label)
        if mid is None:
            best_ask: float | None = None
            no_ask: float | None = None
        else:
            yes_fill: float | None = None
            no_fill: float | None = None
            if fills is not None:
                pair = fills.get(bucket.label)
                if pair is not None:
                    yes_fill, no_fill = pair
            best_ask = yes_fill if yes_fill is not None else min(mid + half, 1.0)
            no_ask = no_fill if no_fill is not None else min((1 - mid) + half, 1.0)
        buckets.append(bucket.model_copy(update={"best_ask": best_ask, "no_ask": no_ask}))
    return market.model_copy(update={"buckets": buckets})


def _snap_fills(fill_list: list[FillPoint], target_ts: int) -> float | None:
    """Snap the nearest BUY fill to target_ts, returning the price or None."""
    points = [PricePoint(t=f.t, p=f.p) for f in fill_list]
    return snap_price(points, target_ts, tolerance_s=SNAP_TOLERANCE_S)


def _outcome_won(outcome: RankedOutcome, actual: float) -> bool:
    """A YES bet wins when its bucket settles; a NO bet wins when it does not."""
    settled = settles(*parse_bucket_label(outcome.bucket_label), actual)
    return (not settled) if outcome.side == "NO" else settled


def replay_market(
    market: Market,
    forecast_set: ForecastSet,
    actual: float,
    histories: dict[str, list[PricePoint]],
    settlement_dt: datetime,
    *,
    leads: Sequence[int],
    floor: float,
    min_sources: int,
    min_sigma: float,
    min_edge: float,
    spread: float = 0.0,
    fill_histories: dict[str, list[FillPoint]] | None = None,
    floor_no: float | None = None,
    max_edge: float | None = None,
    max_p_win: float | None = None,
) -> tuple[list[Bet], int]:
    """One best-edge bet per lead, settled against the actual.

    At each lead the buckets are repriced from their CLOB mid (snapped to the
    settlement timestamp minus the lead), edge-ranked through the live path, and
    collapsed to the single highest-edge recommended bet. Buckets on one market
    describe the same temperature, so counting more than one would inflate the
    P/L; the collapse mirrors live tracking. A lead with no recommended bet or no
    snappable price contributes nothing.

    When `fill_histories` is provided (trades mode), fills are snapped per bucket
    token and used as the ask in place of mid+spread. Coverage is tracked as the
    count of leads where at least one fill snapped for any candidate bucket token.

    Returns (bets, fills_used_count).
    """
    settlement_ts = int(settlement_dt.timestamp())
    bets: list[Bet] = []
    fills_used = 0
    for lead in leads:
        target_ts = settlement_ts - lead * SECONDS_PER_DAY
        mids = {
            bucket.label: snap_price(
                histories.get(bucket.yes_token_id, []), target_ts, tolerance_s=SNAP_TOLERANCE_S
            )
            for bucket in market.buckets
        }
        fills: dict[str, tuple[float | None, float | None]] | None = None
        if fill_histories is not None:
            fills = {}
            this_lead_used = False
            for bucket in market.buckets:
                yes_snapped = _snap_fills(fill_histories.get(bucket.yes_token_id, []), target_ts)
                no_snapped = _snap_fills(fill_histories.get(bucket.no_token_id, []), target_ts)
                fills[bucket.label] = (yes_snapped, no_snapped)
                if yes_snapped is not None or no_snapped is not None:
                    this_lead_used = True
            if this_lead_used:
                fills_used += 1

        report = evaluate_market(
            market_at_lead(market, mids, spread=spread, fills=fills),
            forecast_set,
            floor=floor,
            floor_no=floor_no,
            min_sources=min_sources,
            min_sigma=min_sigma,
            min_edge=min_edge,
        )
        recommended = [o for o in report.outcomes if o.recommended]
        if not recommended:
            continue
        # Apply upper caps after evaluate_market, before picking the best bet.
        # A capped lead falls through to the next-best recommended bet; if none
        # remain, the lead is skipped (no bet for this lead).
        if max_edge is not None or max_p_win is not None:
            recommended = [
                o
                for o in recommended
                if (max_edge is None or o.edge <= max_edge)
                and (max_p_win is None or o.p_win <= max_p_win)
            ]
        if not recommended:
            continue
        best = max(recommended, key=lambda o: (o.edge, o.p_win, o.bucket_label, o.side))
        bets.append(
            Bet(
                lead=lead,
                bucket_label=best.bucket_label,
                side=best.side,
                p_win=best.p_win,
                ask=best.best_ask,
                edge=best.edge,
                won=_outcome_won(best, actual),
            )
        )
    return bets, fills_used


def _metrics(lead: int, bets: list[Bet]) -> LeadPnl:
    """Flat one-unit stake: a win returns 1 - ask, a loss costs the ask."""
    n = len(bets)
    wins = sum(1 for b in bets if b.won)
    staked = sum(b.ask for b in bets)
    total_pnl = sum((1 - b.ask) if b.won else -b.ask for b in bets)
    return LeadPnl(
        lead=lead,
        n_bets=n,
        wins=wins,
        losses=n - wins,
        total_pnl=total_pnl,
        roi=total_pnl / staked if staked else 0.0,
        win_rate=wins / n if n else 0.0,
        mean_edge=sum(b.edge for b in bets) / n if n else 0.0,
    )


def score(bets: list[Bet], leads: Sequence[int]) -> tuple[list[LeadPnl], LeadPnl]:
    """Per-lead and pooled P/L. Leads with no bets are reported as zeroed rows."""
    per_lead = [_metrics(lead, [b for b in bets if b.lead == lead]) for lead in leads]
    return per_lead, _metrics(-1, list(bets))


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def _row(label: str, lp: LeadPnl) -> str:
    return (
        f"| {label} | {lp.n_bets} | {lp.wins}-{lp.losses} | {_pct(lp.win_rate)} | "
        f"{lp.total_pnl:+.2f}u | {lp.roi:+.1%} | {_pct(lp.mean_edge)} |"
    )


def render_pnl_report(result: PnlBacktestResult) -> tuple[str, dict[str, Any]]:
    """Markdown report plus a JSON-able payload. Terminal prints the markdown."""
    lines = [
        "# Betting P/L backtest",
        "",
        f"Hypothetical P/L over {result.n_markets} closed market(s) at a flat "
        "one-unit stake, replayed at several forecast leads. "
        + (
            f"The ask paid is the token mid plus a {result.spread:.2f} spread "
            "haircut (ask = mid + spread/2)."
            if result.spread > 0
            else "The price is the token mid (mid-based, mildly optimistic versus "
            "the ask actually paid)."
        ),
        "",
        f"min_sources is relaxed to {result.min_sources}: the archive is one "
        "source, so recommended here is a superset of the live two-source gate. "
        f"Floor {_pct(result.floor)}, minimum edge {_pct(result.min_edge)}.",
        "",
    ]
    if result.ask_source == "trades" and result.fill_coverage is not None:
        cov = result.fill_coverage
        lines.append(
            f"Ask source: trades fills. Fill coverage: {cov.fills_used} of "
            f"{cov.n_leads} lead-market slots had a real fill "
            f"({cov.fills_used}/{cov.n_leads}); remaining slots fall back to mid.",
        )
        lines.append("")
    if result.max_edge is not None or result.max_p_win is not None:
        cap_parts = []
        if result.max_edge is not None:
            cap_parts.append(f"max_edge={_pct(result.max_edge)}")
        if result.max_p_win is not None:
            cap_parts.append(f"max_p_win={_pct(result.max_p_win)}")
        lines.append(
            f"Upper cap applied in replay ({', '.join(cap_parts)}): recommended outcomes "
            "above the cap are excluded before picking the best-edge bet. A capped lead "
            "falls through to the next-best recommended bet; it is skipped only when no "
            "recommended bet remains under the cap.",
        )
        lines.append("")
    lines += [
        "| Lead | Bets | W-L | Win rate | Total P/L | ROI | Mean edge |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    lines += [_row(str(lp.lead), lp) for lp in result.per_lead]
    lines.append(_row("ALL", result.overall))
    md = "\n".join(lines) + "\n"
    return md, result.model_dump(mode="json")


def _parse_closed_markets(
    events: list[dict[str, Any]], on_or_after: date, city: str | None
) -> list[tuple[Market, datetime, dict[str, str]]]:
    """Parsed TMAX markets settling on or after the cutoff, with their settlement time.

    Mirrors backtest_real's parse and filter, keeping the raw endDate so the
    replay can map a lead to a price timestamp. Non-US-city or unmapped markets
    are skipped, as is anything outside the window or city filter.

    Also returns a mapping of yes_token_id -> conditionId for each sub-market
    that carries a conditionId field (used in trades mode).
    """
    out: list[tuple[Market, datetime, dict[str, str]]] = []
    for ev in events:
        try:
            market = parse_market(ev)
        except (ValueError, KeyError):
            continue
        if market.target.variable != "TMAX" or market.target.local_date < on_or_after:
            continue
        if city not in (None, "all") and market.target.station.city != city:
            continue
        # Build yes_token_id -> conditionId from raw sub-market JSON when present.
        cond_ids: dict[str, str] = {}
        for raw_sub in ev.get("markets", []):
            cond_id = raw_sub.get("conditionId")
            if cond_id:
                tokens = json.loads(raw_sub["clobTokenIds"])
                if tokens:
                    cond_ids[tokens[0]] = cond_id
        out.append((market, datetime.fromisoformat(ev["endDate"]), cond_ids))
    return out


def backtest_pnl(
    events: list[dict[str, Any]],
    client: httpx.Client,
    *,
    on_or_after: date,
    leads: Sequence[int] = (0, 1, 2, 3),
    floor: float = CONFIDENCE_FLOOR,
    floor_no: float | None = None,
    min_sources: int = 1,
    min_sigma: float = MIN_SIGMA_F,
    min_edge: float = MIN_EDGE,
    city: str | None = None,
    spread: float = 0.0,
    ask_source: Literal["mid", "trades"] = "mid",
    max_edge: float | None = None,
    max_p_win: float | None = None,
) -> PnlBacktestResult | None:
    """Replay closed markets at their historical CLOB price and score the P/L.

    Groups parsed markets by station, fetches the archive forecast and NOAA
    actual once per group, then per market fetches the price series for the
    buckets that could clear the confidence floor (on either side) and replays
    each lead. Returns None when nothing scorable remains. min_sources defaults
    to 1 because the archive is a single source; recommended here is therefore a
    superset of the live two-source gate.

    When ask_source="trades", real BUY fills from data-api.polymarket.com are
    fetched for each candidate bucket and used as the ask in place of mid+spread
    where available. Fill coverage is reported in the result.
    """
    parsed = _parse_closed_markets(events, on_or_after, city)
    if not parsed:
        return None

    by_station: dict[str, list[tuple[Market, datetime, dict[str, str]]]] = defaultdict(list)
    for market, settlement_dt, cond_ids in parsed:
        by_station[market.target.station.icao].append((market, settlement_dt, cond_ids))

    max_lead = max(leads)
    bets: list[Bet] = []
    n_markets = 0
    total_n_leads = 0
    total_fills_used = 0
    for group in by_station.values():
        station = group[0][0].target.station
        if station.ghcnd_id is None:
            continue  # intl stations have no NCEI proxy; cannot grade (mirrors #204)
        dates = [m.target.local_date for m, _, _ in group]
        samples_by_date = fetch_historical_samples(station, min(dates), max(dates), client)
        actuals = fetch_actuals(station.ghcnd_id, min(dates), max(dates), client, "TMAX")
        for market, settlement_dt, cond_ids in group:
            samples = samples_by_date.get(market.target.local_date)
            actual = actuals.get(market.target.local_date)
            if not samples or actual is None:
                continue
            n_markets += 1
            forecast_set = forecast_set_from_samples(market.target, samples)
            gaussian = fit_gaussian(samples, min_sigma=min_sigma)
            start_ts = int(settlement_dt.timestamp()) - (max_lead + 1) * SECONDS_PER_DAY
            end_ts = int(settlement_dt.timestamp()) + 3600
            histories: dict[str, list[PricePoint]] = {}
            candidate_token_ids: list[str] = []
            # Use the lowest floor for candidate selection (widest net across sides).
            _candidate_floor = min(floor, floor_no) if floor_no is not None else floor
            for bucket in market.buckets:
                p_win = bucket_probability(gaussian, bucket)
                if p_win >= floor or (1 - p_win) >= _candidate_floor:  # candidate on some side
                    histories[bucket.yes_token_id] = fetch_price_history(
                        bucket.yes_token_id, start_ts, end_ts, client
                    )
                    candidate_token_ids.append(bucket.yes_token_id)

            # In trades mode, fetch fills for candidate buckets (both YES and NO tokens).
            fill_histories: dict[str, list[FillPoint]] | None = None
            if ask_source == "trades":
                fill_histories = {}
                for yes_token_id in candidate_token_ids:
                    cond_id = cond_ids.get(yes_token_id)
                    if cond_id:
                        fill_histories[yes_token_id] = fetch_fills(cond_id, yes_token_id, client)
                        for bucket in market.buckets:
                            if bucket.yes_token_id == yes_token_id:
                                fill_histories[bucket.no_token_id] = fetch_fills(
                                    cond_id, bucket.no_token_id, client
                                )
                                break

            market_bets, fills_used = replay_market(
                market,
                forecast_set,
                actual,
                histories,
                settlement_dt,
                leads=leads,
                floor=floor,
                floor_no=floor_no,
                min_sources=min_sources,
                min_sigma=min_sigma,
                min_edge=min_edge,
                spread=spread,
                fill_histories=fill_histories,
                max_edge=max_edge,
                max_p_win=max_p_win,
            )
            bets.extend(market_bets)
            total_n_leads += len(leads)
            total_fills_used += fills_used

    if n_markets == 0:
        return None
    per_lead, overall = score(bets, leads)
    fill_coverage: FillCoverage | None = None
    if ask_source == "trades":
        fill_coverage = FillCoverage(n_leads=total_n_leads, fills_used=total_fills_used)
    return PnlBacktestResult(
        n_markets=n_markets,
        floor=floor,
        floor_no=floor_no,
        min_sources=min_sources,
        min_edge=min_edge,
        spread=spread,
        ask_source=ask_source,
        fill_coverage=fill_coverage,
        max_edge=max_edge,
        max_p_win=max_p_win,
        per_lead=per_lead,
        overall=overall,
    )
