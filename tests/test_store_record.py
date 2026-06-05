import json
from pathlib import Path

import pytest

from rainmaker.config import CONFIDENCE_FLOOR, MIN_EDGE, MIN_SIGMA_F, MIN_SOURCES
from rainmaker.forecasts.base import ForecastSample, ForecastSet, SourceCoverage
from rainmaker.polymarket.markets import parse_market
from rainmaker.probability.calibration import Accuracy
from rainmaker.ranking.edge import evaluate_market
from rainmaker.store.db import connect, init_schema
from rainmaker.store.query import count_rows, get_predictions, get_run
from rainmaker.store.record import record_run, save_accuracy

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
        market,
        fs,
        floor=CONFIDENCE_FLOOR,
        min_sources=MIN_SOURCES,
        min_sigma=MIN_SIGMA_F,
        min_edge=MIN_EDGE,
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
    # one YES price per bucket, plus a NO price where the bucket has a NO ask
    expected_prices = sum(1 + (1 if b.no_ask is not None else 0) for b in market.buckets)
    assert count_rows(conn, "prices") == expected_prices
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
    expected_prices = sum(1 + (1 if b.no_ask is not None else 0) for b in market.buckets)
    assert count_rows(conn, "prices") == 2 * expected_prices  # appended per run
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


def test_accuracy_save_and_upsert_round_trip():
    conn = connect(":memory:")
    init_schema(conn)
    save_accuracy(
        conn,
        station="KSEA",
        city="Seattle",
        variable="TMAX",
        lead_time=1,
        kind="backtest",
        accuracy=Accuracy(n=60, mae_f=2.1, bias_f=-0.4),
        updated_at="t0",
    )
    row = conn.execute("SELECT * FROM forecast_accuracy").fetchone()
    assert (row["station"], row["city"], row["kind"]) == ("KSEA", "Seattle", "backtest")
    assert row["n"] == 60
    assert row["mae_f"] == pytest.approx(2.1)
    assert row["bias_f"] == pytest.approx(-0.4)

    # same key again -> upserted, not duplicated
    save_accuracy(
        conn,
        station="KSEA",
        city="Seattle",
        variable="TMAX",
        lead_time=1,
        kind="backtest",
        accuracy=Accuracy(n=61, mae_f=2.0, bias_f=-0.3),
        updated_at="t1",
    )
    rows = conn.execute("SELECT * FROM forecast_accuracy").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0]["n"] == 61
    assert rows[0]["mae_f"] == pytest.approx(2.0)  # updated
    assert rows[0]["bias_f"] == pytest.approx(-0.3)  # updated
    assert rows[0]["updated_at"] == "t1"  # updated
