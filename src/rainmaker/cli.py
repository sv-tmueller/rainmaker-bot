import argparse
import json
import os
import sys
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
from pydantic import ValidationError

from rainmaker.backfill import run_backfill, run_backfill_accuracy, season_window
from rainmaker.backtest import BacktestPair, backtest_real, backtest_synthetic, render_report
from rainmaker.config import (
    BACKFILL_DAYS,
    CONFIDENCE_FLOOR,
    CONFIDENCE_FLOOR_NO,
    DB_PATH,
    KALSHI_STATIONS,
    MIN_EDGE,
    MIN_SIGMA_C,
    MIN_SIGMA_F,
    MIN_SOURCES,
    PRECIP_CLIMATOLOGY_YEARS,
    PRECIP_VAR_FLOOR,
    REPORTS_DIR,
    STATIONS,
    Station,
    Target,
)
from rainmaker.domain import Market, PrecipTarget
from rainmaker.forecasts.aggregate import aggregate
from rainmaker.forecasts.base import ForecastSet
from rainmaker.forecasts.nws import NwsSource
from rainmaker.forecasts.openmeteo import OpenMeteoSource
from rainmaker.forecasts.precip import PrecipForecastSet, build_precip_forecast_set
from rainmaker.httpclient import build_client
from rainmaker.kalshi.client import discover_kalshi_markets, discover_kalshi_precip_markets
from rainmaker.pnl_backtest import backtest_pnl, render_pnl_report
from rainmaker.polymarket.client import (
    discover_markets,
    discover_precip_markets,
    fetch_closed_weather_events,
)
from rainmaker.ranking.edge import evaluate_market, evaluate_precip_market
from rainmaker.report.render import Report, render_markdown, render_terminal
from rainmaker.settle import regrade_polymarket_settlements, run_settlement
from rainmaker.settlement_divergence import (
    GhcndToIsdMapping,
    render_divergence_report,
    run_spike,
    summarise,
)
from rainmaker.store.db import connect, init_schema
from rainmaker.store.prune import prune_settled
from rainmaker.store.query import load_calibration
from rainmaker.store.record import (
    EvaluatedMarket,
    PrecipEvaluatedMarket,
    record_run,
    save_accuracy,
    save_calibration,
)
from rainmaker.tracking import (
    compute_attribution,
    compute_calibration,
    compute_clv,
    compute_pnl,
    write_snapshot,
)

SUPPORTED_VARIABLES = {"TMAX", "TMIN"}


def _sigma_floor(market: Market) -> float:
    """Return the sigma floor appropriate for the market's settlement unit."""
    return MIN_SIGMA_C if market.target.station.unit == "C" else MIN_SIGMA_F


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
    client = build_client(30.0)
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
            lead_time = (market.target.local_date - today).days
            if lead_time < 0:  # the day is over: a closed/settling market, not bettable
                print(f"skipped {market.id}: settled ({market.target.local_date})")
                continue
            forecast_set = _forecast_for(market.target, client)
            calibration = load_calibration(
                conn, market.target.station.icao, market.target.variable, lead_time
            )
            report = evaluate_market(
                market,
                forecast_set,
                floor=CONFIDENCE_FLOOR,
                floor_no=CONFIDENCE_FLOOR_NO,
                min_sources=MIN_SOURCES,
                min_sigma=_sigma_floor(market),
                min_edge=MIN_EDGE,
                calibration=calibration,
            )
            evaluated.append((market, forecast_set, report))

        # Kalshi is the secondary venue: its outage must never abort the run.
        try:
            kalshi_markets = discover_kalshi_markets(client)
        except httpx.HTTPError as exc:
            print(f"Kalshi discovery failed, continuing: {exc}", file=sys.stderr)
            kalshi_markets = []
        for market in kalshi_markets:
            if market.target.variable not in SUPPORTED_VARIABLES:
                print(f"skipped {market.id}: unsupported variable {market.target.variable}")
                continue
            lead_time = (market.target.local_date - today).days
            if lead_time < 0:  # the day is over: a closed/settling market, not bettable
                print(f"skipped {market.id}: settled ({market.target.local_date})")
                continue
            forecast_set = _forecast_for(market.target, client)
            calibration = load_calibration(
                conn, market.target.station.icao, market.target.variable, lead_time
            )
            report = evaluate_market(
                market,
                forecast_set,
                floor=CONFIDENCE_FLOOR,
                floor_no=CONFIDENCE_FLOOR_NO,
                min_sources=MIN_SOURCES,
                min_sigma=_sigma_floor(market),
                min_edge=MIN_EDGE,
                calibration=calibration,
            )
            evaluated.append((market, forecast_set, report))

        precip_markets = list(discover_precip_markets(client))
        # Kalshi rain is the secondary venue: its outage must never abort the run.
        try:
            precip_markets += discover_kalshi_precip_markets(client)
        except httpx.HTTPError as exc:
            print(f"Kalshi precip discovery failed, continuing: {exc}", file=sys.stderr)
        precip_evaluated: list[PrecipEvaluatedMarket] = []
        for precip_market in precip_markets:
            if precip_market.target.settlement_date < today:  # month over: not bettable
                day = precip_market.target.settlement_date
                print(f"skipped {precip_market.id}: settled ({day})")
                continue
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


def _distinct_stations() -> list[Station]:
    """Every settlement station across venues, deduped by icao (the calibration key).

    Polymarket and Kalshi share a station for some cities (Miami, LA, Austin) and
    differ for others (NYC: LaGuardia vs Central Park; Chicago: O'Hare vs Midway).
    Deduping by icao fits each physical station once; calibration is keyed by icao,
    so a shared station serves both venues.
    """
    out: dict[str, Station] = {}
    for station in (*STATIONS.values(), *KALSHI_STATIONS.values()):
        out.setdefault(station.icao, station)
    return list(out.values())


def _backfill_stations(city: str) -> list[Station]:
    """Stations to calibrate: all of them for 'all', else every venue's station
    for the named city (NYC and Chicago each resolve to two)."""
    stations = _distinct_stations()
    if city == "all":
        return sorted(stations, key=lambda s: s.icao)
    return [s for s in stations if s.city == city]


def _backfill(city: str, variable: str, days: int, leads: tuple[int, ...], db_path: str) -> None:
    stations = _backfill_stations(city)
    today = _today()
    window = season_window(today, days)
    if window is None:
        # First day of a new meteorological season: no in-season data yet.
        # Skip the fit; apply_calibration falls back to uncalibrated widening.
        print(
            f"skipping backfill: today ({today}) is the first day of a new season, "
            "no in-season data available yet",
            file=sys.stderr,
        )
        return
    start, end = window
    if "://" not in db_path:  # a Postgres DSN has no local parent dir to create
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    client = build_client(60.0)
    label = _db_label(db_path)
    succeeded = 0
    try:
        init_schema(conn)
        for station in stations:
            now = _now_iso()
            city_ok = False
            if 1 in leads:  # lead 1 keeps the calibration + accuracy fit
                try:
                    cal, acc = run_backfill(station, variable, 1, start, end, client)
                except (httpx.HTTPError, ValueError) as exc:
                    if isinstance(exc, ValidationError):
                        raise  # schema bug, not a data gap; fail loud
                    print(f"{station.city}: backfill failed: {exc}", file=sys.stderr)
                else:
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
                    print(
                        f"calibrated {cal.station} {cal.variable} lead={cal.lead_time}: "
                        f"bias={cal.bias:+.2f}F var_a={cal.var_a:.3f} var_b={cal.var_b:.3f} "
                        f"mae={acc.mae_f:.2f}F n={cal.n_samples} -> {label}"
                    )
                    city_ok = True
            higher = tuple(lead for lead in leads if lead != 1)
            if higher:  # higher leads are accuracy-only (no calibration fit)
                try:
                    accs = run_backfill_accuracy(station, variable, higher, start, end, client)
                except (httpx.HTTPError, ValueError) as exc:
                    if isinstance(exc, ValidationError):
                        raise  # schema bug, not a data gap; fail loud
                    print(f"{station.city}: accuracy backfill failed: {exc}", file=sys.stderr)
                else:
                    for lead, acc in sorted(accs.items()):
                        save_accuracy(
                            conn,
                            station=station.icao,
                            city=station.city,
                            variable=variable,
                            lead_time=lead,
                            kind="backtest",
                            accuracy=acc,
                            updated_at=now,
                        )
                        print(
                            f"accuracy {station.icao} {variable} lead={lead}: "
                            f"mae={acc.mae_f:.2f}F bias={acc.bias_f:+.2f}F n={acc.n} -> {label}"
                        )
                        city_ok = True
            if city_ok:
                succeeded += 1
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
    client = build_client(60.0)
    synthetic: dict[str, BacktestPair] = {}
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


def _backtest_pnl(
    city: str,
    days: int,
    leads: tuple[int, ...],
    spread: float,
    reports_dir: str,
    ask_source: str = "mid",
    max_edge: float | None = None,
    max_p_win: float | None = None,
) -> None:
    end = _today() - timedelta(days=1)  # actuals lag real-time; stop at yesterday
    start = end - timedelta(days=days)
    client = build_client(60.0)
    try:
        try:
            events = fetch_closed_weather_events(client)
        except httpx.HTTPError as exc:
            print(f"Polymarket unavailable, aborting: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
        result = backtest_pnl(
            events,
            client,
            on_or_after=start,
            leads=leads,
            city=None if city == "all" else city,
            spread=spread,
            ask_source=ask_source,  # type: ignore[arg-type]
            max_edge=max_edge,
            max_p_win=max_p_win,
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
    client = build_client(60.0)
    try:
        init_schema(conn)
        settled, waiting = run_settlement(conn, client, today, settled_at)
    finally:
        client.close()
        conn.close()
    print(
        f"settled {settled} market(s); {waiting} waiting on ASOS/NCEI data -> {_db_label(db_path)}"
    )


def _regrade(db_path: str) -> None:
    """Re-settle existing Polymarket TMAX/TMIN outcomes using ASOS and re-grade predictions."""
    regraded_at = _now_iso()
    if "://" not in db_path:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    client = build_client(60.0)
    try:
        init_schema(conn)
        regraded = regrade_polymarket_settlements(conn, client, regraded_at)
    finally:
        client.close()
        conn.close()
    print(f"regraded {regraded} Polymarket market(s) onto ASOS -> {_db_label(db_path)}")


def _prune(db_path: str) -> None:
    if "://" not in db_path:  # a Postgres DSN has no local parent dir to create
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        init_schema(conn)
        deleted = prune_settled(conn)
        conn.commit()
    finally:
        conn.close()
    print(f"pruned {deleted} redundant intraday row(s) -> {_db_label(db_path)}")


def _track(db_path: str) -> None:
    if "://" not in db_path:  # a Postgres DSN has no local parent dir to create
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        init_schema(conn)
        pnl = compute_pnl(conn)
        cal = compute_calibration(conn)
        by_venue = {v: compute_pnl(conn, venue=v) for v in ("polymarket", "kalshi")}
    finally:
        conn.close()
    print(
        f"P&L: {pnl['n_bets']} bets, {pnl['wins']}-{pnl['losses']}, "
        f"total {pnl['total_pnl']:+.2f}u, ROI {pnl['roi']:+.1%}"
    )
    for venue, vp in by_venue.items():
        if vp["n_bets"] == 0:
            continue  # only show a venue that actually has settled bets
        print(
            f"  {venue}: {vp['n_bets']} bets, {vp['wins']}-{vp['losses']}, "
            f"total {vp['total_pnl']:+.2f}u, ROI {vp['roi']:+.1%}"
        )
    brier = "n/a" if cal["brier"] is None else f"{cal['brier']:.3f}"
    hit = "n/a" if cal["hit_rate"] is None else f"{cal['hit_rate']:.0%}"
    print(f"calibration: Brier {brier}, recommended hit rate {hit} (n={cal['n']})")


def _attribution(db_path: str) -> None:
    if "://" not in db_path:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        init_schema(conn)
        result = compute_attribution(conn)
    finally:
        conn.close()
    dim_labels = {
        "city": "City",
        "venue": "Venue",
        "variable": "Variable",
        "lead": "Lead (days)",
        "edge": "Edge bucket",
        "p_win": "p_win bucket",
    }
    for dim, label in dim_labels.items():
        segs = result[dim]
        if not segs:
            continue
        print(f"\n--- {label} ---")
        print(
            f"{'Segment':<20} {'n':>5} {'W':>5} {'L':>5} {'Win%':>7} "
            f"{'CI_lo':>7} {'CI_hi':>7} {'ROI':>8}"
        )
        for s in segs:
            win_pct = f"{s['win_pct']:.0%}" if s["n"] else "n/a"
            lo = f"{s['wilson_lo']:.3f}"
            hi = f"{s['wilson_hi']:.3f}"
            roi = f"{s['roi']:+.1%}"
            print(
                f"{s['segment']:<20} {s['n']:>5} {s['wins']:>5} {s['losses']:>5} "
                f"{win_pct:>7} {lo:>7} {hi:>7} {roi:>8}"
            )


def _clv(db_path: str) -> None:
    if "://" not in db_path:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    client = build_client(30.0)
    try:
        init_schema(conn)
        result = compute_clv(conn, client)
    finally:
        client.close()
        conn.close()
    n_bets = result["n_bets"]
    n_clv = result["n_clv"]
    n_coincident = result["n_coincident"]
    mean_clv = result["mean_clv"]
    clv_str = "n/a" if mean_clv is None else f"{mean_clv:+.4f}"
    print(f"CLV: mean {clv_str} ({n_clv}/{n_bets} bets with closing price)")
    print(f"  coincident (CLV~0): {n_coincident}/{n_clv} bets")
    dim_labels = {
        "city": "City",
        "venue": "Venue",
        "variable": "Variable",
        "lead": "Lead (days)",
        "edge": "Edge bucket",
        "p_win": "p_win bucket",
    }
    for dim, label in dim_labels.items():
        segs = result["by_segment"].get(dim, [])
        if not segs:
            continue
        print(f"\n--- {label} ---")
        print(f"{'Segment':<20} {'n':>5} {'mean CLV':>10}")
        for s in segs:
            clv_val = f"{s['mean_clv']:+.4f}"
            print(f"{s['segment']:<20} {s['n']:>5} {clv_val:>10}")


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


def _settle_divergence(pages: int, reports_dir: str) -> None:
    """Fetch closed Polymarket events, run Arm A (GHCND) and Arm B (Mesonet ASOS), write report."""
    client = build_client(60.0)
    mapping = GhcndToIsdMapping.default()
    try:
        try:
            events = fetch_closed_weather_events(client, max_pages=pages)
        except httpx.HTTPError as exc:
            print(f"Polymarket unavailable, aborting: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
        rows = run_spike(events, client, mapping)
    finally:
        client.close()

    if not rows:
        print(
            "no resolved US-city temperature markets found in the sampled window",
            file=sys.stderr,
        )
        raise SystemExit(1)

    city_results = summarise(rows)
    run_date = _today().isoformat()
    md = render_divergence_report(rows, city_results, run_date)
    print(md)
    out = Path(reports_dir)
    out.mkdir(parents=True, exist_ok=True)
    report_path = out / "settlement-divergence-2026-06.md"
    report_path.write_text(md)
    n_rows = len(rows)
    total_n = sum(r.n for r in city_results.values())
    total_ncei = sum(r.ncei_flips for r in city_results.values())
    total_asos = sum(r.asos_flips for r in city_results.values())
    print(
        f"wrote {report_path}; "
        f"{n_rows} events sampled, {total_n} both-armed; "
        f"NCEI flips: {total_ncei}/{total_n}, ASOS flips: {total_asos}/{total_n}"
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
    backfill.add_argument(
        "--days", type=int, default=BACKFILL_DAYS, help="history window length in days"
    )
    backfill.add_argument(
        "--leads",
        default="1,2,3",
        help=(
            "comma-separated leads in days; lead 1 fits calibration, higher leads are accuracy-only"
        ),
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
        "--spread",
        type=float,
        default=0.0,
        help="bid/ask spread charged as an ask haircut (ask = mid + spread/2)",
    )
    btp.add_argument(
        "--asks",
        choices=["mid", "trades"],
        default="mid",
        help="ask price source: mid (default, uses token mid) or trades (uses real BUY fills)",
    )
    btp.add_argument(
        "--max-edge",
        type=float,
        default=None,
        help="upper edge cap: exclude recommended bets with edge above this value",
    )
    btp.add_argument(
        "--max-p-win",
        type=float,
        default=None,
        help="upper confidence cap: exclude recommended bets with p_win above this value",
    )
    btp.add_argument(
        "--reports-dir", default=REPORTS_DIR, help="directory for the P/L backtest report"
    )

    sdiv = sub.add_parser(
        "settle-divergence",
        help="spike: measure NCEI GHCND vs ASOS/ISD settlement divergence on closed markets",
    )
    sdiv.add_argument(
        "--pages",
        type=int,
        default=12,
        help="max Gamma pages to fetch (100 events/page, most recent first)",
    )
    sdiv.add_argument(
        "--reports-dir",
        default=REPORTS_DIR,
        help="directory for the divergence report",
    )

    settle = sub.add_parser("settle", help="settle past markets against NOAA actuals")
    settle.add_argument("--db", default=DB_PATH, help="SQLite database path")

    regrade = sub.add_parser(
        "regrade",
        help="re-settle existing Polymarket TMAX/TMIN outcomes using ASOS (one-time migration)",
    )
    regrade.add_argument("--db", default=DB_PATH, help="SQLite database path")

    prune = sub.add_parser("prune", help="delete redundant intraday rows for settled markets")
    prune.add_argument("--db", default=DB_PATH, help="SQLite database path")

    track = sub.add_parser("track", help="report P&L and calibration over settled markets")
    track.add_argument("--db", default=DB_PATH, help="SQLite database path")

    attr = sub.add_parser(
        "attribution", help="per-segment P&L breakdown by city/venue/variable/lead/edge/p_win"
    )
    attr.add_argument("--db", default=DB_PATH, help="SQLite database path")

    clv_cmd = sub.add_parser(
        "clv",
        help="closing-line value: how well advised prices compare to the market close",
    )
    clv_cmd.add_argument("--db", default=DB_PATH, help="SQLite database path")

    snapshot = sub.add_parser("snapshot", help="write a daily P&L/calibration snapshot row")
    snapshot.add_argument("--db", default=DB_PATH, help="SQLite database path")

    args = parser.parse_args(argv)

    if args.command == "backtest":
        _backtest(args.city, args.days, args.width, args.span, args.reports_dir, args.real)
        return
    if args.command == "backtest-pnl":
        _backtest_pnl(
            args.city,
            args.days,
            _parse_leads(args.leads),
            args.spread,
            args.reports_dir,
            args.asks,
            args.max_edge,
            args.max_p_win,
        )
        return
    if args.command == "settle-divergence":
        _settle_divergence(args.pages, args.reports_dir)
        return

    db = _datastore(args.db)
    if args.command == "run":
        _run(args.reports_dir, db)
    elif args.command == "backfill":
        _backfill(args.city, args.variable, args.days, _parse_leads(args.leads), db)
    elif args.command == "settle":
        _settle(db)
    elif args.command == "regrade":
        _regrade(db)
    elif args.command == "prune":
        _prune(db)
    elif args.command == "track":
        _track(db)
    elif args.command == "attribution":
        _attribution(db)
    elif args.command == "clv":
        _clv(db)
    elif args.command == "snapshot":
        _snapshot(db)
