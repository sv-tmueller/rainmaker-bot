import argparse
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from rainmaker.config import NWS_USER_AGENT, STATIONS, build_target
from rainmaker.forecasts.aggregate import aggregate
from rainmaker.forecasts.base import ForecastSet
from rainmaker.forecasts.nws import NwsSource
from rainmaker.forecasts.openmeteo import OpenMeteoSource


def _default_date(timezone_name: str) -> date:
    today_local = datetime.now(ZoneInfo(timezone_name)).date()
    return today_local + timedelta(days=1)


def _print_report(fs: ForecastSet) -> None:
    target = fs.target
    print(f"Target: {target.station.icao} {target.variable} {target.local_date}")
    print("Coverage:")
    for c in fs.coverage:
        status = "ok" if c.ok else f"FAILED ({c.error})"
        print(f"  {c.source:12} {status:30} samples={c.n_samples}")
    print(f"Samples ({len(fs.samples)}):")
    print(f"  {'source':12} {'model':24} {'member':>6} {'value_f':>8} {'lead':>4}")
    for s in sorted(fs.samples, key=lambda x: (x.source, x.model, x.member or 0)):
        member = "" if s.member is None else str(s.member)
        print(f"  {s.source:12} {s.model:24} {member:>6} {s.value_f:>8.1f} {s.lead_time_days:>4}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="rainmaker")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="fetch and normalize forecasts for one target")
    run.add_argument("--city", default="NYC")
    run.add_argument("--variable", default="TMAX")
    run.add_argument("--date", default=None, help="YYYY-MM-DD local; default tomorrow")
    args = parser.parse_args(argv)

    if args.command == "run":
        station = STATIONS[args.city]
        target_date = date.fromisoformat(args.date) if args.date else _default_date(station.timezone)
        target = build_target(args.city, args.variable, target_date)
        client = httpx.Client(headers={"User-Agent": NWS_USER_AGENT}, timeout=30.0)
        try:
            fs = aggregate(target, [NwsSource(client), OpenMeteoSource(client)])
        finally:
            client.close()
        _print_report(fs)
