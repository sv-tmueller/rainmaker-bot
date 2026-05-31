from datetime import date

from rainmaker.forecasts.base import SourceCoverage
from rainmaker.ranking.edge import MarketReport, RankedOutcome
from rainmaker.report.render import Report, render_markdown, render_terminal


def _market_report() -> MarketReport:
    return MarketReport(
        market_id="m1",
        title="Highest temperature in NYC on May 31?",
        station="KLGA",
        variable="TMAX",
        settlement_date=date(2026, 5, 31),
        mu=70.5,
        sigma=2.0,
        n_sources=2,
        coverage=[
            SourceCoverage(source="nws", ok=True, n_samples=1),
            SourceCoverage(source="open-meteo", ok=True, n_samples=124),
        ],
        outcomes=[
            RankedOutcome(
                bucket_label="70-71°F", p_win=0.93, best_ask=0.40, edge=0.53, recommended=True
            ),
            RankedOutcome(
                bucket_label="72-73°F", p_win=0.04, best_ask=0.30, edge=-0.26, recommended=False
            ),
        ],
        excluded_no_ask=["59°F or below"],
    )


def test_report_json_round_trips():
    report = Report(run_date=date(2026, 5, 31), markets=[_market_report()])
    data = report.model_dump(mode="json")
    assert data["run_date"] == "2026-05-31"
    assert data["markets"][0]["outcomes"][0]["bucket_label"] == "70-71°F"
    assert data["markets"][0]["outcomes"][0]["recommended"] is True


def test_render_terminal_shows_key_columns_and_recommended_marker():
    report = Report(run_date=date(2026, 5, 31), markets=[_market_report()])
    text = render_terminal(report)
    assert "KLGA" in text
    assert "70-71°F" in text
    assert "0.93" in text  # p_win
    assert "0.40" in text  # best ask
    assert "0.53" in text  # edge
    assert "REC" in text  # recommended marker on the recommended row
    assert "59°F or below" in text  # excluded note


def test_render_markdown_has_table_and_settlement_date():
    report = Report(run_date=date(2026, 5, 31), markets=[_market_report()])
    md = render_markdown(report)
    assert md.startswith("# Rainmaker report 2026-05-31")
    assert "| bucket | P(win) | ask | edge | rec |" in md
    assert "2026-05-31" in md
