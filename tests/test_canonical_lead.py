"""The ONE canonical lead ruler (#460, implements #438).

Lead is the station-local calendar-day difference between the target date and
the anchor instant - the quantity the markets settle on. Three call sites must
agree on it: the cli gate (calibration load), NWS sample tagging (anchored at
issuance), and Open-Meteo sample tagging (anchored at fetch). They historically
diverged during the 00:00-06:00 UTC cron window, when UTC-today runs one local
day ahead of station-local today.
"""

import copy
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from rainmaker.config import build_target
from rainmaker.domain import canonical_lead

FIXTURES = Path(__file__).parent / "fixtures"

# 2026-06-01 00:30 UTC = 2026-05-31 20:30 EDT: inside the cron window, UTC-today
# (June 1) is one day ahead of station-local today (May 31).
CRON_WINDOW_NOW = datetime(2026, 6, 1, 0, 30, tzinfo=UTC)
# 2026-06-01 12:30 UTC = 08:30 EDT: after local midnight, the rulers agree even
# under the old UTC definition.
MORNING_NOW = datetime(2026, 6, 1, 12, 30, tzinfo=UTC)


# ---------------------------------------------------------------------------
# The definition itself
# ---------------------------------------------------------------------------


def test_lead_zero_for_current_local_day():
    target = build_target("NYC", "TMAX", date(2026, 6, 1))
    assert canonical_lead(target, MORNING_NOW) == 0


def test_lead_counts_station_local_calendar_days():
    target = build_target("NYC", "TMAX", date(2026, 6, 2))
    assert canonical_lead(target, MORNING_NOW) == 1
    target3 = build_target("NYC", "TMAX", date(2026, 6, 4))
    assert canonical_lead(target3, MORNING_NOW) == 3


def test_lead_ignores_the_utc_day_inside_the_cron_window():
    # The #438 divergence case: at 00:30 UTC the naive UTC ruler says a
    # next-local-day target is lead 0 (UTC-today == the target date); the
    # canonical ruler says 1 (station-local today is still May 31).
    target = build_target("NYC", "TMAX", date(2026, 6, 1))
    assert canonical_lead(target, CRON_WINDOW_NOW) == 1


def test_lead_uses_each_stations_own_timezone():
    # 00:30 UTC is 19:30 CDT May 31 in Chicago and 20:30 EDT May 31 in NYC:
    # the same window, different clocks, same station-local answer.
    chi = build_target("Chicago", "TMAX", date(2026, 6, 1))
    assert canonical_lead(chi, CRON_WINDOW_NOW) == 1
    la = build_target("Los Angeles", "TMAX", date(2026, 6, 1))
    assert canonical_lead(la, CRON_WINDOW_NOW) == 1


def test_negative_lead_means_the_local_day_has_passed():
    target = build_target("NYC", "TMAX", date(2026, 5, 31))
    assert canonical_lead(target, MORNING_NOW) == -1


def test_requires_aware_now():
    target = build_target("NYC", "TMAX", date(2026, 6, 1))
    with pytest.raises(ValueError):
        canonical_lead(target, datetime(2026, 6, 1, 0, 30))


# ---------------------------------------------------------------------------
# Cross-call-site agreement across the cron-window boundary
# ---------------------------------------------------------------------------


def _nws_periods_json(update_time: str, target_date: date) -> dict[str, Any]:
    """Minimal periods product whose daytime period covers the target day."""
    iso = target_date.isoformat()
    return {
        "properties": {
            "updateTime": update_time,
            "periods": [
                {
                    "name": target_date.strftime("%A"),
                    "startTime": f"{iso}T12:00:00-04:00",
                    "endTime": f"{iso}T18:00:00-04:00",
                    "isDaytime": True,
                    "temperatureUnit": "F",
                    "temperature": 75,
                    "temperatureTrend": None,
                    "probabilityOfPrecipitation": {"unitCode": "wmoUnit:percent", "value": None},
                    "windSpeed": "5 mph",
                    "windDirection": "SW",
                    "shortForecast": "Sunny",
                    "detailedForecast": "",
                }
            ],
        }
    }


def _openmeteo_payload(base_date: date, target_date: date) -> dict[str, Any]:
    days = [(base_date + timedelta(days=i)).isoformat() for i in range(7)]
    return {
        "latitude": 40.7792,
        "longitude": -73.8803,
        "daily": {
            "time": days,
            "temperature_2m_max_gfs_seamless": [70.0 + i for i in range(7)],
        },
        "daily_units": {"temperature_2m_max_gfs_seamless": "°F"},
    }


def _om_lead(payload: dict[str, Any], target, now: datetime) -> int:
    from rainmaker.forecasts.openmeteo import OpenMeteoSource

    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    with httpx.Client(transport=transport) as client:
        samples = OpenMeteoSource(client).fetch(target, now=now)
    assert len(samples) == 1
    return samples[0].lead_time_days


def _nws_lead(payload: dict[str, Any], target) -> int:
    from rainmaker.forecasts.nws import parse

    samples = parse(copy.deepcopy(payload), target)
    assert len(samples) == 1
    return samples[0].lead_time_days


def test_all_three_rulers_agree_inside_the_cron_window():
    # One local day out, fetched at 00:30 UTC: the old gate said 0 (bug), both
    # source tags said 1. All three must now say 1.
    target = build_target("NYC", "TMAX", date(2026, 6, 1))

    gate_lead = canonical_lead(target, CRON_WINDOW_NOW)

    # NWS anchored at issuance (00:23 UTC = 20:23 EDT May 31).
    nws_json = _nws_periods_json("2026-06-01T00:23:35+00:00", date(2026, 6, 1))
    nws_lead = _nws_lead(nws_json, target)

    # Open-Meteo anchored at fetch; its payload bases on station-local today.
    om_lead = _om_lead(
        _openmeteo_payload(date(2026, 5, 31), date(2026, 6, 1)), target, CRON_WINDOW_NOW
    )

    assert gate_lead == nws_lead == om_lead == 1


def test_all_three_rulers_agree_after_local_midnight():
    # Morning run: the next local day out is lead 1 everywhere, and the
    # current local day is lead 0 everywhere.
    target_today = build_target("NYC", "TMAX", date(2026, 6, 1))
    target_tomorrow = build_target("NYC", "TMAX", date(2026, 6, 2))

    nws_json = _nws_periods_json("2026-06-01T11:23:35+00:00", date(2026, 6, 2))

    assert canonical_lead(target_today, MORNING_NOW) == 0
    assert (
        canonical_lead(target_tomorrow, MORNING_NOW)
        == _nws_lead(nws_json, target_tomorrow)
        == _om_lead(
            _openmeteo_payload(date(2026, 6, 1), date(2026, 6, 2)), target_tomorrow, MORNING_NOW
        )
        == 1
    )


def test_om_fetch_reanchors_a_cached_payload_whose_base_day_lagged():
    # Defense for the stale-cache shape: the payload still bases on yesterday
    # while the fetch happens on the next local day. The persisted lead must
    # follow the canonical ruler (fetch anchor), not the payload index.
    target = build_target("NYC", "TMAX", date(2026, 6, 1))
    stale_payload = _openmeteo_payload(date(2026, 5, 30), date(2026, 6, 1))
    assert _om_lead(stale_payload, target, CRON_WINDOW_NOW) == 1
