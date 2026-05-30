from datetime import date

from rainmaker import cli
from rainmaker.config import build_target
from rainmaker.forecasts.base import ForecastSample, ForecastSet, SourceCoverage


def test_cli_run_prints_samples_and_coverage(monkeypatch, capsys):
    target = build_target("NYC", "TMAX", date(2026, 5, 31))
    fake = ForecastSet(
        target=target,
        samples=[
            ForecastSample(
                source="nws",
                model="nws",
                member=None,
                station="KLGA",
                variable="TMAX",
                target_date=date(2026, 5, 31),
                lead_time_days=1,
                value_f=76.0,
                issued_at=None,
            )
        ],
        coverage=[SourceCoverage(source="nws", ok=True, n_samples=1)],
    )

    def fake_aggregate(target, sources):
        return fake

    monkeypatch.setattr(cli, "aggregate", fake_aggregate)
    monkeypatch.setattr(cli.httpx, "Client", lambda **kw: _DummyClient())

    cli.main(["run", "--city", "NYC", "--variable", "TMAX", "--date", "2026-05-31"])
    out = capsys.readouterr().out
    assert "KLGA" in out
    assert "76.0" in out
    assert "nws" in out


class _DummyClient:
    def close(self):
        pass
