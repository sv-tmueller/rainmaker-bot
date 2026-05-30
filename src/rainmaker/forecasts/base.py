from datetime import date
from typing import Protocol

from pydantic import AwareDatetime, BaseModel

from rainmaker.config import Target, Variable


class ForecastSample(BaseModel):
    source: str
    model: str
    member: int | None
    station: str
    variable: Variable
    target_date: date
    lead_time_days: int
    value_f: float
    issued_at: AwareDatetime | None


class SourceCoverage(BaseModel):
    source: str
    ok: bool
    n_samples: int
    error: str | None = None


class ForecastSet(BaseModel):
    target: Target
    samples: list[ForecastSample]
    coverage: list[SourceCoverage]


class ForecastSource(Protocol):
    name: str

    def fetch(self, target: Target) -> list[ForecastSample]: ...
