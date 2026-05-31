import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

from rainmaker.config import (
    CONFIDENCE_FLOOR,
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

SUPPORTED_VARIABLES = {"TMAX"}


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


def _run(reports_dir: str) -> None:
    client = httpx.Client(headers={"User-Agent": NWS_USER_AGENT}, timeout=30.0)
    try:
        try:
            markets = discover_markets(client)
        except httpx.HTTPError as exc:
            print(f"Polymarket unavailable, aborting: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc

        market_reports = []
        for market in markets:
            if market.target.variable not in SUPPORTED_VARIABLES:
                print(f"skipped {market.id}: unsupported variable {market.target.variable}")
                continue
            forecast_set = _forecast_for(market.target, client)
            market_reports.append(
                evaluate_market(
                    market,
                    forecast_set,
                    floor=CONFIDENCE_FLOOR,
                    min_sources=MIN_SOURCES,
                    min_sigma=MIN_SIGMA_F,
                )
            )
    finally:
        client.close()

    run_date = market_reports[0].settlement_date if market_reports else datetime.now(UTC).date()
    report = Report(run_date=run_date, markets=market_reports)
    print(render_terminal(report))
    paths = _write_reports(report, reports_dir)
    print(f"wrote {paths[0]} and {paths[1]}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="rainmaker")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="produce the daily edge-ranked report")
    run.add_argument(
        "--reports-dir", default=REPORTS_DIR, help="directory for dated md/json output"
    )
    args = parser.parse_args(argv)

    if args.command == "run":
        _run(args.reports_dir)
