"""Throwaway comparison script: ASOS (Iowa State Mesonet) vs NOAA wrh/timeseries (Synoptic API).

Pulls daily TMAX/TMIN from both sources for the 11 US Polymarket stations over
~14 recent settled dates, computes per-station/date/variable deltas, and prints
a summary table plus a machine-readable JSON file.

Usage:
    uv run python scripts/compare_asos_vs_wrh.py [--days N] [--output FILE]

The wrh/timeseries page (https://www.weather.gov/wrh/timeseries?site=<lc-icao>)
loads data via the Synoptic Data API (api.synopticdata.com/v2/stations/timeseries)
using a hardcoded token embedded in weather.gov's apiKey.js. The API requires a
Referer header matching weather.gov.

Both sources ultimately read the same ASOS station observations, but differ in:
  - Temporal granularity: Synoptic returns all obs (incl. SPECI); our ASOS path
    sends report_type=3 (routine hourly METAR only).
  - Day bucketing: wrh/timeseries uses local-day (obtimezone=local); our ASOS
    US path uses UTC-day bucketing.
  - Units: both return Fahrenheit.

We compute daily max/min from the raw hourly temps ourselves (local-day bucketing
for wrh, UTC-day bucketing for ASOS) to isolate the source/methodology difference.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rainmaker.config import STATIONS  # noqa: E402
from rainmaker.forecasts.asos import (  # noqa: E402
    ICAO_TO_ASOS_STATION,
    fetch_asos_daily_extreme,
)

SYNOPTIC_API_URL = "https://api.synopticdata.com/v2/stations/timeseries"
SYNOPTIC_TOKEN = "7c76618b66c74aee913bdbae4b448bdd"
WRH_REFERER = "https://www.weather.gov/wrh/timeseries"

# Rate limiting
REQUEST_DELAY_S = 0.5


def fetch_wrh_timeseries(
    stid: str,
    start: date,
    end: date,
    client: httpx.Client,
) -> list[dict]:
    """Fetch hourly temperature observations from the Synoptic API backing wrh/timeseries.

    Returns a list of dicts with 'timestamp' (str, local TZ) and 'temp_f' (float|None).
    """
    start_str = start.strftime("%Y%m%d%H%M")
    end_str = end.strftime("%Y%m%d%H%M")

    params = {
        "STID": stid,
        "showemptystations": "1",
        "units": "temp|F,speed|mph,english",
        "start": start_str,
        "end": end_str,
        "complete": "1",
        "token": SYNOPTIC_TOKEN,
        "obtimezone": "local",
    }
    headers = {
        "Referer": f"{WRH_REFERER}?site={stid.lower()}",
        "Origin": "https://www.weather.gov",
    }

    for attempt in range(4):
        resp = client.get(SYNOPTIC_API_URL, params=params, headers=headers)
        if resp.status_code == 429:
            if attempt + 1 == 4:
                resp.raise_for_status()
            time.sleep(5.0)
            continue
        resp.raise_for_status()
        break

    data = resp.json()
    if data.get("SUMMARY", {}).get("RESPONSE_CODE") != 1:
        print(f"  WRH API error for {stid}: {data.get('SUMMARY', {})}", file=sys.stderr)
        return []

    station_data = data["STATION"][0]["OBSERVATIONS"]
    timestamps: list[str] = station_data.get("date_time", [])
    temps: list[float | None] = station_data.get("air_temp_set_1", [])

    results = []
    for ts_str, temp_val in zip(timestamps, temps, strict=False):
        if temp_val is None:
            continue
        results.append({"timestamp": ts_str, "temp_f": float(temp_val)})
    return results


def compute_wrh_daily_extremes(
    obs: list[dict],
    timezone: str,
    target_dates: list[date],
) -> dict[date, dict[str, float]]:
    """Compute daily TMAX/TMIN from wrh observations using local-day bucketing.

    Returns {date: {"TMAX": float, "TMIN": float}}.
    """
    tz = ZoneInfo(timezone)
    by_day: dict[date, list[float]] = {}
    for ob in obs:
        # Timestamps look like "2026-08-20T14:55:00-0500"
        try:
            dt = datetime.fromisoformat(ob["timestamp"])
            local_dt = dt.astimezone(tz)
            local_date = local_dt.date()
        except (ValueError, TypeError):
            continue
        if local_date not in set(target_dates):
            continue
        by_day.setdefault(local_date, []).append(ob["temp_f"])

    result = {}
    for d, readings in by_day.items():
        if readings:
            result[d] = {"TMAX": max(readings), "TMIN": min(readings)}
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=14, help="Number of recent days to compare")
    parser.add_argument(
        "--output",
        type=str,
        default="scripts/comparison_results.json",
        help="Output JSON file path",
    )
    args = parser.parse_args()

    # Date range: most recent N settled days (excluding today since data may be incomplete)
    today = date.today()
    end_date = today - timedelta(days=1)
    start_date = end_date - timedelta(days=args.days - 1)
    target_dates = [start_date + timedelta(days=i) for i in range(args.days)]

    print(f"Comparing ASOS vs wrh/timeseries for {args.days} days: {start_date} to {end_date}")
    print(f"Stations: {len(STATIONS)} US Polymarket stations")
    print()

    client = httpx.Client(timeout=60.0)

    all_results = []
    station_summaries = {}

    for city, station in STATIONS.items():
        icao = station.icao
        asos_code = ICAO_TO_ASOS_STATION[icao]
        lc_icao = icao.lower()
        tz = station.timezone

        print(f"[{city}] ICAO={icao} ASOS={asos_code}")

        # --- ASOS (Iowa State Mesonet) ---
        # Pad by 1 day on each side to catch UTC/local boundary effects
        asos_start = start_date - timedelta(days=1)
        asos_end = end_date + timedelta(days=1)
        try:
            asos_max = fetch_asos_daily_extreme(asos_code, asos_start, asos_end, client, "TMAX")
            time.sleep(REQUEST_DELAY_S)
            asos_min = fetch_asos_daily_extreme(asos_code, asos_start, asos_end, client, "TMIN")
            time.sleep(REQUEST_DELAY_S)
        except Exception as e:
            print(f"  ASOS fetch failed: {e}", file=sys.stderr)
            continue

        # --- WRH (Synoptic API) ---
        wrh_start = start_date - timedelta(days=1)
        wrh_end = end_date + timedelta(days=1)
        try:
            wrh_obs = fetch_wrh_timeseries(lc_icao, wrh_start, wrh_end, client)
            time.sleep(REQUEST_DELAY_S)
        except Exception as e:
            print(f"  WRH fetch failed: {e}", file=sys.stderr)
            continue

        wrh_extremes = compute_wrh_daily_extremes(wrh_obs, tz, target_dates)

        # --- Compare ---
        station_results = []
        for d in target_dates:
            asos_tmax = asos_max.get(d)
            asos_tmin = asos_min.get(d)
            wrh_day = wrh_extremes.get(d)
            wrh_tmax = wrh_day["TMAX"] if wrh_day else None
            wrh_tmin = wrh_day["TMIN"] if wrh_day else None

            tmax_delta = (
                abs(asos_tmax - wrh_tmax)
                if asos_tmax is not None and wrh_tmax is not None
                else None
            )
            tmin_delta = (
                abs(asos_tmin - wrh_tmin)
                if asos_tmin is not None and wrh_tmin is not None
                else None
            )

            station_results.append(
                {
                    "city": city,
                    "icao": icao,
                    "date": d.isoformat(),
                    "asos_tmax": round(asos_tmax, 1) if asos_tmax is not None else None,
                    "wrh_tmax": round(wrh_tmax, 1) if wrh_tmax is not None else None,
                    "tmax_delta": round(tmax_delta, 1) if tmax_delta is not None else None,
                    "asos_tmin": round(asos_tmin, 1) if asos_tmin is not None else None,
                    "wrh_tmin": round(wrh_tmin, 1) if wrh_tmin is not None else None,
                    "tmin_delta": round(tmin_delta, 1) if tmin_delta is not None else None,
                }
            )

        all_results.extend(station_results)

        # Quick summary for this station
        tmax_deltas = [r["tmax_delta"] for r in station_results if r["tmax_delta"] is not None]
        tmin_deltas = [r["tmin_delta"] for r in station_results if r["tmin_delta"] is not None]
        all_deltas = tmax_deltas + tmin_deltas

        ge1_count = sum(1 for d in all_deltas if d >= 1.0)
        total_count = len(all_deltas)

        station_summaries[city] = {
            "icao": icao,
            "total_comparisons": total_count,
            "deltas_ge_1f": ge1_count,
            "pct_ge_1f": round(100.0 * ge1_count / total_count, 1) if total_count else 0,
            "mean_abs_delta": round(sum(all_deltas) / total_count, 2) if total_count else 0,
            "max_abs_delta": round(max(all_deltas), 1) if all_deltas else 0,
        }

        pct = station_summaries[city]["pct_ge_1f"]
        mean_d = station_summaries[city]["mean_abs_delta"]
        max_d = station_summaries[city]["max_abs_delta"]
        print(
            f"  Comparisons: {total_count}, >=1F: {ge1_count} ({pct}%), "
            f"mean|delta|={mean_d}, max|delta|={max_d}"
        )
        print()

    client.close()

    # Overall summary
    all_deltas_flat = [r["tmax_delta"] for r in all_results if r["tmax_delta"] is not None] + [
        r["tmin_delta"] for r in all_results if r["tmin_delta"] is not None
    ]
    total = len(all_deltas_flat)
    ge1 = sum(1 for d in all_deltas_flat if d >= 1.0)
    overall_pct = round(100.0 * ge1 / total, 1) if total else 0
    mean_abs = round(sum(all_deltas_flat) / total, 3) if total else 0
    max_abs = round(max(all_deltas_flat), 1) if all_deltas_flat else 0

    print("=" * 70)
    print("OVERALL SUMMARY")
    print(f"  Total comparisons (station-date-variable): {total}")
    print(f"  Deltas >= 1 deg F: {ge1} ({overall_pct}%)")
    print(f"  Mean absolute delta: {mean_abs} deg F")
    print(f"  Max absolute delta: {max_abs} deg F")
    print("  Threshold for material: >=1 deg F on >15% of station-days")
    print(f"  Result: {'DIVERGENCE MATERIAL' if overall_pct > 15 else 'DIVERGENCE IMMATERIAL'}")
    print()

    # Save results
    output = {
        "date_range": {"start": start_date.isoformat(), "end": end_date.isoformat()},
        "stations": station_summaries,
        "results": all_results,
        "summary": {
            "total_comparisons": total,
            "deltas_ge_1f": ge1,
            "pct_ge_1f": overall_pct,
            "mean_abs_delta": mean_abs,
            "max_abs_delta": max_abs,
            "material_threshold_pct": 15,
            "material_threshold_deg_f": 1.0,
            "result": "DIVERGENCE MATERIAL" if overall_pct > 15 else "DIVERGENCE IMMATERIAL",
        },
    }

    output_path = Path(args.output)
    output_path.write_text(json.dumps(output, indent=2))
    print(f"Results written to {output_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
