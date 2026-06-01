import argparse
import json
import sys
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

import httpx

from rainmaker.config import (
    CONFIDENCE_FLOOR,
    DB_PATH,
    MIN_SIGMA_F,
    MIN_SOURCES,
    NWS_USER_AGENT,
    REPORTS_DIR,
    Target,
)
from rainmaker.forecasts.aggregate import aggregate
from rainmaker.forecasts.base import ForecastSet
from rainmaker.forecasts.nws import NwsSource
from rainmaker.forecasts.openmeteo import OpenMeteoSource
from rainmaker.polymarket.client import discover_markets
from rainmaker.ranking.edge import evaluate_market
from rainmaker.report.render import Report, render_markdown, render_terminal
from rainmaker.store.db import connect, init_schema
from rainmaker.store.query import load_calibration
from rainmaker.store.record import EvaluatedMarket, record_run

SUPPORTED_VARIABLES = {"TMAX"}


def _today() -> date:
    return datetime.now(UTC).date()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _new_run_id() -> str:
    return str(uuid.uuid4())


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
        print(f"wrote {paths[0]} and {paths[1]}; recorded run to {db_path}")
    finally:
        client.close()
        conn.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="rainmaker")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="produce the daily edge-ranked report")
    run.add_argument(
        "--reports-dir", default=REPORTS_DIR, help="directory for dated md/json output"
    )
    run.add_argument("--db", default=DB_PATH, help="SQLite database path for run persistence")
    args = parser.parse_args(argv)

    if args.command == "run":
        _run(args.reports_dir, args.db)
