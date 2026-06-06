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
from rainmaker.backtest import BacktestResult, backtest_real, backtest_synthetic, render_report
from rainmaker.config import (
    CONFIDENCE_FLOOR,
    DB_PATH,
    MIN_EDGE,
    MIN_SIGMA_F,
    MIN_SOURCES,
    NWS_USER_AGENT,
    PRECIP_CLIMATOLOGY_YEARS,
    PRECIP_VAR_FLOOR,
    REPORTS_DIR,
    STATIONS,
    Target,
)
from rainmaker.forecasts.aggregate import aggregate
from rainmaker.forecasts.base import ForecastSet
from rainmaker.forecasts.nws import NwsSource
from rainmaker.forecasts.openmeteo import OpenMeteoSource
from rainmaker.forecasts.precip import PrecipForecastSet, build_precip_forecast_set
from rainmaker.pnl_backtest import backtest_pnl, render_pnl_report
from rainmaker.polymarket.client import (
    discover_markets,
    discover_precip_markets,
    fetch_closed_weather_events,
)
from rainmaker.polymarket.precip_markets import PrecipTarget
from rainmaker.ranking.edge import evaluate_market, evaluate_precip_market
from rainmaker.report.render import Report, render_markdown, render_terminal
from rainmaker.settle import run_settlement
from rainmaker.store.db import connect, init_schema
from rainmaker.store.query import load_calibration
from rainmaker.store.record import (
    EvaluatedMarket,
    PrecipEvaluatedMarket,
    record_run,
    save_accuracy,
    save_calibration,
)
from rainmaker.tracking import compute_calibration, compute_pnl, write_snapshot

SUPPORTED_VARIABLES = {"TMAX", "TMIN"}


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


def _precip_forecast_for(
    target: PrecipTarget, today: date, client: httpx.Client
) -> PrecipForecastSet:
    return build_precip_forecast_set(
        target,
        today=today,
        client=client,
        var_floor=PRECIP_VAR_FLOOR,
        lookback_years=PRECIP_CLIMATOLOGY_YEARS,
    )


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

        precip_evaluated: list[PrecipEvaluatedMarket] = []
        for precip_market in discover_precip_markets(client):
            precip_set = _precip_forecast_for(precip_market.target, today, client)
            precip_report = evaluate_precip_market(
                precip_market,
                precip_set,
                floor=CONFIDENCE_FLOOR,
                min_sources=MIN_SOURCES,
                min_edge=MIN_EDGE,
                var_floor=PRECIP_VAR_FLOOR,
            )
            precip_evaluated.append((precip_market, precip_report))
        finished_at = _now_iso()

        daily_report = Report(
            run_date=today,
            markets=[r for _, _, r in evaluated] + [r for _, r in precip_evaluated],
        )
        print(render_terminal(daily_report))
        paths = _write_reports(daily_report, reports_dir)
        record_run(
            conn,
            run_id=_new_run_id(),
            started_at=started_at,
            finished_at=finished_at,
            status="ok",
            evaluated=evaluated,
            precip_evaluated=precip_evaluated,
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


def _backtest(
    city: str, days: int, width: int, span: int, reports_dir: str, include_real: bool
) -> None:
    cities = sorted(STATIONS) if city == "all" else [city]
    end = _today() - timedelta(days=1)  # actuals lag real-time; stop at yesterday
    start = end - timedelta(days=days)
    client = httpx.Client(headers={"User-Agent": NWS_USER_AGENT}, timeout=60.0)
    synthetic: dict[str, BacktestResult] = {}
    real = None
    try:
        for name in cities:
            try:
                res = backtest_synthetic(
                    STATIONS[name], "TMAX", start, end, client, width=width, span=span
                )
            except httpx.HTTPError as exc:
                print(f"{name}: backtest failed: {exc}", file=sys.stderr)
                continue
            if res is not None:
                synthetic[name] = res
        if include_real:
            try:
                events = fetch_closed_weather_events(client)
                real = backtest_real(events, client, on_or_after=start)
            except httpx.HTTPError as exc:
                print(f"real-market check failed: {exc}", file=sys.stderr)
    finally:
        client.close()
    if not synthetic:
        print("no backtest data over the requested window", file=sys.stderr)
        raise SystemExit(1)

    md, payload = render_report(synthetic, real)
    print(md)
    out = Path(reports_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = _today().isoformat()
    (out / f"backtest-{stamp}.md").write_text(md)
    (out / f"backtest-{stamp}.json").write_text(json.dumps(payload, indent=2))
    print(f"wrote backtest-{stamp}.md and backtest-{stamp}.json to {reports_dir}")


def _parse_leads(spec: str) -> tuple[int, ...]:
    return tuple(int(part) for part in spec.split(",") if part.strip() != "")


def _backtest_pnl(city: str, days: int, leads: tuple[int, ...], reports_dir: str) -> None:
    end = _today() - timedelta(days=1)  # actuals lag real-time; stop at yesterday
    start = end - timedelta(days=days)
    client = httpx.Client(headers={"User-Agent": NWS_USER_AGENT}, timeout=60.0)
    try:
        try:
            events = fetch_closed_weather_events(client)
        except httpx.HTTPError as exc:
            print(f"Polymarket unavailable, aborting: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
        result = backtest_pnl(
            events, client, on_or_after=start, leads=leads, city=None if city == "all" else city
        )
    finally:
        client.close()
    if result is None:
        print("no P/L backtest data over the requested window", file=sys.stderr)
        raise SystemExit(1)

    md, payload = render_pnl_report(result)
    print(md)
    out = Path(reports_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = _today().isoformat()
    (out / f"pnl-backtest-{stamp}.md").write_text(md)
    (out / f"pnl-backtest-{stamp}.json").write_text(json.dumps(payload, indent=2))
    print(f"wrote pnl-backtest-{stamp}.md and pnl-backtest-{stamp}.json to {reports_dir}")


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

    bt = sub.add_parser("backtest", help="backtest forecast calibration and win-rate over history")
    bt.add_argument("--city", default="all", help="city key from the station registry, or 'all'")
    bt.add_argument("--days", type=int, default=730, help="history window length in days")
    bt.add_argument("--width", type=int, default=2, help="synthetic bucket width in degrees F")
    bt.add_argument("--span", type=int, default=10, help="degrees F covered each side of center")
    bt.add_argument("--reports-dir", default=REPORTS_DIR, help="directory for the backtest report")
    bt.add_argument(
        "--real",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="include the real closed-market reality check",
    )

    btp = sub.add_parser(
        "backtest-pnl", help="backtest hypothetical betting P/L over closed markets"
    )
    btp.add_argument("--city", default="all", help="city key from the station registry, or 'all'")
    btp.add_argument("--days", type=int, default=730, help="history window length in days")
    btp.add_argument("--leads", default="0,1,2,3", help="comma-separated forecast leads in days")
    btp.add_argument(
        "--reports-dir", default=REPORTS_DIR, help="directory for the P/L backtest report"
    )

    settle = sub.add_parser("settle", help="settle past markets against NOAA actuals")
    settle.add_argument("--db", default=DB_PATH, help="SQLite database path")

    track = sub.add_parser("track", help="report P&L and calibration over settled markets")
    track.add_argument("--db", default=DB_PATH, help="SQLite database path")

    snapshot = sub.add_parser("snapshot", help="write a daily P&L/calibration snapshot row")
    snapshot.add_argument("--db", default=DB_PATH, help="SQLite database path")

    args = parser.parse_args(argv)

    if args.command == "backtest":
        _backtest(args.city, args.days, args.width, args.span, args.reports_dir, args.real)
        return
    if args.command == "backtest-pnl":
        _backtest_pnl(args.city, args.days, _parse_leads(args.leads), args.reports_dir)
        return

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
