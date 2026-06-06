# tests/test_cli.py
from datetime import date

import httpx
import pytest

from rainmaker import cli
from rainmaker.config import build_target
from rainmaker.forecasts.base import ForecastSample, ForecastSet, SourceCoverage
from rainmaker.polymarket.markets import Bucket, Market
from rainmaker.probability.calibration import Calibration
from rainmaker.store.db import connect
from rainmaker.store.query import count_rows, load_calibration


def _market(variable: str) -> Market:
    return Market(
        id="m1",
        slug="s",
        title=f"{'Highest' if variable == 'TMAX' else 'Lowest'} temperature in NYC on May 31?",
        target=build_target("NYC", variable, date(2026, 5, 31)),
        buckets=[
            Bucket(
                label="70-71°F",
                kind="range",
                lo=70,
                hi=71,
                threshold=None,
                yes_token_id="t",
                best_ask=0.40,
                best_bid=None,
                yes_price=0.0,
            )
        ],
    )


def _forecast_set(variable: str = "TMAX") -> ForecastSet:
    target = build_target("NYC", variable, date(2026, 5, 31))
    samples = [
        ForecastSample(
            source="nws",
            model="m",
            member=None,
            station="KLGA",
            variable=variable,
            target_date=date(2026, 5, 31),
            lead_time_days=1,
            value_f=v,
            issued_at=None,
        )
        for v in (69, 70, 71, 72)
    ]
    return ForecastSet(
        target=target,
        samples=samples,
        coverage=[
            SourceCoverage(source="nws", ok=True, n_samples=4),
            SourceCoverage(source="open-meteo", ok=True, n_samples=4),
        ],
    )


def test_run_builds_report_and_writes_files(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "discover_markets", lambda client: [_market("TMAX")])
    monkeypatch.setattr(cli, "discover_precip_markets", lambda client: [])
    monkeypatch.setattr(cli, "_forecast_for", lambda target, client: _forecast_set())
    monkeypatch.setattr(cli.httpx, "Client", lambda **kw: _DummyClient())
    monkeypatch.setattr(cli, "_today", lambda: date(2026, 5, 31))
    db = tmp_path / "t.db"

    cli.main(["run", "--reports-dir", str(tmp_path), "--db", str(db)])

    out = capsys.readouterr().out
    assert "70-71°F" in out
    assert "KLGA" in out
    written = sorted(p.name for p in tmp_path.iterdir() if p.suffix in {".md", ".json"})
    assert written == ["2026-05-31.json", "2026-05-31.md"]

    conn = connect(str(db))
    assert count_rows(conn, "runs") == 1
    assert count_rows(conn, "predictions") == 1  # one bucket -> one prediction
    conn.close()


def test_run_processes_tmin_market(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "discover_markets", lambda client: [_market("TMIN")])
    monkeypatch.setattr(cli, "discover_precip_markets", lambda client: [])
    monkeypatch.setattr(cli, "_forecast_for", lambda target, client: _forecast_set("TMIN"))
    monkeypatch.setattr(cli.httpx, "Client", lambda **kw: _DummyClient())
    monkeypatch.setattr(cli, "_today", lambda: date(2026, 5, 31))
    db = tmp_path / "t.db"

    cli.main(["run", "--reports-dir", str(tmp_path), "--db", str(db)])

    out = capsys.readouterr().out
    assert "70-71°F" in out
    assert "KLGA" in out

    conn = connect(str(db))
    assert count_rows(conn, "predictions") == 1  # one bucket -> one prediction
    conn.close()


def test_run_skips_when_variable_unsupported(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "SUPPORTED_VARIABLES", {"TMAX"})
    monkeypatch.setattr(cli, "discover_markets", lambda client: [_market("TMIN")])
    monkeypatch.setattr(cli, "discover_precip_markets", lambda client: [])
    monkeypatch.setattr(cli.httpx, "Client", lambda **kw: _DummyClient())

    cli.main(["run", "--reports-dir", str(tmp_path), "--db", str(tmp_path / "t.db")])

    out = capsys.readouterr().out
    assert "skipped" in out.lower()
    assert "TMIN" in out


def test_run_aborts_when_polymarket_down(monkeypatch, tmp_path):
    def _boom(client):
        raise httpx.HTTPStatusError(
            "down", request=httpx.Request("GET", "x"), response=httpx.Response(500)
        )

    monkeypatch.setattr(cli, "discover_markets", _boom)
    monkeypatch.setattr(cli.httpx, "Client", lambda **kw: _DummyClient())

    with pytest.raises(SystemExit) as exc:
        cli.main(["run", "--reports-dir", str(tmp_path), "--db", str(tmp_path / "t.db")])
    assert exc.value.code != 0


def test_backfill_fits_and_saves_calibration_and_accuracy(monkeypatch, tmp_path, capsys):
    from rainmaker.probability.calibration import Accuracy

    cal = Calibration(
        station="KLGA", variable="TMAX", lead_time=1, bias=-2.0, spread_scale=1.1, n_samples=42
    )
    acc = Accuracy(n=42, mae_f=2.5, bias_f=-2.0)
    monkeypatch.setattr(cli, "run_backfill", lambda *a, **k: (cal, acc))
    monkeypatch.setattr(cli.httpx, "Client", lambda **kw: _DummyClient())
    db = tmp_path / "t.db"

    cli.main(["backfill", "--db", str(db), "--lead", "1"])

    out = capsys.readouterr().out
    assert "calibrated KLGA TMAX lead=1" in out
    assert "mae=2.50F" in out
    conn = connect(str(db))
    saved = load_calibration(conn, "KLGA", "TMAX", 1)
    row = conn.execute("SELECT * FROM forecast_accuracy").fetchone()
    conn.close()
    assert saved == cal
    assert (row["station"], row["city"], row["kind"]) == ("KLGA", "NYC", "backtest")
    assert row["n"] == 42


def test_backfill_all_covers_every_city(monkeypatch, tmp_path):
    from rainmaker.config import STATIONS
    from rainmaker.probability.calibration import Accuracy

    def _fake(station, variable, lead, start, end, client):
        cal = Calibration(
            station=station.icao,
            variable=variable,
            lead_time=lead,
            bias=0.0,
            spread_scale=1.0,
            n_samples=42,
        )
        return cal, Accuracy(n=42, mae_f=2.0, bias_f=0.0)

    monkeypatch.setattr(cli, "run_backfill", _fake)
    monkeypatch.setattr(cli.httpx, "Client", lambda **kw: _DummyClient())
    db = tmp_path / "t.db"

    cli.main(["backfill", "--city", "all", "--db", str(db)])

    conn = connect(str(db))
    n = conn.execute("SELECT count(*) AS n FROM forecast_accuracy").fetchone()["n"]
    conn.close()
    assert n == len(STATIONS)


def test_backfill_exits_nonzero_when_all_cities_fail(monkeypatch, tmp_path, capsys):
    def _boom(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(cli, "run_backfill", _boom)
    monkeypatch.setattr(cli.httpx, "Client", lambda **kw: _DummyClient())

    with pytest.raises(SystemExit) as exc:
        cli.main(["backfill", "--city", "all", "--db", str(tmp_path / "t.db")])
    assert exc.value.code == 1
    assert "failed" in capsys.readouterr().err


def test_backfill_partial_failure_exits_zero(monkeypatch, tmp_path, capsys):
    from rainmaker.config import STATIONS
    from rainmaker.probability.calibration import Accuracy

    fail_city = sorted(STATIONS)[0]

    def _mixed(station, variable, lead, start, end, client):
        if station.icao == STATIONS[fail_city].icao:
            raise httpx.ConnectError("down")
        cal = Calibration(
            station=station.icao,
            variable=variable,
            lead_time=lead,
            bias=0.0,
            spread_scale=1.0,
            n_samples=42,
        )
        return cal, Accuracy(n=42, mae_f=2.0, bias_f=0.0)

    monkeypatch.setattr(cli, "run_backfill", _mixed)
    monkeypatch.setattr(cli.httpx, "Client", lambda **kw: _DummyClient())
    db = tmp_path / "t.db"

    cli.main(["backfill", "--city", "all", "--db", str(db)])  # must not raise SystemExit

    err = capsys.readouterr().err
    assert fail_city in err and "failed" in err
    conn = connect(str(db))
    n = conn.execute("SELECT count(*) AS n FROM forecast_accuracy").fetchone()["n"]
    conn.close()
    assert n == len(STATIONS) - 1


def test_snapshot_command_writes_and_reports(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        cli,
        "write_snapshot",
        lambda conn, on_date, created_at: {
            "pnl": {"n_bets": 2, "wins": 1, "losses": 1, "total_pnl": 0.3, "roi": 0.42},
            "calibration": {"n": 2, "brier": 0.13, "hit_rate": 0.5},
        },
    )
    cli.main(["snapshot", "--db", str(tmp_path / "t.db")])
    out = capsys.readouterr().out
    assert "snapshot" in out and "2 bets" in out


def test_track_command_reports_pnl_and_calibration(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        cli,
        "compute_pnl",
        lambda conn: {"n_bets": 2, "wins": 1, "losses": 1, "total_pnl": 0.3, "roi": 0.42},
    )
    monkeypatch.setattr(
        cli, "compute_calibration", lambda conn: {"n": 2, "brier": 0.127, "hit_rate": 0.5}
    )
    cli.main(["track", "--db", str(tmp_path / "t.db")])
    out = capsys.readouterr().out
    assert "P&L: 2 bets, 1-1" in out
    assert "Brier 0.127" in out


def test_settle_command_reports_summary(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "run_settlement", lambda conn, client, today, settled_at: (2, 1))
    monkeypatch.setattr(cli.httpx, "Client", lambda **kw: _DummyClient())
    cli.main(["settle", "--db", str(tmp_path / "t.db")])
    out = capsys.readouterr().out
    assert "settled 2 market(s); 1 waiting" in out


def test_backtest_command_writes_report(monkeypatch, tmp_path, capsys):
    from rainmaker.backtest import BacktestResult, ReliabilityBin

    result = BacktestResult(
        n=100,
        modal_hit_rate=0.55,
        mean_modal_p=0.50,
        mean_brier=0.20,
        coverage={0.5: 0.5, 0.8: 0.8, 0.9: 0.9},
        reliability=[
            ReliabilityBin(lo=0.5, hi=0.6, predicted_mean=0.55, observed_freq=0.6, count=20)
        ],
    )
    monkeypatch.setattr(cli, "backtest_synthetic", lambda *a, **k: result)
    monkeypatch.setattr(cli, "fetch_closed_weather_events", lambda client: [])
    monkeypatch.setattr(cli, "backtest_real", lambda *a, **k: None)
    monkeypatch.setattr(cli.httpx, "Client", lambda **kw: _DummyClient())
    monkeypatch.setattr(cli, "_today", lambda: date(2026, 6, 5))

    cli.main(["backtest", "--city", "NYC", "--days", "365", "--reports-dir", str(tmp_path)])

    out = capsys.readouterr().out
    assert "Forecast backtest" in out and "NYC" in out
    written = {p.name for p in tmp_path.iterdir()}
    assert {"backtest-2026-06-05.md", "backtest-2026-06-05.json"} <= written


def test_backtest_exits_when_no_data(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "backtest_synthetic", lambda *a, **k: None)
    monkeypatch.setattr(cli.httpx, "Client", lambda **kw: _DummyClient())
    with pytest.raises(SystemExit) as exc:
        cli.main(["backtest", "--city", "NYC", "--no-real", "--reports-dir", str(tmp_path)])
    assert exc.value.code == 1


def test_backtest_pnl_command_writes_report(monkeypatch, tmp_path, capsys):
    from rainmaker.pnl_backtest import LeadPnl, PnlBacktestResult

    lp = LeadPnl(
        lead=0, n_bets=2, wins=2, losses=0, total_pnl=0.30, roi=0.18, win_rate=1.0, mean_edge=0.12
    )
    result = PnlBacktestResult(
        n_markets=2,
        floor=0.90,
        min_sources=1,
        min_edge=0.05,
        per_lead=[lp],
        overall=lp.model_copy(update={"lead": -1}),
    )
    captured: dict[str, object] = {}

    def _fake_backtest_pnl(events, client, **kwargs):
        captured.update(kwargs)
        return result

    monkeypatch.setattr(cli, "backtest_pnl", _fake_backtest_pnl)
    monkeypatch.setattr(cli, "fetch_closed_weather_events", lambda client: [])
    monkeypatch.setattr(cli.httpx, "Client", lambda **kw: _DummyClient())
    monkeypatch.setattr(cli, "_today", lambda: date(2026, 6, 5))

    cli.main(
        [
            "backtest-pnl",
            "--city",
            "NYC",
            "--days",
            "365",
            "--leads",
            "0,1,2",
            "--reports-dir",
            str(tmp_path),
        ]
    )

    out = capsys.readouterr().out
    assert "Betting P/L backtest" in out
    written = {p.name for p in tmp_path.iterdir()}
    assert {"pnl-backtest-2026-06-05.md", "pnl-backtest-2026-06-05.json"} <= written
    assert captured["leads"] == (0, 1, 2)  # the --leads list is parsed to a tuple of ints
    assert captured["city"] == "NYC"


def test_backtest_pnl_exits_when_no_data(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "backtest_pnl", lambda *a, **k: None)
    monkeypatch.setattr(cli, "fetch_closed_weather_events", lambda client: [])
    monkeypatch.setattr(cli.httpx, "Client", lambda **kw: _DummyClient())
    with pytest.raises(SystemExit) as exc:
        cli.main(["backtest-pnl", "--city", "NYC", "--reports-dir", str(tmp_path)])
    assert exc.value.code == 1


def test_datastore_prefers_database_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    assert cli._datastore("local.db") == "postgresql://x/y"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert cli._datastore("local.db") == "local.db"


class _DummyClient:
    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_db_label_redacts_postgres_dsn():
    assert cli._db_label("postgresql://user:secret@host:5432/db") == "postgres"
    assert cli._db_label("postgres://user:secret@host/db") == "postgres"
    assert cli._db_label("rainmaker.db") == "rainmaker.db"


def _precip_market_and_set():
    import json
    from pathlib import Path

    from rainmaker.forecasts.precip import PrecipForecastSet
    from rainmaker.polymarket.precip_markets import parse_precip_event

    fixtures = Path(__file__).parent / "fixtures"
    market = parse_precip_event(
        json.loads((fixtures / "polymarket_precip_monthly_nyc.json").read_text())
    )
    fs = PrecipForecastSet(
        target=market.target,
        mean=2.5,
        var=0.6,
        coverage=[
            SourceCoverage(source="open-meteo", ok=True, n_samples=40),
            SourceCoverage(source="nws", ok=True, n_samples=3),
        ],
        n_observed_days=5,
        n_forecast_days=7,
        n_clim_days=18,
    )
    return market, fs


def test_run_routes_precip_market(monkeypatch, tmp_path, capsys):
    market, fs = _precip_market_and_set()
    monkeypatch.setattr(cli, "discover_markets", lambda client: [])
    monkeypatch.setattr(cli, "discover_precip_markets", lambda client: [market])
    monkeypatch.setattr(cli, "_precip_forecast_for", lambda target, today, client: fs)
    monkeypatch.setattr(cli.httpx, "Client", lambda **kw: _DummyClient())
    monkeypatch.setattr(cli, "_today", lambda: date(2026, 6, 6))
    db = tmp_path / "t.db"

    cli.main(["run", "--reports-dir", str(tmp_path), "--db", str(db)])

    out = capsys.readouterr().out
    assert "Central Park NY" in out  # the resolution station is named
    assert "PRCP" in out
    conn = connect(str(db))
    assert count_rows(conn, "predictions") >= 6  # one per inch bracket, persisted
    conn.close()
