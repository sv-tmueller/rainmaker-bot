"""Venue-neutral domain types shared across Polymarket, Kalshi, and the core pipeline.

This module owns the geometry types that describe a betting market independent of
which venue hosts it. Venue packages (polymarket/, kalshi/) import these and
supply their own parsers; the probability engine, ranking, and store layers import
from here rather than from a specific venue package.
"""

import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict

from rainmaker.config import PrecipStation, Target, Variable

# ---------------------------------------------------------------------------
# Temperature-bucket types
# ---------------------------------------------------------------------------

BucketKind = Literal["below", "range", "above"]

# Two distinct patterns: temperature buckets use integers and allow negatives;
# precip brackets use floats and are always non-negative.
_TEMP_RANGE_RE = re.compile(r"(-?\d+)\s*-\s*(-?\d+)")
_TEMP_THRESHOLD_RE = re.compile(r"(-?\d+)")

_PRECIP_RANGE_RE = re.compile(r"([\d.]+)\s*-\s*([\d.]+)")
_PRECIP_NUM_RE = re.compile(r"([\d.]+)")


def parse_bucket_label(label: str) -> tuple[BucketKind, int | None, int | None, int | None]:
    """Parse a temperature-bucket title into (kind, lo, hi, threshold).

    "59°F or below"  -> ("below", None, None, 59)
    "70-71°F"        -> ("range", 70, 71, None)
    "78°F or higher" -> ("above", None, None, 78)
    "-10--5°F"       -> ("range", -10, -5, None)
    "15°C"           -> ("range", 15, 15, None)  single-degree Celsius bucket
    """
    lowered = label.lower()
    if "below" in lowered:
        match = _TEMP_THRESHOLD_RE.search(label)
        if match is None:
            raise ValueError(f"no threshold in below-bucket label: {label!r}")
        return ("below", None, None, int(match.group(1)))
    if "higher" in lowered or "above" in lowered:
        match = _TEMP_THRESHOLD_RE.search(label)
        if match is None:
            raise ValueError(f"no threshold in above-bucket label: {label!r}")
        return ("above", None, None, int(match.group(1)))
    match = _TEMP_RANGE_RE.search(label)
    if match is not None:
        lo, hi = int(match.group(1)), int(match.group(2))
        if lo >= hi:
            raise ValueError(f"inverted range bucket label: {label!r}")
        return ("range", lo, hi, None)
    # Single-degree Celsius bucket: "15°C" -> range with lo=hi=15.
    # Polymarket intl markets use one bucket per whole degree.
    single = _TEMP_THRESHOLD_RE.search(label)
    if single is not None:
        v = int(single.group(1))
        return ("range", v, v, None)
    raise ValueError(f"unrecognized bucket label: {label!r}")


class Bucket(BaseModel):
    model_config = ConfigDict(frozen=True)

    label: str
    kind: BucketKind
    lo: int | None
    hi: int | None
    threshold: int | None
    yes_token_id: str
    best_ask: float | None
    best_bid: float | None
    yes_price: float
    # NO side. Gamma exposes only the YES book, so the NO ask is the complement of
    # the YES bid (buying NO == selling YES). None when there is no YES bid to take.
    no_token_id: str = ""
    no_ask: float | None = None


class Market(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    slug: str
    title: str
    target: Target
    buckets: list[Bucket]
    venue: str = "polymarket"  # "kalshi" when discovered from Kalshi


# ---------------------------------------------------------------------------
# Precipitation-bracket types
# ---------------------------------------------------------------------------

# Monthly precip resolution rule, confirmed verbatim from both Gamma market
# descriptions. The deferred settlement task reads these; settling logic is not
# part of this build.
SETTLEMENT_DECIMALS = 2  # the NOAA monthly figure is reported to 2 decimals


def parse_precip_bracket_label(
    label: str,
) -> tuple[BucketKind, float | None, float | None, float | None]:
    """Parse a precip bracket title into (kind, lo, hi, threshold) with inch bounds.

    '<2"'    -> ("below", None, None, 2.0)   open-low tail
    '2-3"'   -> ("range", 2.0, 3.0, None)    interior range
    '>6"'    -> ("above", None, None, 6.0)   open-high tail
    """
    s = label.strip()
    if s.startswith("<"):
        match = _PRECIP_NUM_RE.search(s)
        if match is None:
            raise ValueError(f"no threshold in open-low precip label: {label!r}")
        return ("below", None, None, float(match.group(1)))
    if s.startswith(">"):
        match = _PRECIP_NUM_RE.search(s)
        if match is None:
            raise ValueError(f"no threshold in open-high precip label: {label!r}")
        return ("above", None, None, float(match.group(1)))
    match = _PRECIP_RANGE_RE.search(s)
    if match is None:
        raise ValueError(f"unrecognized precip bracket label: {label!r}")
    lo, hi = float(match.group(1)), float(match.group(2))
    if lo >= hi:
        raise ValueError(f"inverted range precip bracket label: {label!r}")
    return ("range", lo, hi, None)


class PrecipBracket(BaseModel):
    model_config = ConfigDict(frozen=True)

    label: str
    kind: BucketKind
    lo: float | None
    hi: float | None
    threshold: float | None
    yes_token_id: str
    best_ask: float | None
    best_bid: float | None
    yes_price: float
    # NO side, derived exactly as in markets.py: Gamma exposes only the YES book,
    # so the NO ask is the complement of the YES bid (buying NO == selling YES).
    no_token_id: str = ""
    no_ask: float | None = None


class PrecipTarget(BaseModel):
    model_config = ConfigDict(frozen=True)

    station: PrecipStation
    variable: Variable
    year: int
    month: int
    settlement_date: date  # last calendar day of the month


class PrecipMonthlyMarket(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    slug: str
    title: str
    target: PrecipTarget
    buckets: list[PrecipBracket]
    venue: str = "polymarket"  # "kalshi" when discovered from Kalshi
