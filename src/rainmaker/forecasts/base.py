from datetime import date, datetime
from typing import Protocol

from pydantic import BaseModel

from rainmaker.config import Target


class ForecastSample(BaseModel):
    source: str
    model: str
    member: int | None
    station: str
    variable: str
    target_date: date
    lead_time_days: int
    value_f: float
    issued_at: datetime | None


class SourceCoverage(BaseModel):
    source: str
    ok: bool
    n_samples: int
    error: str | None = None


class ForecastSet(BaseModel):
    target: Target | None
    samples: list[ForecastSample]
    coverage: list[SourceCoverage]


class ForecastSource(Protocol):
    name: str

    def fetch(self, target: Target) -> list[ForecastSample]: ...
