from datetime import date

from pydantic import BaseModel

from rainmaker.forecasts.base import ForecastSet, SourceCoverage
from rainmaker.polymarket.markets import Market
from rainmaker.probability.distribution import fit_gaussian
from rainmaker.probability.outcomes import bucket_probability


class RankedOutcome(BaseModel):
    bucket_label: str
    p_win: float
    best_ask: float
    edge: float
    recommended: bool


class MarketReport(BaseModel):
    market_id: str
    title: str
    station: str
    variable: str
    settlement_date: date
    mu: float | None
    sigma: float | None
    n_sources: int
    coverage: list[SourceCoverage]
    outcomes: list[RankedOutcome]
    excluded_no_ask: list[str]


def evaluate_market(
    market: Market,
    forecast_set: ForecastSet,
    *,
    floor: float,
    min_sources: int,
    min_sigma: float,
) -> MarketReport:
    n_sources = sum(1 for c in forecast_set.coverage if c.ok)
    base = MarketReport(
        market_id=market.id,
        title=market.title,
        station=market.target.station.icao,
        variable=market.target.variable,
        settlement_date=market.target.local_date,
        mu=None,
        sigma=None,
        n_sources=n_sources,
        coverage=forecast_set.coverage,
        outcomes=[],
        excluded_no_ask=[],
    )
    if not forecast_set.samples:
        return base

    gaussian = fit_gaussian(forecast_set.samples, min_sigma=min_sigma)
    outcomes: list[RankedOutcome] = []
    excluded: list[str] = []
    for bucket in market.buckets:
        if bucket.best_ask is None or bucket.best_ask <= 0:
            excluded.append(bucket.label)
            continue
        p_win = bucket_probability(gaussian, bucket)
        edge = p_win - bucket.best_ask
        recommended = p_win >= floor and n_sources >= min_sources and edge > 0
        outcomes.append(
            RankedOutcome(
                bucket_label=bucket.label,
                p_win=p_win,
                best_ask=bucket.best_ask,
                edge=edge,
                recommended=recommended,
            )
        )
    outcomes.sort(key=lambda o: o.edge, reverse=True)
    return base.model_copy(
        update={
            "mu": gaussian.mu,
            "sigma": gaussian.sigma,
            "outcomes": outcomes,
            "excluded_no_ask": excluded,
        }
    )
