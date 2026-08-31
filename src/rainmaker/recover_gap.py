"""Backfill the discovery gap left by the pre-#361 ICAO guard rejection.

From ~Aug 23 through Aug 31 2026, Polymarket's description format change caused
the ICAO guard to reject all US TMAX/TMIN markets, leaving a hole in the store.
This module orchestrates the existing pipeline components to rediscover,
reprice, re-forecast, and persist those missed runs with deterministic run IDs
so re-runs are idempotent.

Settlement is deliberately NOT embedded here; the operator runs
``uv run rainmaker settle`` separately afterward.
"""

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from rainmaker.backfill import fetch_historical_samples
from rainmaker.config import (
    CONFIDENCE_FLOOR,
    CONFIDENCE_FLOOR_NO,
    MAX_EDGE,
    MIN_EDGE,
    MIN_SIGMA_F,
    STATION_EDGE_DELTA,
    STATION_POLICIES,
    STATIONS,
)
from rainmaker.domain import Market
from rainmaker.forecasts.base import ForecastSet
from rainmaker.pnl_backtest import (
    SECONDS_PER_DAY,
    SNAP_TOLERANCE_S,
    forecast_set_from_samples,
    market_at_lead,
)
from rainmaker.polymarket.markets import parse_market
from rainmaker.polymarket.prices import fetch_price_history, last_before
from rainmaker.ranking.edge import evaluate_market
from rainmaker.store.db import Conn
from rainmaker.store.query import get_run, load_calibration
from rainmaker.store.record import EvaluatedMarket, record_run

_US_CITY_NAMES = frozenset(STATIONS.keys())


def _gap_events(
    events: list[dict[str, Any]],
    *,
    from_date: date,
    to_date: date,
) -> list[dict[str, Any]]:
    """Closed weather events settling in [from_date, to_date], US TMAX/TMIN only.

    Events arrive ordered by endDate descending. Once we see an event whose
    settlement date is older than from_date, we can stop scanning: everything
    after it is older still.
    """
    out: list[dict[str, Any]] = []
    for ev in events:
        try:
            market = parse_market(ev)
        except (ValueError, KeyError):
            continue
        if market.target.variable not in ("TMAX", "TMIN"):
            continue
        if market.target.station.city not in _US_CITY_NAMES:
            continue
        sdate = market.target.local_date
        if sdate < from_date:
            break  # events are ordered endDate descending; stop early
        if sdate > to_date:
            continue
        out.append(ev)
    return out


def _run_id_for(settlement_date: date) -> str:
    return f"backfill-{settlement_date.isoformat()}"


def _run_ts_for(settlement_date: date) -> datetime:
    """Synthesized run timestamp: noon UTC that day."""
    return datetime.combine(settlement_date, datetime.min.time(), tzinfo=UTC).replace(hour=12)


def _repriced_market(
    market: Market,
    client: httpx.Client,
    run_ts: datetime,
) -> Market:
    """Snap each bucket's YES mid from CLOB price history at the run timestamp."""
    target_ts = int(run_ts.timestamp())
    start_ts = target_ts - SECONDS_PER_DAY
    end_ts = target_ts + 3600
    mids: dict[str, float | None] = {}
    for bucket in market.buckets:
        history = fetch_price_history(bucket.yes_token_id, start_ts, end_ts, client)
        mids[bucket.label] = last_before(history, target_ts, max_age_s=SNAP_TOLERANCE_S)
    return market_at_lead(market, mids)


def _evaluate(
    market: Market,
    forecast_set: ForecastSet,
    conn: Conn,
    run_date: date,
) -> EvaluatedMarket:
    settlement_date = market.target.local_date
    lead_time = (settlement_date - run_date).days
    calibration = load_calibration(
        conn, market.target.station.icao, market.target.variable, lead_time
    )
    report = evaluate_market(
        market,
        forecast_set,
        floor=CONFIDENCE_FLOOR,
        floor_no=CONFIDENCE_FLOOR_NO,
        min_sources=1,
        min_sigma=MIN_SIGMA_F,
        min_edge=MIN_EDGE,
        calibration=calibration,
        station_policy=STATION_POLICIES.get(market.target.station.icao),
        min_edge_delta=STATION_EDGE_DELTA.get(
            (market.target.station.icao, market.target.variable), 0.0
        ),
        max_edge=MAX_EDGE,
    )
    return (market, forecast_set, report)


def recover_gap(
    conn: Conn,
    client: httpx.Client,
    events: list[dict[str, Any]],
    *,
    from_date: date,
    to_date: date,
) -> int:
    """Discover, reprice, re-forecast, and persist gap-period runs.

    Returns the number of runs recorded (dates persisted). Dates whose
    deterministic run ID already exists in the store are skipped (idempotency).
    """
    gap_events = _gap_events(events, from_date=from_date, to_date=to_date)
    if not gap_events:
        return 0

    # Group parsed markets by settlement date so we persist one run per date.
    by_date: dict[date, list[Market]] = defaultdict(list)
    for ev in gap_events:
        market = parse_market(ev)
        by_date[market.target.local_date].append(market)

    # Pre-fetch historical forecast samples per station over the full span,
    # matching the pnl_backtest pattern (one request per station covers all dates).
    all_dates = sorted(by_date.keys())
    samples_by_station: dict[str, dict[date, list[Any]]] = {}
    stations_seen: set[str] = set()
    for d in all_dates:
        for market in by_date[d]:
            stations_seen.add(market.target.station.icao)
    for icao in stations_seen:
        station = next(s for s in STATIONS.values() if s.icao == icao)
        samples_by_station[icao] = fetch_historical_samples(
            station, all_dates[0], all_dates[-1], client
        )

    recorded = 0
    for settlement_date in all_dates:
        run_id = _run_id_for(settlement_date)
        if get_run(conn, run_id) is not None:
            continue  # idempotency: already recovered

        run_ts = _run_ts_for(settlement_date)
        started_at = run_ts.isoformat()
        finished_at = (run_ts + timedelta(minutes=1)).isoformat()

        evaluated: list[EvaluatedMarket] = []
        for market in by_date[settlement_date]:
            repriced = _repriced_market(market, client, run_ts)
            samples = samples_by_station.get(market.target.station.icao, {}).get(settlement_date)
            if samples:
                forecast_set = forecast_set_from_samples(market.target, samples)
            else:
                forecast_set = ForecastSet(target=market.target, samples=[], coverage=[])
            evaluated.append(_evaluate(repriced, forecast_set, conn, settlement_date))

        record_run(
            conn,
            run_id=run_id,
            started_at=started_at,
            finished_at=finished_at,
            status="ok",
            evaluated=evaluated,
        )
        recorded += 1

    return recorded
