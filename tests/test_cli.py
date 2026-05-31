# tests/test_cli.py
from datetime import date

import httpx
import pytest

from rainmaker import cli
from rainmaker.config import build_target
from rainmaker.forecasts.base import ForecastSample, ForecastSet, SourceCoverage
from rainmaker.polymarket.markets import Bucket, Market


def _market(variable: str) -> Market:
    return Market(
        id="m1", slug="s",
        title=f"{'Highest' if variable == 'TMAX' else 'Lowest'} temperature in NYC on May 31?",
        target=build_target("NYC", variable, date(2026, 5, 31)),
        buckets=[Bucket(label="70-71°F", kind="range", lo=70, hi=71, threshold=None,
                        yes_token_id="t", best_ask=0.40, best_bid=None, yes_price=0.0)],
    )


def _forecast_set() -> ForecastSet:
    target = build_target("NYC", "TMAX", date(2026, 5, 31))
    samples = [
        ForecastSample(source="nws", model="m", member=None, station="KLGA", variable="TMAX",
                       target_date=date(2026, 5, 31), lead_time_days=1, value_f=v, issued_at=None)
        for v in (69, 70, 71, 72)
    ]
    return ForecastSet(target=target, samples=samples,
                       coverage=[SourceCoverage(source="nws", ok=True, n_samples=4),
                                 SourceCoverage(source="open-meteo", ok=True, n_samples=4)])


def test_run_builds_report_and_writes_files(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "discover_markets", lambda client: [_market("TMAX")])
    monkeypatch.setattr(cli, "_forecast_for", lambda target, client: _forecast_set())
    monkeypatch.setattr(cli.httpx, "Client", lambda **kw: _DummyClient())

    cli.main(["run", "--reports-dir", str(tmp_path)])

    out = capsys.readouterr().out
    assert "70-71°F" in out
    assert "KLGA" in out
    written = sorted(p.name for p in tmp_path.iterdir())
    assert written == ["2026-05-31.json", "2026-05-31.md"]


def test_run_skips_unsupported_variable(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "discover_markets", lambda client: [_market("TMIN")])
    monkeypatch.setattr(cli.httpx, "Client", lambda **kw: _DummyClient())

    cli.main(["run", "--reports-dir", str(tmp_path)])

    out = capsys.readouterr().out
    assert "skipped" in out.lower()
    assert "TMIN" in out


def test_run_aborts_when_polymarket_down(monkeypatch, tmp_path):
    def _boom(client):
        raise httpx.HTTPStatusError("down", request=httpx.Request("GET", "x"), response=httpx.Response(500))

    monkeypatch.setattr(cli, "discover_markets", _boom)
    monkeypatch.setattr(cli.httpx, "Client", lambda **kw: _DummyClient())

    with pytest.raises(SystemExit) as exc:
        cli.main(["run", "--reports-dir", str(tmp_path)])
    assert exc.value.code != 0


class _DummyClient:
    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False
