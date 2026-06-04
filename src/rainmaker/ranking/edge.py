from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict

from rainmaker.forecasts.base import ForecastSet, SourceCoverage
from rainmaker.polymarket.markets import Market
from rainmaker.probability.calibration import Calibration, apply_calibration
from rainmaker.probability.distribution import fit_gaussian
from rainmaker.probability.outcomes import bucket_probability


class RankedOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    bucket_label: str
    p_win: float
    best_ask: float
    edge: float
    recommended: bool


class MarketReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    market_id: str
    title: str
    station: str
    variable: str
    settlement_date: date
    mu: float | None
    sigma: float | None
    n_sources: int
    calibrated: bool
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
    min_edge: float,
    calibration: Calibration | None = None,
) -> MarketReport:
    n_sources = sum(1 for c in forecast_set.coverage if c.ok)
    common: dict[str, Any] = dict(
        market_id=market.id,
        title=market.title,
        station=market.target.station.icao,
        variable=market.target.variable,
        settlement_date=market.target.local_date,
        n_sources=n_sources,
        coverage=forecast_set.coverage,
    )
    if not forecast_set.samples:
        return MarketReport(
            **common, calibrated=False, mu=None, sigma=None, outcomes=[], excluded_no_ask=[]
        )

    gaussian = fit_gaussian(forecast_set.samples, min_sigma=min_sigma)
    # Apply calibration only when a cell is provided; with none, use the raw fit.
    calibrated = False
    if calibration is not None:
        gaussian, calibrated = apply_calibration(gaussian, calibration, min_sigma=min_sigma)
    outcomes: list[RankedOutcome] = []
    excluded: list[str] = []
    for bucket in market.buckets:
        if bucket.best_ask is None or bucket.best_ask <= 0:
            excluded.append(bucket.label)
            continue
        p_win = bucket_probability(gaussian, bucket)
        edge = p_win - bucket.best_ask
        # recommended gates: confidence floor + min sources + minimum edge.
        # The edge threshold keeps near-worthless bets (pay 0.99 to win 0.01)
        # out of the recommendations.
        recommended = p_win >= floor and n_sources >= min_sources and edge >= min_edge
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
    return MarketReport(
        **common,
        calibrated=calibrated,
        mu=gaussian.mu,
        sigma=gaussian.sigma,
        outcomes=outcomes,
        excluded_no_ask=excluded,
    )
