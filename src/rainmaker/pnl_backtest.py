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

from rainmaker.config import Target
from rainmaker.forecasts.base import ForecastSample, ForecastSet, SourceCoverage
from rainmaker.polymarket.markets import Market


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
