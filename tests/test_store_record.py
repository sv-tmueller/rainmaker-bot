import json
from pathlib import Path

from rainmaker.config import CONFIDENCE_FLOOR, MIN_SIGMA_F, MIN_SOURCES
from rainmaker.forecasts.base import ForecastSample, ForecastSet, SourceCoverage
from rainmaker.polymarket.markets import parse_market
from rainmaker.ranking.edge import evaluate_market
from rainmaker.store.db import connect, init_schema
from rainmaker.store.query import count_rows, get_predictions, get_run
from rainmaker.store.record import record_run

FIXTURES = Path(__file__).parent / "fixtures"


def _nyc_market():
    events = json.loads((FIXTURES / "polymarket_weather_events.json").read_text())
    return parse_market(next(e for e in events if e["id"] == "533147"))


def _forecast_set(target):
    nws = [
        ForecastSample(
            source="nws",
            model="nws",
            member=None,
            station="KLGA",
            variable="TMAX",
            target_date=target.local_date,
            lead_time_days=1,
            value_f=v,
            issued_at=None,
        )
        for v in (70.0, 71.0, 72.0)
    ]
    om = [
        ForecastSample(
            source="open-meteo",
            model="gfs_seamless",
            member=None,
            station="KLGA",
            variable="TMAX",
            target_date=target.local_date,
            lead_time_days=1,
            value_f=v,
            issued_at=None,
        )
        for v in (69.5, 70.5)
    ]
    return ForecastSet(
        target=target,
        samples=nws + om,
        coverage=[
            SourceCoverage(source="nws", ok=True, n_samples=3),
            SourceCoverage(source="open-meteo", ok=True, n_samples=2),
        ],
    )


def _evaluated():
    market = _nyc_market()
    fs = _forecast_set(market.target)
    report = evaluate_market(
        market, fs, floor=CONFIDENCE_FLOOR, min_sources=MIN_SOURCES, min_sigma=MIN_SIGMA_F
    )
    return market, fs, report


def test_record_run_persists_all_tables():
    conn = connect(":memory:")
    init_schema(conn)
    market, fs, report = _evaluated()
    record_run(
        conn,
        run_id="run-1",
        started_at="2026-05-31T10:00:00Z",
        finished_at="2026-05-31T10:00:05Z",
        status="ok",
        evaluated=[(market, fs, report)],
    )
    assert count_rows(conn, "runs") == 1
    assert count_rows(conn, "markets") == 1
    assert count_rows(conn, "prices") == len(market.buckets)
    assert count_rows(conn, "forecasts") == 2  # grouped by (source, model): nws + gfs_seamless
    assert count_rows(conn, "predictions") == len(report.outcomes)
    conn.close()


def test_round_trip_run_and_predictions():
    conn = connect(":memory:")
    init_schema(conn)
    market, fs, report = _evaluated()
    record_run(
        conn,
        run_id="run-1",
        started_at="2026-05-31T10:00:00Z",
        finished_at="2026-05-31T10:00:05Z",
        status="ok",
        evaluated=[(market, fs, report)],
    )

    run = get_run(conn, "run-1")
    assert run is not None
    assert run["status"] == "ok"
    coverage = json.loads(run["coverage"])
    assert coverage["n_markets"] == 1
    assert coverage["ok_sources"] == ["nws", "open-meteo"]

    preds = get_predictions(conn, "run-1")
    assert len(preds) == len(report.outcomes)
    assert preds[0]["edge"] >= preds[-1]["edge"]  # ordered by edge desc
    assert all(p["market_id"] == market.id for p in preds)
    assert get_run(conn, "missing") is None
    conn.close()


def test_record_market_upsert_is_idempotent():
    conn = connect(":memory:")
    init_schema(conn)
    market, fs, report = _evaluated()
    record_run(
        conn,
        run_id="run-1",
        started_at="t0",
        finished_at="t1",
        status="ok",
        evaluated=[(market, fs, report)],
    )
    record_run(
        conn,
        run_id="run-2",
        started_at="t2",
        finished_at="t3",
        status="ok",
        evaluated=[(market, fs, report)],
    )
    assert count_rows(conn, "markets") == 1  # upserted, not duplicated
    assert count_rows(conn, "runs") == 2
    assert count_rows(conn, "prices") == 2 * len(market.buckets)  # appended per run
    conn.close()


def test_record_predictions_stores_bucket():
    conn = connect(":memory:")
    init_schema(conn)
    market, fs, report = _evaluated()
    record_run(
        conn,
        run_id="run-1",
        started_at="t0",
        finished_at="t1",
        status="ok",
        evaluated=[(market, fs, report)],
    )
    rows = conn.execute("SELECT bucket FROM predictions WHERE run_id = ?", ("run-1",)).fetchall()
    conn.close()
    assert {r["bucket"] for r in rows} == {o.bucket_label for o in report.outcomes}
