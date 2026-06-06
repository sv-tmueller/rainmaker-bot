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

from collections.abc import Sequence
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from rainmaker.config import Target
from rainmaker.forecasts.base import ForecastSample, ForecastSet, SourceCoverage
from rainmaker.polymarket.markets import Market, parse_bucket_label
from rainmaker.polymarket.prices import PricePoint, snap_price
from rainmaker.probability.outcomes import settles
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
    per_lead: list[LeadPnl]
    overall: LeadPnl


def forecast_set_from_samples(target: Target, samples: list[ForecastSample]) -> ForecastSet:
    """Pool archive samples into a single-source ForecastSet for evaluate_market."""
    return ForecastSet(
        target=target,
        samples=samples,
        coverage=[SourceCoverage(source="open-meteo", ok=True, n_samples=len(samples))],
    )


def market_at_lead(market: Market, mids: dict[str, float | None]) -> Market:
    """Reprice a market's buckets from per-bucket mids keyed by bucket label.

    A bucket's YES ask becomes the mid and its NO ask the complement (1 - mid).
    A missing or None mid leaves both sides unpriced so evaluate_market drops it.
    """
    buckets = []
    for bucket in market.buckets:
        mid = mids.get(bucket.label)
        buckets.append(
            bucket.model_copy(update={"best_ask": mid, "no_ask": None if mid is None else 1 - mid})
        )
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
            market_at_lead(market, mids),
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
