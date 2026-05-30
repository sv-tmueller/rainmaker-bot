from datetime import UTC, datetime, timedelta

from rainmaker.config import FRESHNESS_LIMIT_HOURS, Target
from rainmaker.forecasts.base import ForecastSample, ForecastSet, ForecastSource, SourceCoverage


def _is_fresh(sample: ForecastSample, now: datetime, limit_hours: int) -> bool:
    # None issued_at means the source does not publish a run time (e.g. Open-Meteo).
    # Treat as fresh: we have no evidence it is stale.
    if sample.issued_at is None:
        return True
    return (now - sample.issued_at) <= timedelta(hours=limit_hours)


def aggregate(
    target: Target,
    sources: list[ForecastSource],
    now: datetime | None = None,
    freshness_limit_hours: int = FRESHNESS_LIMIT_HOURS,
) -> ForecastSet:
    now = now or datetime.now(UTC)
    samples: list[ForecastSample] = []
    coverage: list[SourceCoverage] = []
    for source in sources:
        try:
            fetched = source.fetch(target)
        except Exception as exc:  # noqa: BLE001 - one source failing must not abort the run
            coverage.append(
                SourceCoverage(source=source.name, ok=False, n_samples=0, error=str(exc))
            )
            continue
        fresh = [s for s in fetched if _is_fresh(s, now, freshness_limit_hours)]
        samples.extend(fresh)
        coverage.append(SourceCoverage(source=source.name, ok=True, n_samples=len(fresh)))
    return ForecastSet(target=target, samples=samples, coverage=coverage)
