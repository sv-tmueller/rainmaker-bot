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
"""

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


class PnlBacktestResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    n_markets: int
    floor: float
    min_sources: int
    min_edge: float
    spread: float = 0.0
    per_lead: list[LeadPnl]
    overall: LeadPnl


def forecast_set_from_samples(target: Target, samples: list[ForecastSample]) -> ForecastSet:
    """Pool archive samples into a single-source ForecastSet for evaluate_market."""
    return ForecastSet(
        target=target,
        samples=samples,
        coverage=[SourceCoverage(source="open-meteo", ok=True, n_samples=len(samples))],
    )


def market_at_lead(market: Market, mids: dict[str, float | None], *, spread: float = 0.0) -> Market:
    """Reprice a market's buckets from per-bucket mids keyed by bucket label.

    A bucket's YES ask is the mid and its NO ask the complement (1 - mid). With a
    positive `spread` the ask paid is the mid plus half the spread on each side
    (ask = mid + spread/2, capped at 1), the symmetric-book approximation of the
    cost actually paid; spread=0 is the raw mid. A missing or None mid leaves both
    sides unpriced so evaluate_market drops it.
    """
    half = spread / 2
    buckets = []
    for bucket in market.buckets:
        mid = mids.get(bucket.label)
        if mid is None:
            best_ask: float | None = None
            no_ask: float | None = None
        else:
            best_ask = min(mid + half, 1.0)
            no_ask = min((1 - mid) + half, 1.0)
        buckets.append(bucket.model_copy(update={"best_ask": best_ask, "no_ask": no_ask}))
    return market.model_copy(update={"buckets": buckets})


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
) -> list[Bet]:
    """One best-edge bet per lead, settled against the actual.

    At each lead the buckets are repriced from their CLOB mid (snapped to the
    settlement timestamp minus the lead), edge-ranked through the live path, and
    collapsed to the single highest-edge recommended bet. Buckets on one market
    describe the same temperature, so counting more than one would inflate the
    P/L; the collapse mirrors live tracking. A lead with no recommended bet or no
    snappable price contributes nothing.
    """
    settlement_ts = int(settlement_dt.timestamp())
    bets: list[Bet] = []
    for lead in leads:
        target_ts = settlement_ts - lead * SECONDS_PER_DAY
        mids = {
            bucket.label: snap_price(
                histories.get(bucket.yes_token_id, []), target_ts, tolerance_s=SNAP_TOLERANCE_S
            )
            for bucket in market.buckets
        }
        report = evaluate_market(
            market_at_lead(market, mids, spread=spread),
            forecast_set,
            floor=floor,
            min_sources=min_sources,
            min_sigma=min_sigma,
            min_edge=min_edge,
        )
        recommended = [o for o in report.outcomes if o.recommended]
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
    return bets


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
        "| Lead | Bets | W-L | Win rate | Total P/L | ROI | Mean edge |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    lines += [_row(str(lp.lead), lp) for lp in result.per_lead]
    lines.append(_row("ALL", result.overall))
    md = "\n".join(lines) + "\n"
    return md, result.model_dump(mode="json")


def _parse_closed_markets(
    events: list[dict[str, Any]], on_or_after: date, city: str | None
) -> list[tuple[Market, datetime]]:
    """Parsed TMAX markets settling on or after the cutoff, with their settlement time.

    Mirrors backtest_real's parse and filter, keeping the raw endDate so the
    replay can map a lead to a price timestamp. Non-US-city or unmapped markets
    are skipped, as is anything outside the window or city filter.
    """
    out: list[tuple[Market, datetime]] = []
    for ev in events:
        try:
            market = parse_market(ev)
        except (ValueError, KeyError):
            continue
        if market.target.variable != "TMAX" or market.target.local_date < on_or_after:
            continue
        if city not in (None, "all") and market.target.station.city != city:
            continue
        out.append((market, datetime.fromisoformat(ev["endDate"])))
    return out


def backtest_pnl(
    events: list[dict[str, Any]],
    client: httpx.Client,
    *,
    on_or_after: date,
    leads: Sequence[int] = (0, 1, 2, 3),
    floor: float = CONFIDENCE_FLOOR,
    min_sources: int = 1,
    min_sigma: float = MIN_SIGMA_F,
    min_edge: float = MIN_EDGE,
    city: str | None = None,
    spread: float = 0.0,
) -> PnlBacktestResult | None:
    """Replay closed markets at their historical CLOB price and score the P/L.

    Groups parsed markets by station, fetches the archive forecast and NOAA
    actual once per group, then per market fetches the price series for the
    buckets that could clear the confidence floor (on either side) and replays
    each lead. Returns None when nothing scorable remains. min_sources defaults
    to 1 because the archive is a single source; recommended here is therefore a
    superset of the live two-source gate.
    """
    parsed = _parse_closed_markets(events, on_or_after, city)
    if not parsed:
        return None

    by_station: dict[str, list[tuple[Market, datetime]]] = defaultdict(list)
    for market, settlement_dt in parsed:
        by_station[market.target.station.icao].append((market, settlement_dt))

    max_lead = max(leads)
    bets: list[Bet] = []
    n_markets = 0
    for group in by_station.values():
        station = group[0][0].target.station
        dates = [m.target.local_date for m, _ in group]
        samples_by_date = fetch_historical_samples(station, min(dates), max(dates), client)
        actuals = fetch_actuals(station.ghcnd_id, min(dates), max(dates), client, "TMAX")  # type: ignore[arg-type]
        for market, settlement_dt in group:
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
            for bucket in market.buckets:
                p_win = bucket_probability(gaussian, bucket)
                if p_win >= floor or (1 - p_win) >= floor:  # candidate on some side
                    histories[bucket.yes_token_id] = fetch_price_history(
                        bucket.yes_token_id, start_ts, end_ts, client
                    )
            bets.extend(
                replay_market(
                    market,
                    forecast_set,
                    actual,
                    histories,
                    settlement_dt,
                    leads=leads,
                    floor=floor,
                    min_sources=min_sources,
                    min_sigma=min_sigma,
                    min_edge=min_edge,
                    spread=spread,
                )
            )

    if n_markets == 0:
        return None
    per_lead, overall = score(bets, leads)
    return PnlBacktestResult(
        n_markets=n_markets,
        floor=floor,
        min_sources=min_sources,
        min_edge=min_edge,
        spread=spread,
        per_lead=per_lead,
        overall=overall,
    )
