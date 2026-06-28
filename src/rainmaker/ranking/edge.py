import math
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from rainmaker.domain import Market, PrecipMonthlyMarket
from rainmaker.forecasts.base import ForecastSample, ForecastSet, SourceCoverage
from rainmaker.forecasts.precip import PrecipForecastSet
from rainmaker.probability.calibration import Calibration, apply_calibration
from rainmaker.probability.distribution import fit_gaussian
from rainmaker.probability.outcomes import bucket_probability
from rainmaker.probability.precip_distribution import fit_gamma
from rainmaker.probability.precip_outcomes import bracket_probability


def _f_to_c(sample: ForecastSample) -> ForecastSample:
    """Return a copy of the sample with value_f converted from F to C."""
    return sample.model_copy(update={"value_f": (sample.value_f - 32) * 5 / 9})


class RankedOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    bucket_label: str
    side: Literal["YES", "NO"] = "YES"
    p_win: float
    best_ask: float  # price paid: the YES ask for a YES bet, the NO ask for a NO bet
    edge: float
    recommended: bool


class MarketReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    market_id: str
    title: str
    city: str
    station: str
    variable: str
    unit: str = "F"  # settlement unit of the market ("F" or "C")
    settlement_date: date
    mu: float | None
    sigma: float | None
    n_sources: int
    calibrated: Literal["uncalibrated", "bias_only", "full"]
    coverage: list[SourceCoverage]
    outcomes: list[RankedOutcome]
    excluded_no_ask: list[str]
    venue: str = "polymarket"


def evaluate_market(
    market: Market,
    forecast_set: ForecastSet,
    *,
    floor: float,
    min_sources: int,
    min_sigma: float,
    min_edge: float,
    floor_no: float | None = None,
    calibration: Calibration | None = None,
) -> MarketReport:
    """Edge-rank a temperature market.

    floor applies to YES bets. floor_no applies to NO bets; when None it
    falls back to floor (flat behaviour, preserving all existing call sites).
    """
    no_floor = floor_no if floor_no is not None else floor
    unit = market.target.station.unit
    n_sources = sum(1 for c in forecast_set.coverage if c.ok and c.n_samples > 0)
    # Markets with no GHCN-D id cannot be calibrated (no actuals history to fit against).
    # Force recommended off while leaving the forecast and advisory display intact.
    uncalibratable = market.target.station.ghcnd_id is None
    common: dict[str, Any] = dict(
        market_id=market.id,
        title=market.title,
        city=market.target.station.city,
        station=market.target.station.icao,
        variable=market.target.variable,
        unit=unit,
        settlement_date=market.target.local_date,
        n_sources=n_sources,
        coverage=forecast_set.coverage,
        venue=market.venue,
    )
    if not forecast_set.samples:
        return MarketReport(
            **common,
            calibrated="uncalibrated",
            mu=None,
            sigma=None,
            outcomes=[],
            excluded_no_ask=[],
        )

    # Forecast sources always produce F values. For C markets, convert to C so
    # the fitted Gaussian lives in the same unit as the bucket edges.
    samples = [_f_to_c(s) for s in forecast_set.samples] if unit == "C" else forecast_set.samples
    gaussian = fit_gaussian(samples, min_sigma=min_sigma)
    # Apply calibration only when a cell is provided; with none, use the raw fit.
    calibrated: Literal["uncalibrated", "bias_only", "full"] = "uncalibrated"
    if calibration is not None:
        gaussian, calibrated = apply_calibration(gaussian, calibration, min_sigma=min_sigma)
    outcomes: list[RankedOutcome] = []
    excluded: list[str] = []
    for bucket in market.buckets:
        p_win = bucket_probability(gaussian, bucket)
        # YES side: priced off the YES ask. recommended gates are confidence floor
        # + min sources + minimum edge. The edge threshold keeps near-worthless
        # bets (pay 0.99 to win 0.01) out of the recommendations.
        if bucket.best_ask is not None and bucket.best_ask > 0:
            edge = p_win - bucket.best_ask
            recommended = (
                not uncalibratable
                and p_win >= floor
                and n_sources >= min_sources
                and edge >= min_edge
            )
            outcomes.append(
                RankedOutcome(
                    bucket_label=bucket.label,
                    side="YES",
                    p_win=p_win,
                    best_ask=bucket.best_ask,
                    edge=edge,
                    recommended=recommended,
                )
            )
        else:
            excluded.append(bucket.label)
        # NO side is independent of the YES ask: it is priced off the YES bid
        # (no_ask = 1 - yes_bid), absent only when there is no YES bid to take.
        if bucket.no_ask is not None and 0 < bucket.no_ask < 1:
            p_no = 1 - p_win
            edge_no = p_no - bucket.no_ask
            recommended_no = (
                not uncalibratable
                and p_no >= no_floor
                and n_sources >= min_sources
                and edge_no >= min_edge
            )
            outcomes.append(
                RankedOutcome(
                    bucket_label=bucket.label,
                    side="NO",
                    p_win=p_no,
                    best_ask=bucket.no_ask,
                    edge=edge_no,
                    recommended=recommended_no,
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


def evaluate_precip_market(
    market: PrecipMonthlyMarket,
    forecast_set: PrecipForecastSet,
    *,
    floor: float,
    min_sources: int,
    min_edge: float,
    var_floor: float,
    floor_no: float | None = None,
) -> MarketReport:
    """Edge-rank a monthly precipitation market via the gamma over inch brackets.

    The parallel of evaluate_market for the precip path: same YES/NO gates
    (confidence floor, min sources, min edge), same MarketReport, but the
    distribution is a method-of-moments gamma and calibration is not applied.

    floor applies to YES bets. floor_no applies to NO bets; when None it
    falls back to floor (flat behaviour, preserving all existing call sites).
    """
    no_floor = floor_no if floor_no is not None else floor
    n_sources = sum(1 for c in forecast_set.coverage if c.ok and c.n_samples > 0)
    gamma = fit_gamma(forecast_set.mean, forecast_set.var, floor=var_floor)
    outcomes: list[RankedOutcome] = []
    excluded: list[str] = []
    for bracket in market.buckets:
        p_win = bracket_probability(gamma, bracket)
        if bracket.best_ask is not None and bracket.best_ask > 0:
            edge = p_win - bracket.best_ask
            recommended = p_win >= floor and n_sources >= min_sources and edge >= min_edge
            outcomes.append(
                RankedOutcome(
                    bucket_label=bracket.label,
                    side="YES",
                    p_win=p_win,
                    best_ask=bracket.best_ask,
                    edge=edge,
                    recommended=recommended,
                )
            )
        else:
            excluded.append(bracket.label)
        if bracket.no_ask is not None and 0 < bracket.no_ask < 1:
            p_no = 1 - p_win
            edge_no = p_no - bracket.no_ask
            recommended_no = p_no >= no_floor and n_sources >= min_sources and edge_no >= min_edge
            outcomes.append(
                RankedOutcome(
                    bucket_label=bracket.label,
                    side="NO",
                    p_win=p_no,
                    best_ask=bracket.no_ask,
                    edge=edge_no,
                    recommended=recommended_no,
                )
            )
    outcomes.sort(key=lambda o: o.edge, reverse=True)
    return MarketReport(
        market_id=market.id,
        title=market.title,
        city=market.target.station.city,
        station=market.target.station.resolution_name,
        variable=market.target.variable,
        settlement_date=market.target.settlement_date,
        mu=forecast_set.mean,
        sigma=math.sqrt(forecast_set.var),
        n_sources=n_sources,
        calibrated="uncalibrated",
        coverage=forecast_set.coverage,
        outcomes=outcomes,
        excluded_no_ask=excluded,
        venue=market.venue,
    )
