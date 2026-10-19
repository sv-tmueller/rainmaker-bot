"""Venue-neutral domain types shared across Polymarket, Kalshi, and the core pipeline.

This module owns the geometry types that describe a betting market independent of
which venue hosts it. Venue packages (polymarket/, kalshi/) import these and
supply their own parsers; the probability engine, ranking, and store layers import
from here rather than from a specific venue package.
"""

import re
from datetime import UTC, date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

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
    # The market's own probability estimate (last trade or bid/ask mid). None on
    # Kalshi when no quote component exists at all: fabricating 0.0 would poison
    # the NO row's implied_prob (1 - 0.0 = 1.0) downstream (#398).
    yes_price: float | None
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
# Canonical lead (#460, implements #438)
# ---------------------------------------------------------------------------


def canonical_lead(target: Target, now: datetime) -> int:
    """THE lead ruler: station-local calendar-day difference to the target.

    Lead is how many local calendar days away the target settles, the quantity
    the markets settle on. Every call site that tags or gates on lead derives
    from here: the cli gate/calibration load (anchored at the run instant), NWS
    sample tagging (anchored at issuance), and Open-Meteo sample tagging
    (anchored at fetch). The rulers they replaced agreed outside the 00:00-06:00
    UTC cron window and diverged inside it, because UTC-today runs one day
    ahead of station-local today for every US station; a naive UTC ruler then
    both understated tomorrow's lead and marked today's still-in-play local
    day as settled. `now` must be timezone-aware; a naive instant would be read
    in the machine's local zone and silently pick the wrong calendar day.
    """
    if now.tzinfo is None:
        raise ValueError(f"canonical_lead needs an aware instant, got naive: {now!r}")
    local_today = now.astimezone(ZoneInfo(target.station.timezone)).date()
    return (target.local_date - local_today).days


# ---------------------------------------------------------------------------
# Precipitation-bracket types
# ---------------------------------------------------------------------------

# Monthly precip resolution rule, confirmed verbatim from both Gamma market
# descriptions. The deferred settlement task reads these; settling logic is not
# part of this build.
SETTLEMENT_DECIMALS = 2  # the NOAA monthly figure is reported to 2 decimals


# ---------------------------------------------------------------------------
# Stored-timestamp parsing (#429)
# ---------------------------------------------------------------------------


def _stored_timestamp(stamp: str) -> datetime:
    """Parse a stored ISO-8601 timestamp (runs.started_at and kin) to a UTC datetime.

    The store writes ISO-8601 UTC text on both backends, but a stamp that
    arrives carrying a non-UTC offset must be normalized before any day/hour
    judgment: slicing the raw text (stamp[:10], stamp[11:13]) silently used
    the local calendar day and clock hour, which disagree with UTC whenever
    the offset crosses a midnight boundary (23:00 at -05:00 is 04:00 the
    next UTC day). Naive stamps are read as UTC, matching the store's
    convention. Raises ValueError on anything unparsable so callers' existing
    skip-on-unparsable branches keep firing instead of forming garbage keys.
    """
    dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def utc_day(stamp: str) -> date:
    """The UTC calendar day of a stored ISO timestamp."""
    return _stored_timestamp(stamp).date()


def utc_hour(stamp: str) -> int:
    """The UTC clock hour (0-23) of a stored ISO timestamp."""
    return _stored_timestamp(stamp).hour


def day_key(stamp: str) -> str:
    """Tolerant UTC-day grouping key: the ISO date when the stamp parses, else
    the raw stamp unchanged.

    Pure grouping/dedup keys (prune day buckets, per-(market, day) collapses,
    the snapshot as-of comparison) must tolerate a malformed stamp the way the
    old [:10] slice did -- grouping it under its raw text -- instead of raising
    and killing the whole pass. Day/hour *judgments* (lead times, hour splits)
    should use utc_day/utc_hour, which raise on the same input so callers'
    skip branches fire.
    """
    try:
        return utc_day(stamp).isoformat()
    except ValueError:
        return stamp


def hour_key(stamp: str) -> str:
    """Tolerant UTC-hour grouping key: str(hour) when the stamp parses, else
    the raw [11:13] slice ("" for short/malformed stamps), matching legacy
    slicing instead of raising. See day_key for when to use the tolerant keys.
    """
    try:
        return str(utc_hour(stamp))
    except ValueError:
        return stamp[11:13]


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
    # Same None-not-zero rule as Bucket.yes_price (#398).
    yes_price: float | None
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
