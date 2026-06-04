import argparse
import json
import os
import sys
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
from pydantic import ValidationError

from rainmaker.backfill import run_backfill
from rainmaker.config import (
    CONFIDENCE_FLOOR,
    DB_PATH,
    MIN_EDGE,
    MIN_SIGMA_F,
    MIN_SOURCES,
    NWS_USER_AGENT,
    REPORTS_DIR,
    STATIONS,
    Target,
)
from rainmaker.forecasts.aggregate import aggregate
from rainmaker.forecasts.base import ForecastSet
from rainmaker.forecasts.nws import NwsSource
from rainmaker.forecasts.openmeteo import OpenMeteoSource
from rainmaker.polymarket.client import discover_markets
from rainmaker.ranking.edge import evaluate_market
from rainmaker.report.render import Report, render_markdown, render_terminal
from rainmaker.settle import run_settlement
from rainmaker.store.db import connect, init_schema
from rainmaker.store.query import load_calibration
from rainmaker.store.record import EvaluatedMarket, record_run, save_accuracy, save_calibration
from rainmaker.tracking import compute_calibration, compute_pnl, write_snapshot

SUPPORTED_VARIABLES = {"TMAX"}


def _today() -> date:
    return datetime.now(UTC).date()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _new_run_id() -> str:
    return str(uuid.uuid4())


def _datastore(default: str) -> str:
    """Use the Postgres DSN from the environment when set, else the SQLite path."""
    return os.environ.get("DATABASE_URL") or default


def _db_label(db_path: str) -> str:
    """Safe display name for the datastore: never echo DSN credentials."""
    return "postgres" if db_path.startswith(("postgres://", "postgresql://")) else db_path


def _forecast_for(target: Target, client: httpx.Client) -> ForecastSet:
    return aggregate(target, [NwsSource(client), OpenMeteoSource(client)])


def _write_reports(report: Report, reports_dir: str) -> list[Path]:
    out = Path(reports_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = report.run_date.isoformat()
    md_path = out / f"{stamp}.md"
    json_path = out / f"{stamp}.json"
    md_path.write_text(render_markdown(report))
    json_path.write_text(json.dumps(report.model_dump(mode="json"), indent=2))
    return [md_path, json_path]


def _run(reports_dir: str, db_path: str) -> None:
    started_at = _now_iso()
    today = _today()
    if "://" not in db_path:  # a Postgres DSN has no local parent dir to create
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    init_schema(conn)
    client = httpx.Client(headers={"User-Agent": NWS_USER_AGENT}, timeout=30.0)
    try:
        try:
            markets = discover_markets(client)
        except httpx.HTTPError as exc:
            print(f"Polymarket unavailable, aborting: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc

        evaluated: list[EvaluatedMarket] = []
        for market in markets:
            if market.target.variable not in SUPPORTED_VARIABLES:
                print(f"skipped {market.id}: unsupported variable {market.target.variable}")
                continue
            forecast_set = _forecast_for(market.target, client)
            lead_time = (market.target.local_date - today).days
            calibration = load_calibration(
                conn, market.target.station.icao, market.target.variable, lead_time
            )
            report = evaluate_market(
                market,
                forecast_set,
                floor=CONFIDENCE_FLOOR,
                min_sources=MIN_SOURCES,
                min_sigma=MIN_SIGMA_F,
                min_edge=MIN_EDGE,
                calibration=calibration,
            )
            evaluated.append((market, forecast_set, report))
        finished_at = _now_iso()

        daily_report = Report(run_date=today, markets=[r for _, _, r in evaluated])
        print(render_terminal(daily_report))
        paths = _write_reports(daily_report, reports_dir)
        record_run(
            conn,
            run_id=_new_run_id(),
            started_at=started_at,
            finished_at=finished_at,
            status="ok",
            evaluated=evaluated,
        )
        print(f"wrote {paths[0]} and {paths[1]}; recorded run to {_db_label(db_path)}")
    finally:
        client.close()
        conn.close()


def _backfill(city: str, variable: str, days: int, lead: int, db_path: str) -> None:
    cities = sorted(STATIONS) if city == "all" else [city]
    end = _today() - timedelta(days=1)  # actuals lag real-time; stop at yesterday
    start = end - timedelta(days=days)
    if "://" not in db_path:  # a Postgres DSN has no local parent dir to create
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    client = httpx.Client(headers={"User-Agent": NWS_USER_AGENT}, timeout=60.0)
    succeeded = 0
    try:
        init_schema(conn)
        for name in cities:
            station = STATIONS[name]
            try:
                cal, acc = run_backfill(station, variable, lead, start, end, client)
            except (httpx.HTTPError, ValueError) as exc:
                if isinstance(exc, ValidationError):
                    raise  # schema bug, not a data gap; fail loud
                print(f"{name}: backfill failed: {exc}", file=sys.stderr)
                continue
            now = _now_iso()
            save_calibration(conn, cal, updated_at=now)
            save_accuracy(
                conn,
                station=cal.station,
                city=station.city,
                variable=cal.variable,
                lead_time=cal.lead_time,
                kind="backtest",
                accuracy=acc,
                updated_at=now,
            )
            succeeded += 1
            print(
                f"calibrated {cal.station} {cal.variable} lead={cal.lead_time}: "
                f"bias={cal.bias:+.2f}F spread_scale={cal.spread_scale:.2f} "
                f"mae={acc.mae_f:.2f}F n={cal.n_samples} -> {_db_label(db_path)}"
            )
    finally:
        client.close()
        conn.close()
    if succeeded == 0:
        raise SystemExit(1)


def _settle(db_path: str) -> None:
    today = _today()
    settled_at = _now_iso()
    if "://" not in db_path:  # a Postgres DSN has no local parent dir to create
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    client = httpx.Client(headers={"User-Agent": NWS_USER_AGENT}, timeout=60.0)
    try:
        init_schema(conn)
        settled, waiting = run_settlement(conn, client, today, settled_at)
    finally:
        client.close()
        conn.close()
    print(f"settled {settled} market(s); {waiting} waiting on NCEI data -> {_db_label(db_path)}")


def _track(db_path: str) -> None:
    if "://" not in db_path:  # a Postgres DSN has no local parent dir to create
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        init_schema(conn)
        pnl = compute_pnl(conn)
        cal = compute_calibration(conn)
    finally:
        conn.close()
    print(
        f"P&L: {pnl['n_bets']} bets, {pnl['wins']}-{pnl['losses']}, "
        f"total {pnl['total_pnl']:+.2f}u, ROI {pnl['roi']:+.1%}"
    )
    brier = "n/a" if cal["brier"] is None else f"{cal['brier']:.3f}"
    hit = "n/a" if cal["hit_rate"] is None else f"{cal['hit_rate']:.0%}"
    print(f"calibration: Brier {brier}, recommended hit rate {hit} (n={cal['n']})")


def _snapshot(db_path: str) -> None:
    on_date = _today().isoformat()
    if "://" not in db_path:  # a Postgres DSN has no local parent dir to create
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        init_schema(conn)
        result = write_snapshot(conn, on_date, _now_iso())
    finally:
        conn.close()
    p = result["pnl"]
    print(
        f"snapshot {on_date}: {p['n_bets']} bets, total {p['total_pnl']:+.2f}u "
        f"-> {_db_label(db_path)}"
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="rainmaker")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="produce the daily edge-ranked report")
    run.add_argument(
        "--reports-dir", default=REPORTS_DIR, help="directory for dated md/json output"
    )
    run.add_argument("--db", default=DB_PATH, help="SQLite database path for run persistence")

    backfill = sub.add_parser(
        "backfill", help="fit calibration from historical forecasts vs actuals"
    )
    backfill.add_argument(
        "--city", default="NYC", help="city key from the station registry, or 'all'"
    )
    backfill.add_argument("--variable", default="TMAX")
    backfill.add_argument("--days", type=int, default=60, help="history window length in days")
    backfill.add_argument(
        "--lead", type=int, default=1, help="forecast lead time the archive represents"
    )
    backfill.add_argument("--db", default=DB_PATH, help="SQLite database path")

    settle = sub.add_parser("settle", help="settle past markets against NOAA actuals")
    settle.add_argument("--db", default=DB_PATH, help="SQLite database path")

    track = sub.add_parser("track", help="report P&L and calibration over settled markets")
    track.add_argument("--db", default=DB_PATH, help="SQLite database path")

    snapshot = sub.add_parser("snapshot", help="write a daily P&L/calibration snapshot row")
    snapshot.add_argument("--db", default=DB_PATH, help="SQLite database path")

    args = parser.parse_args(argv)

    db = _datastore(args.db)
    if args.command == "run":
        _run(args.reports_dir, db)
    elif args.command == "backfill":
        _backfill(args.city, args.variable, args.days, args.lead, db)
    elif args.command == "settle":
        _settle(db)
    elif args.command == "track":
        _track(db)
    elif args.command == "snapshot":
        _snapshot(db)
