"""Tests for the gap-recovery subcommand (issue #363).

Fixture closed events, mocked price history, and mocked forecasts exercise the
core guarantees: deterministic run IDs, idempotency on re-run, and correct row
counts in the store.
"""

import json
from datetime import date
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from rainmaker.pnl_backtest import SECONDS_PER_DAY
from rainmaker.recover_gap import recover_gap
from rainmaker.store.db import connect, init_schema
from rainmaker.store.query import count_rows, get_run


def _make_event(
    event_id: str,
    city: str,
    icao: str,
    station_name: str,
    variable: str,
    settlement_date: date,
    token_prefix: str = "tok",
) -> dict[str, Any]:
    """Build a minimal Gamma closed-event dict that parse_market accepts."""
    kind_word = "highest" if variable == "TMAX" else "lowest"
    title = (
        f"{kind_word.capitalize()} temperature in {city} on {settlement_date.strftime('%B %-d')}?"
    )
    end_date = f"{settlement_date.isoformat()}T12:00:00Z"
    # Description must name the station (ICAO or name) per the relaxed guard.
    description = f"Resolves at {station_name} ({icao}) in degrees Fahrenheit."
    n_buckets = 3
    buckets = []
    for i in range(n_buckets):
        buckets.append(
            {
                "groupItemTitle": f"{60 + i * 5}-{60 + i * 5 + 1}°F",
                "outcomes": '["Yes", "No"]',
                "outcomePrices": json.dumps([str(0.3 + i * 0.1), str(0.7 - i * 0.1)]),
                "bestAsk": 0.3 + i * 0.1,
                "bestBid": 0.28 + i * 0.1,
                "clobTokenIds": json.dumps([f"{token_prefix}_{i}0", f"{token_prefix}_{i}1"]),
            }
        )
    return {
        "id": event_id,
        "slug": f"{kind_word}-temp-{city.lower()}-{settlement_date.isoformat()}",
        "title": title,
        "endDate": end_date,
        "description": description,
        "markets": buckets,
    }


_NYC = ("NYC", "KLGA", "LaGuardia Airport")
_MIAMI = ("Miami", "KMIA", "Miami Intl Airport")


def _us_events_desc_order() -> list[dict[str, Any]]:
    """Two dates, two cities, TMAX and TMIN, ordered endDate descending."""
    d1 = date(2026, 8, 25)
    d2 = date(2026, 8, 24)
    events = [
        _make_event("evt-1", *_NYC, "TMAX", d1, token_prefix="ny1"),
        _make_event("evt-2", *_MIAMI, "TMIN", d1, token_prefix="mi1"),
        _make_event("evt-3", *_NYC, "TMAX", d2, token_prefix="ny2"),
        _make_event("evt-4", *_MIAMI, "TMIN", d2, token_prefix="mi2"),
    ]
    return events


def _old_event_before_window() -> list[dict[str, Any]]:
    """An event older than --from to verify early exit."""
    return [*_us_events_desc_order(), _make_event("evt-old", *_NYC, "TMAX", date(2026, 8, 20))]


def _intl_event() -> list[dict[str, Any]]:
    """A non-US event that must be filtered out."""
    return [
        *_us_events_desc_order(),
        _make_event("evt-intl", "London", "EGLC", "London City Airport", "TMAX", date(2026, 8, 25)),
    ]


@pytest.fixture()
def db():
    conn = connect(":memory:")
    init_schema(conn)
    yield conn
    conn.close()


def _mock_price_history(token_id: str, start_ts: int, end_ts: int, client, **kwargs):
    """Return price points within the snap tolerance of the implicit target ts.

    _repriced_market calls fetch_price_history with start_ts = target_ts - 1 day
    and end_ts = target_ts + 1 hour. A point at target_ts - 1 hour is well
    inside the 12-hour snap tolerance (SNAP_TOLERANCE_S = 43200s).
    """
    from rainmaker.polymarket.prices import PricePoint

    target_ts = start_ts + SECONDS_PER_DAY
    t = target_ts - 3600
    return [PricePoint(t=t, p=0.30), PricePoint(t=t + 1800, p=0.31)]


def _mock_fetch_samples(*args, **kwargs):
    """Return one dummy ForecastSample per date so forecast_set_from_samples works."""
    from rainmaker.forecasts.base import ForecastSample

    station = args[0]
    start = args[1]
    end = args[2]
    out: dict[date, list[ForecastSample]] = {}
    d = start
    while d <= end:
        out[d] = [
            ForecastSample(
                source="open-meteo",
                model="gfs",
                member=None,
                station=station.icao,
                variable="TMAX",
                target_date=d,
                lead_time_days=1,
                value_f=70.0,
                issued_at=None,
            )
        ]
        d = date.fromordinal(d.toordinal() + 1)
    return out


class TestGapEventFiltering:
    """Checkpoint 1: filter closed events to the gap window."""

    def test_filters_to_us_cities(self, db):
        events = _intl_event()
        with httpx.Client() as client:
            with patch(
                "rainmaker.recover_gap.fetch_price_history", side_effect=_mock_price_history
            ):
                with patch(
                    "rainmaker.recover_gap.fetch_historical_samples",
                    side_effect=_mock_fetch_samples,
                ):
                    recorded = recover_gap(
                        db,
                        client,
                        events,
                        from_date=date(2026, 8, 23),
                        to_date=date(2026, 8, 31),
                    )
        assert recorded == 2  # two dates (Aug 24, Aug 25), not the intl event

    def test_early_exit_on_old_events(self, db):
        events = _old_event_before_window()
        with httpx.Client() as client:
            with patch(
                "rainmaker.recover_gap.fetch_price_history", side_effect=_mock_price_history
            ):
                with patch(
                    "rainmaker.recover_gap.fetch_historical_samples",
                    side_effect=_mock_fetch_samples,
                ):
                    recorded = recover_gap(
                        db,
                        client,
                        events,
                        from_date=date(2026, 8, 23),
                        to_date=date(2026, 8, 31),
                    )
        assert recorded == 2  # Aug 24 and 25, not Aug 20

    def test_empty_window_records_zero(self, db):
        events = _us_events_desc_order()
        with httpx.Client() as client:
            with patch(
                "rainmaker.recover_gap.fetch_price_history", side_effect=_mock_price_history
            ):
                with patch(
                    "rainmaker.recover_gap.fetch_historical_samples",
                    side_effect=_mock_fetch_samples,
                ):
                    recorded = recover_gap(
                        db,
                        client,
                        events,
                        from_date=date(2026, 9, 1),
                        to_date=date(2026, 9, 15),
                    )
        assert recorded == 0


class TestIdempotency:
    """Checkpoint 5: re-running recover-gap skips already-persisted dates."""

    def test_second_run_skips_existing(self, db):
        events = _us_events_desc_order()
        with httpx.Client() as client:
            with patch(
                "rainmaker.recover_gap.fetch_price_history", side_effect=_mock_price_history
            ):
                with patch(
                    "rainmaker.recover_gap.fetch_historical_samples",
                    side_effect=_mock_fetch_samples,
                ):
                    first = recover_gap(
                        db,
                        client,
                        events,
                        from_date=date(2026, 8, 23),
                        to_date=date(2026, 8, 31),
                    )
                    second = recover_gap(
                        db,
                        client,
                        events,
                        from_date=date(2026, 8, 23),
                        to_date=date(2026, 8, 31),
                    )
        assert first == 2
        assert second == 0  # all dates already present

    def test_deterministic_run_ids(self, db):
        events = _us_events_desc_order()
        with httpx.Client() as client:
            with patch(
                "rainmaker.recover_gap.fetch_price_history", side_effect=_mock_price_history
            ):
                with patch(
                    "rainmaker.recover_gap.fetch_historical_samples",
                    side_effect=_mock_fetch_samples,
                ):
                    recover_gap(
                        db,
                        client,
                        events,
                        from_date=date(2026, 8, 23),
                        to_date=date(2026, 8, 31),
                    )
        assert get_run(db, "backfill-2026-08-24") is not None
        assert get_run(db, "backfill-2026-08-25") is not None


class TestRowCounts:
    """Checkpoint 7: correct row counts after recovery."""

    def test_markets_and_runs_recorded(self, db):
        events = _us_events_desc_order()
        with httpx.Client() as client:
            with patch(
                "rainmaker.recover_gap.fetch_price_history", side_effect=_mock_price_history
            ):
                with patch(
                    "rainmaker.recover_gap.fetch_historical_samples",
                    side_effect=_mock_fetch_samples,
                ):
                    recover_gap(
                        db,
                        client,
                        events,
                        from_date=date(2026, 8, 23),
                        to_date=date(2026, 8, 31),
                    )
        # 2 dates x 2 markets each = 4 markets (UPSERT by id)
        assert count_rows(db, "markets") == 4
        # 2 runs (one per date)
        assert count_rows(db, "runs") == 2
        # Prices: 4 markets x 3 buckets x 2 sides (YES + NO) = 24 price rows
        assert count_rows(db, "prices") == 24
        # Predictions: 4 markets x 3 buckets x 2 sides (YES + NO) = 24 rows
        assert count_rows(db, "predictions") == 24
        # Forecasts: 4 markets x 1 forecast row each = 4
        assert count_rows(db, "forecasts") == 4

    def test_no_duplicate_rows_on_rerun(self, db):
        events = _us_events_desc_order()
        with httpx.Client() as client:
            with patch(
                "rainmaker.recover_gap.fetch_price_history", side_effect=_mock_price_history
            ):
                with patch(
                    "rainmaker.recover_gap.fetch_historical_samples",
                    side_effect=_mock_fetch_samples,
                ):
                    recover_gap(
                        db,
                        client,
                        events,
                        from_date=date(2026, 8, 23),
                        to_date=date(2026, 8, 31),
                    )
                    before_runs = count_rows(db, "runs")
                    before_prices = count_rows(db, "prices")
                    before_preds = count_rows(db, "predictions")
                    before_fcsts = count_rows(db, "forecasts")

                    recover_gap(
                        db,
                        client,
                        events,
                        from_date=date(2026, 8, 23),
                        to_date=date(2026, 8, 31),
                    )
        assert count_rows(db, "runs") == before_runs
        assert count_rows(db, "prices") == before_prices
        assert count_rows(db, "predictions") == before_preds
        assert count_rows(db, "forecasts") == before_fcsts


class TestMissingForecastsGraceful:
    """Risk: missing forecast archive -> market recorded with prices, no predictions."""

    def test_missing_forecast_still_records_market(self, db):
        events = _us_events_desc_order()
        with httpx.Client() as client:
            with patch(
                "rainmaker.recover_gap.fetch_price_history", side_effect=_mock_price_history
            ):
                with patch("rainmaker.recover_gap.fetch_historical_samples", return_value={}):
                    recover_gap(
                        db,
                        client,
                        events,
                        from_date=date(2026, 8, 23),
                        to_date=date(2026, 8, 31),
                    )
        # Markets and prices still recorded despite no forecast samples
        assert count_rows(db, "markets") == 4
        assert count_rows(db, "prices") == 24
        assert count_rows(db, "runs") == 2
