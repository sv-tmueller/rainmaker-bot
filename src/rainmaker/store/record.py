"""Persist a completed pipeline run.

Writes from the domain objects (Market, ForecastSet, MarketReport), not the
rendered report, so every run is fully reconstructable.
"""

import json
from collections import defaultdict

from rainmaker.forecasts.base import ForecastSet
from rainmaker.polymarket.markets import Market
from rainmaker.probability.calibration import Calibration
from rainmaker.ranking.edge import MarketReport
from rainmaker.store.db import Conn

# One evaluated market: the market, the forecasts it got, and the resulting report.
EvaluatedMarket = tuple[Market, ForecastSet, MarketReport]


def record_run(
    conn: Conn,
    *,
    run_id: str,
    started_at: str,
    finished_at: str,
    status: str,
    evaluated: list[EvaluatedMarket],
) -> None:
    """Persist a run and everything it produced, in one transaction."""
    conn.execute(
        "INSERT INTO runs (id, started_at, finished_at, status, coverage) VALUES (?, ?, ?, ?, ?)",
        (run_id, started_at, finished_at, status, json.dumps(_run_coverage(evaluated))),
    )
    for market, forecast_set, report in evaluated:
        _record_market(conn, market, started_at)
        _record_prices(conn, run_id, market, started_at)
        _record_forecasts(conn, run_id, market.id, forecast_set, started_at)
        _record_predictions(conn, run_id, market.id, report, finished_at)
    conn.commit()


def _run_coverage(evaluated: list[EvaluatedMarket]) -> dict[str, object]:
    sources: set[str] = set()
    for _, forecast_set, _ in evaluated:
        sources.update(c.source for c in forecast_set.coverage if c.ok)
    return {"n_markets": len(evaluated), "ok_sources": sorted(sources)}


def _record_market(conn: Conn, market: Market, captured_at: str) -> None:
    spec = [
        {"label": b.label, "kind": b.kind, "lo": b.lo, "hi": b.hi, "threshold": b.threshold}
        for b in market.buckets
    ]
    conn.execute(
        """
        INSERT INTO markets
            (id, slug, title, city, variable, resolution_source, settlement_date,
             outcome_spec, raw, captured_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            slug = excluded.slug, title = excluded.title, city = excluded.city,
            variable = excluded.variable, resolution_source = excluded.resolution_source,
            settlement_date = excluded.settlement_date, outcome_spec = excluded.outcome_spec,
            raw = excluded.raw, captured_at = excluded.captured_at
        """,
        (
            market.id,
            market.slug,
            market.title,
            market.target.station.city,
            market.target.variable,
            market.target.station.wunderground_url,
            market.target.local_date.isoformat(),
            json.dumps(spec),
            json.dumps(market.model_dump(mode="json")),
            captured_at,
        ),
    )


def _record_prices(conn: Conn, run_id: str, market: Market, captured_at: str) -> None:
    for b in market.buckets:
        conn.execute(
            "INSERT INTO prices (run_id, market_id, outcome, price, implied_prob, captured_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, market.id, b.label, b.best_ask, b.yes_price, captured_at),
        )


def _record_forecasts(
    conn: Conn,
    run_id: str,
    market_id: str,
    forecast_set: ForecastSet,
    fetched_at: str,
) -> None:
    groups: dict[tuple[str, str, str, int], list[float]] = defaultdict(list)
    for s in forecast_set.samples:
        groups[(s.source, s.model, s.variable, s.lead_time_days)].append(s.value_f)
    for (source, model, variable, lead_time), values in groups.items():
        conn.execute(
            "INSERT INTO forecasts "
            "(run_id, market_id, source, model, variable, values_json, lead_time, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, market_id, source, model, variable, json.dumps(values), lead_time, fetched_at),
        )


def _record_predictions(
    conn: Conn,
    run_id: str,
    market_id: str,
    report: MarketReport,
    created_at: str,
) -> None:
    dist_params = json.dumps(
        {"mu": report.mu, "sigma": report.sigma, "n_sources": report.n_sources}
    )
    for o in report.outcomes:
        conn.execute(
            "INSERT INTO predictions "
            "(run_id, market_id, bucket, p_win, confidence, dist_params, edge, recommended, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            # confidence stays NULL: no calibrated confidence metric is recorded here.
            (
                run_id,
                market_id,
                o.bucket_label,
                o.p_win,
                None,
                dist_params,
                o.edge,
                int(o.recommended),
                created_at,
            ),
        )


def save_calibration(conn: Conn, cal: Calibration, *, updated_at: str) -> None:
    """Upsert one calibration cell (keyed by station, variable, lead_time)."""
    conn.execute(
        """
        INSERT INTO calibration
            (station, variable, lead_time, bias, spread_scale, n_samples, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(station, variable, lead_time) DO UPDATE SET
            bias = excluded.bias, spread_scale = excluded.spread_scale,
            n_samples = excluded.n_samples, updated_at = excluded.updated_at
        """,
        (
            cal.station,
            cal.variable,
            cal.lead_time,
            cal.bias,
            cal.spread_scale,
            cal.n_samples,
            updated_at,
        ),
    )
    conn.commit()


def record_outcome(conn: Conn, market_id: str, actual_value: float, settled_at: str) -> None:
    """Upsert the settled actual for a market (keyed by market_id)."""
    conn.execute(
        "INSERT INTO outcomes (market_id, actual_value, settled_at) VALUES (?, ?, ?) "
        "ON CONFLICT(market_id) DO UPDATE SET "
        "actual_value = excluded.actual_value, settled_at = excluded.settled_at",
        (market_id, actual_value, settled_at),
    )
    conn.commit()
