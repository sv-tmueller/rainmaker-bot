"""Settlement-divergence spike (#101a).

Measures how often the NCEI GHCND daily extreme (our current source) falls in
the same bucket that Polymarket resolved, versus how often the ASOS daily
extreme from the Iowa State Mesonet ASOS API falls in that bucket.

Ground truth: for closed Polymarket temperature events the resolved bucket is
the market whose outcomePrices YES side == 1.0.

Arm A: NCEI GHCND daily extreme -> bucket -> match?
Arm B: Iowa State Mesonet ASOS hourly tmpc -> daily extreme -> bucket -> match?

Note: the sub-plan named NCEI ISD hourly as the Arm B source. In practice NCEI
ISD has a ~10-month data lag, so no ISD data is available for 2025/2026 closed
events. Iowa State Mesonet serves the same ASOS observations with minimal lag
and is used as Arm B instead. The NCEI ISD fetcher (fetch_isd_actuals) is
retained for reference and future use.

The result is a list of DivergenceRow, one per resolved market.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

from rainmaker.config import STATIONS, Variable
from rainmaker.domain import BucketKind, parse_bucket_label

# NCEI ISD (global-hourly) endpoint - same base as daily-summaries
NCEI_ISD_URL = "https://www.ncei.noaa.gov/access/services/data/v1"

# Iowa State Mesonet ASOS API - near-real-time hourly ASOS observations
MESONET_ASOS_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"

# Bad ISD quality flags: suspend or erroneous readings
_BAD_ISD_FLAGS = frozenset("1 2 6 A B C D I".split())

# Missing-value sentinel in ISD TMP: +9999 tenths of C
_ISD_MISSING_TMP = 9999

# Map ICAO station id (as stored on Station) to ASOS 3-letter code used by Iowa State Mesonet.
# The Mesonet uses FAA 3-letter codes (without the K prefix for US stations).
ICAO_TO_ASOS_STATION: dict[str, str] = {
    "KLGA": "LGA",  # NYC LaGuardia
    "KMIA": "MIA",  # Miami Intl
    "KORD": "ORD",  # Chicago O'Hare
    "KDAL": "DAL",  # Dallas Love Field
    "KHOU": "HOU",  # Houston Hobby
    "KLAX": "LAX",  # Los Angeles Intl
    "KSFO": "SFO",  # San Francisco Intl
    "KSEA": "SEA",  # Seattle-Tacoma Intl
    "KAUS": "AUS",  # Austin-Bergstrom Intl
    "KATL": "ATL",  # Atlanta Hartsfield-Jackson
    "KBKF": "BKF",  # Denver / Buckley Space Force Base
}


# ---------------------------------------------------------------------------
# GHCND -> ISD station mapping
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GhcndToIsdMapping:
    """Maps GHCND station id to ISD 11-character station id.

    ISD station ids used by the NCEI global-hourly API are 11-char strings
    formed by concatenating the 6-digit USAF code and 5-digit WBAN code
    (e.g., "72503014732" for LaGuardia). The WBAN is the trailing 5 digits of
    the GHCND USW0000XXXXX id. The USAF was sourced from NCEI isd-history.csv.
    """

    data: dict[str, str]  # ghcnd_id -> isd_11char

    @classmethod
    def default(cls) -> GhcndToIsdMapping:
        """Hardcoded mapping for all 11 Polymarket US cities.

        Verified against NCEI ISD station list. The WBAN matches the trailing
        5 digits of each GHCND id (the USW-prefixed ids encode the WBAN). The
        USAF id was confirmed via https://www.ncei.noaa.gov/pub/data/noaa/isd-history.csv
        for each airport/station. Station IDs are 11-char (USAF+WBAN, no dash)
        as required by the NCEI global-hourly API endpoint.
        """
        return cls(
            data={
                # NYC / LaGuardia Airport  GHCND=USW00014732  USAF=725030  WBAN=14732
                "USW00014732": "72503014732",
                # Miami Intl Airport  GHCND=USW00012839  USAF=722020  WBAN=12839
                "USW00012839": "72202012839",
                # Chicago O'Hare Intl Airport  GHCND=USW00094846  USAF=725300  WBAN=94846
                "USW00094846": "72530094846",
                # Dallas Love Field  GHCND=USW00013960  USAF=722583  WBAN=13960
                "USW00013960": "72258313960",
                # Houston Hobby Airport  GHCND=USW00012918  USAF=722435  WBAN=12918
                "USW00012918": "72243512918",
                # Los Angeles Intl Airport  GHCND=USW00023174  USAF=722950  WBAN=23174
                "USW00023174": "72295023174",
                # San Francisco Intl Airport  GHCND=USW00023234  USAF=724940  WBAN=23234
                "USW00023234": "72494023234",
                # Seattle-Tacoma Intl Airport  GHCND=USW00024233  USAF=727930  WBAN=24233
                "USW00024233": "72793024233",
                # Austin-Bergstrom Intl Airport  GHCND=USW00013904  USAF=722540  WBAN=13904
                "USW00013904": "72254013904",
                # Atlanta Hartsfield-Jackson  GHCND=USW00013874  USAF=722190  WBAN=13874
                "USW00013874": "72219013874",
                # Denver / Buckley SFB  GHCND=USW00023036  USAF=724695  WBAN=23036
                "USW00023036": "72469523036",
            }
        )


def isd_station_for(ghcnd_id: str, mapping: GhcndToIsdMapping) -> str | None:
    """Return the ISD USAF-WBAN id for a GHCND station id, or None if unmapped."""
    return mapping.data.get(ghcnd_id)


# ---------------------------------------------------------------------------
# Resolved-bucket detection
# ---------------------------------------------------------------------------


def resolved_bucket_label(markets: list[dict[str, Any]]) -> str | None:
    """Return the label of the market whose YES outcomePrices == 1.0, or None.

    Polymarket sets outcomePrices to ["1", "0"] (or ["1.0", "0.0"]) on the
    winning bucket when an event resolves. The first element is the YES price.
    """
    for m in markets:
        try:
            prices = json.loads(m["outcomePrices"])
            yes_price = float(prices[0])
        except (KeyError, IndexError, ValueError, TypeError):
            continue
        if yes_price == 1.0:
            return str(m["groupItemTitle"])
    return None


# ---------------------------------------------------------------------------
# Bucket membership test
# ---------------------------------------------------------------------------


def temperature_in_bucket(value: float, bucket_label: str) -> bool:
    """Return True if value (degrees F) settles in the named bucket.

    Uses the same rounding rule as the live settle path: round() (banker's
    rounding) to whole degrees F, then compare.
    """
    kind: BucketKind
    lo: int | None
    hi: int | None
    threshold: int | None
    kind, lo, hi, threshold = parse_bucket_label(bucket_label)
    v = round(value)
    if kind == "below":
        assert threshold is not None
        return v <= threshold
    if kind == "above":
        assert threshold is not None
        return v >= threshold
    assert lo is not None and hi is not None
    return lo <= v <= hi


# ---------------------------------------------------------------------------
# ISD hourly fetch -> daily extreme
# ---------------------------------------------------------------------------


def _parse_isd_tmp(tmp_str: str) -> float | None:
    """Parse an ISD TMP field "TTTT,Q" into degrees Fahrenheit, or None.

    TTTT is the temperature as a signed integer in tenths of Celsius.
    Q is the quality flag; bad flags are excluded.
    The missing-value sentinel (+9999) is also excluded.
    """
    if not tmp_str or "," not in tmp_str:
        return None
    parts = tmp_str.split(",", 1)
    if len(parts) != 2:
        return None
    tmp_raw_str, quality = parts[0].strip(), parts[1].strip()
    if quality in _BAD_ISD_FLAGS:
        return None
    try:
        tmp_tenths = int(tmp_raw_str)
    except ValueError:
        return None
    if tmp_tenths == _ISD_MISSING_TMP or tmp_tenths == -_ISD_MISSING_TMP:
        return None
    celsius = tmp_tenths / 10.0
    return celsius * 9.0 / 5.0 + 32.0


def fetch_isd_actuals(
    isd_station: str,
    start: date,
    end: date,
    client: httpx.Client,
    variable: str = "TMAX",
) -> dict[date, float]:
    """Daily TMAX or TMIN (degrees F) from NCEI ISD hourly.

    Fetches the global-hourly dataset for the given ISD USAF-WBAN station,
    parses TMP readings, applies quality control, and reduces each UTC day
    to its maximum (TMAX) or minimum (TMIN). Raises on HTTP error.

    Note: ISD timestamps are UTC. For US cities this may slightly misalign
    with local-day extremes (e.g., a midnight local reading appears on the
    previous UTC day). The error is bounded to the first/last ~8 hours of
    the day, same order as the GHCND aggregation window difference.
    """
    resp = client.get(
        NCEI_ISD_URL,
        params={
            "dataset": "global-hourly",
            "stations": isd_station,
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "dataTypes": "TMP",
            "format": "json",
        },
    )
    resp.raise_for_status()
    body: dict[str, Any] = resp.json()
    rows: list[dict[str, Any]] = body.get("results", body) if isinstance(body, dict) else body

    reduce = max if variable == "TMAX" else min
    by_day: dict[date, list[float]] = {}
    for row in rows:
        date_str = row.get("DATE", "")
        if not date_str:
            continue
        # DATE is "YYYY-MM-DDTHH:MM:SS" (UTC)
        try:
            day = date.fromisoformat(date_str[:10])
        except ValueError:
            continue
        tmp_str = row.get("TMP")
        if not tmp_str:
            continue
        value_f = _parse_isd_tmp(str(tmp_str))
        if value_f is None:
            continue
        by_day.setdefault(day, []).append(value_f)

    return {day: reduce(readings) for day, readings in by_day.items() if readings}


# ---------------------------------------------------------------------------
# Iowa State Mesonet ASOS fetch -> daily extreme
# ---------------------------------------------------------------------------


def fetch_asos_actuals_mesonet(
    asos_station: str,
    start: date,
    end: date,
    client: httpx.Client,
    variable: str = "TMAX",
) -> dict[date, float]:
    """Daily TMAX or TMIN (degrees F) from Iowa State Mesonet ASOS.

    The Mesonet returns CSV with columns: station, valid (UTC timestamp), tmpc.
    Lines starting with '#' are debug/comment lines and are skipped.
    Missing values are reported as 'M' and are excluded.

    Note: timestamps are UTC. The same UTC-vs-local caveat as fetch_isd_actuals
    applies here: readings from the first/last ~8 hours may belong to the
    adjacent local day.
    """
    resp = client.get(
        MESONET_ASOS_URL,
        params={
            "station": asos_station,
            "data": "tmpc",
            "year1": start.year,
            "month1": start.month,
            "day1": start.day,
            "hour1": 0,
            "minute1": 0,
            "year2": end.year,
            "month2": end.month,
            "day2": end.day,
            "hour2": 23,
            "minute2": 59,
            "tz": "UTC",
            "format": "onlycomma",
            "latlon": "no",
            "elev": "no",
            "report_type": "3",  # routine hourly METAR only (excludes specials)
        },
    )
    resp.raise_for_status()

    reduce = max if variable == "TMAX" else min
    by_day: dict[date, list[float]] = {}

    reader = csv.DictReader(
        line for line in io.StringIO(resp.text).readlines() if not line.startswith("#")
    )
    for row in reader:
        valid_str = row.get("valid", "")
        tmpc_str = row.get("tmpc", "M")
        if not valid_str or tmpc_str in ("M", ""):
            continue
        try:
            # valid is "YYYY-MM-DD HH:MM" (UTC)
            day = date.fromisoformat(valid_str[:10])
            celsius = float(tmpc_str)
        except ValueError:
            continue
        fahrenheit = celsius * 9.0 / 5.0 + 32.0
        by_day.setdefault(day, []).append(fahrenheit)

    return {day: reduce(readings) for day, readings in by_day.items() if readings}


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class DivergenceRow:
    city: str
    local_date: date
    variable: Variable
    resolved_label: str  # the market label that resolved to YES=1
    # Arm A (NCEI GHCND)
    ncei_value: float | None  # daily extreme in degrees F; None if not available
    ncei_in_bucket: bool | None  # None when ncei_value is None
    # Arm B (ASOS/ISD)
    asos_value: float | None  # daily extreme in degrees F; None if not available
    asos_in_bucket: bool | None  # None when asos_value is None
    # Signed gap: actual minus bucket midpoint (positive = we over-read)
    ncei_gap: float | None  # degrees F gap from bucket edge when ncei_in_bucket is False
    asos_gap: float | None  # degrees F gap from bucket edge when asos_in_bucket is False


# ---------------------------------------------------------------------------
# Per-city flip-rate summary
# ---------------------------------------------------------------------------


@dataclass
class CityResult:
    city: str
    variable: Variable
    n: int  # total settled markets with both arms
    ncei_flips: int  # markets where NCEI disagrees with resolved bucket
    asos_flips: int  # markets where ASOS disagrees with resolved bucket
    ncei_flip_rate: float  # ncei_flips / n
    asos_flip_rate: float  # asos_flips / n
    rows: list[DivergenceRow]


def summarise(rows: list[DivergenceRow]) -> dict[str, CityResult]:
    """Group rows by city and compute per-city flip rates."""
    by_city: dict[str, list[DivergenceRow]] = {}
    for row in rows:
        by_city.setdefault(row.city, []).append(row)

    results: dict[str, CityResult] = {}
    for city, city_rows in sorted(by_city.items()):
        # Only count rows where both arms produced a value
        scored = [
            r for r in city_rows if r.ncei_in_bucket is not None and r.asos_in_bucket is not None
        ]
        n = len(scored)
        if n == 0:
            continue
        ncei_flips = sum(1 for r in scored if not r.ncei_in_bucket)
        asos_flips = sum(1 for r in scored if not r.asos_in_bucket)
        # Determine the variable (all rows for a city have the same variable in practice)
        variable: Variable = scored[0].variable
        results[city] = CityResult(
            city=city,
            variable=variable,
            n=n,
            ncei_flips=ncei_flips,
            asos_flips=asos_flips,
            ncei_flip_rate=ncei_flips / n,
            asos_flip_rate=asos_flips / n,
            rows=city_rows,
        )
    return results


# ---------------------------------------------------------------------------
# Spike runner: fetch closed events, run both arms, collect rows
# ---------------------------------------------------------------------------


def run_spike(
    events: list[dict[str, Any]],
    client: httpx.Client,
    mapping: GhcndToIsdMapping,
) -> list[DivergenceRow]:
    """Run the divergence spike over a list of Polymarket closed event dicts.

    For each US-city temperature event with a resolved bucket:
    - Arm A: fetch GHCND daily extreme via NCEI GHCND, check bucket membership.
    - Arm B: fetch ASOS hourly tmpc via Iowa State Mesonet, reduce to daily
      extreme, check bucket membership.

    The GhcndToIsdMapping parameter is retained for future ISD use but is not
    used for Arm B in the current implementation (Mesonet uses ICAO_TO_ASOS_STATION).
    """
    from rainmaker.backfill import fetch_actuals
    from rainmaker.polymarket.markets import parse_market

    rows: list[DivergenceRow] = []
    for ev in events:
        resolved = resolved_bucket_label(ev.get("markets", []))
        if resolved is None:
            continue
        try:
            market = parse_market(ev)
        except (ValueError, KeyError):
            continue
        if market.target.variable not in ("TMAX", "TMIN"):
            continue
        if market.target.station.city not in STATIONS:
            continue

        station = market.target.station
        local_date = market.target.local_date
        variable: Variable = market.target.variable

        # Arm A: NCEI GHCND
        ncei_value: float | None = None
        ncei_in_bucket: bool | None = None
        ncei_gap: float | None = None
        try:
            actuals = fetch_actuals(station.ghcnd_id, local_date, local_date, client, variable)
            ncei_value = actuals.get(local_date)
            if ncei_value is not None:
                ncei_in_bucket = temperature_in_bucket(ncei_value, resolved)
        except httpx.HTTPError:
            pass

        # Arm B: Iowa State Mesonet ASOS
        asos_value: float | None = None
        asos_in_bucket: bool | None = None
        asos_gap: float | None = None
        asos_code = ICAO_TO_ASOS_STATION.get(station.icao)
        if asos_code is not None:
            try:
                mesonet_actuals = fetch_asos_actuals_mesonet(
                    asos_code, local_date, local_date, client, variable
                )
                asos_value = mesonet_actuals.get(local_date)
                if asos_value is not None:
                    asos_in_bucket = temperature_in_bucket(asos_value, resolved)
            except httpx.HTTPError:
                pass

        rows.append(
            DivergenceRow(
                city=station.city,
                local_date=local_date,
                variable=variable,
                resolved_label=resolved,
                ncei_value=ncei_value,
                ncei_in_bucket=ncei_in_bucket,
                asos_value=asos_value,
                asos_in_bucket=asos_in_bucket,
                ncei_gap=ncei_gap,
                asos_gap=asos_gap,
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def render_divergence_report(
    rows: list[DivergenceRow],
    city_results: dict[str, CityResult],
    run_date: str,
) -> str:
    """Render a markdown settlement-divergence report."""
    lines = [
        "# Settlement Divergence Spike - June 2026",
        "",
        f"Run date: {run_date}",
        "",
        "Measures how often NCEI GHCND (Arm A, our current source) and Iowa State Mesonet ASOS",
        "(Arm B) agree with the bucket Polymarket actually resolved.",
        "",
        "Note: the sub-plan named NCEI ISD as Arm B. NCEI ISD data has a ~10-month lag so",
        "no ISD data is available for 2025/2026 events. Iowa State Mesonet serves the same",
        "ASOS observations with minimal lag and was used for Arm B instead.",
        "",
        "**Flip rate** = fraction of markets where the arm's computed extreme falls",
        "outside the resolved bucket. Lower is better.",
        "",
        "## Per-city results",
        "",
        "| City | Variable | N | NCEI flip rate (A) | ASOS flip rate (B) |",
        "| --- | --- | --- | --- | --- |",
    ]

    for city, res in sorted(city_results.items()):
        ncei_pct = f"{res.ncei_flip_rate:.0%}"
        asos_pct = f"{res.asos_flip_rate:.0%}"
        lines.append(f"| {city} | {res.variable} | {res.n} | {ncei_pct} | {asos_pct} |")

    if rows:
        total_n = sum(r.n for r in city_results.values())
        total_ncei = sum(r.ncei_flips for r in city_results.values())
        total_asos = sum(r.asos_flips for r in city_results.values())
        ncei_overall = f"{total_ncei / total_n:.0%}" if total_n else "n/a"
        asos_overall = f"{total_asos / total_n:.0%}" if total_n else "n/a"
        lines.append(f"| **ALL** | - | {total_n} | {ncei_overall} | {asos_overall} |")

    lines += [
        "",
        "## Row-level detail",
        "",
        "| City | Date | Variable | Resolved bucket | NCEI | NCEI match | ASOS | ASOS match |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]

    for row in sorted(rows, key=lambda r: (r.city, r.local_date)):
        ncei_v = f"{row.ncei_value:.1f}F" if row.ncei_value is not None else "n/a"
        asos_v = f"{row.asos_value:.1f}F" if row.asos_value is not None else "n/a"
        ncei_m = "n/a" if row.ncei_in_bucket is None else ("yes" if row.ncei_in_bucket else "NO")
        asos_m = "n/a" if row.asos_in_bucket is None else ("yes" if row.asos_in_bucket else "NO")
        lines.append(
            f"| {row.city} | {row.local_date} | {row.variable} | {row.resolved_label} "
            f"| {ncei_v} | {ncei_m} | {asos_v} | {asos_m} |"
        )

    lines += [
        "",
        "## Recommendation",
        "",
    ]

    total_n = sum(r.n for r in city_results.values())
    if total_n == 0:
        lines.append("Insufficient data to make a recommendation.")
    else:
        total_ncei_flips = sum(r.ncei_flips for r in city_results.values())
        total_asos_flips = sum(r.asos_flips for r in city_results.values())
        ncei_rate = total_ncei_flips / total_n
        asos_rate = total_asos_flips / total_n
        delta = ncei_rate - asos_rate  # positive means ASOS is better
        if ncei_rate == 0 and asos_rate == 0:
            lines.append(
                "Both arms matched the resolved bucket on all sampled markets (0% flip rate). "
                "No source switch is needed (#101b: document and bound)."
            )
        elif delta > 0.05:
            lines.append(
                f"ASOS (Arm B) outperforms NCEI GHCND (Arm A) by {delta:.0%} points. "
                "Proceed to #101b: investigate switching settlement to ASOS/ISD."
            )
        elif delta < -0.05:
            lines.append(
                f"NCEI GHCND (Arm A) outperforms ASOS (Arm B) by {-delta:.0%} points. "
                "Keep current source; document divergence and bound (#101b: document)."
            )
        else:
            lines.append(
                f"Both arms have similar flip rates "
                f"(NCEI: {ncei_rate:.0%}, ASOS: {asos_rate:.0%}). "
                "The divergence is too small to justify a source switch. "
                "Proceed to #101b: document and bound the known gap."
            )

    return "\n".join(lines) + "\n"
