from datetime import date

from rainmaker.forecasts.base import SourceCoverage
from rainmaker.ranking.edge import MarketReport, RankedOutcome
from rainmaker.report.render import Report, render_markdown, render_terminal


def _market_report() -> MarketReport:
    return MarketReport(
        market_id="m1",
        title="Highest temperature in NYC on May 31?",
        city="NYC",
        station="KLGA",
        variable="TMAX",
        settlement_date=date(2026, 5, 31),
        mu=70.5,
        sigma=2.0,
        n_sources=2,
        calibrated="full",
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
    assert "calibrated" in text  # calibration status shown


def test_render_markdown_has_table_and_settlement_date():
    report = Report(run_date=date(2026, 5, 31), markets=[_market_report()])
    md = render_markdown(report)
    assert md.startswith("# Rainmaker report 2026-05-31")
    assert "| bucket | side | P(win) | ask | edge | rec |" in md
    assert "2026-05-31" in md


def test_render_markdown_shows_excluded_no_ask_note():
    report = Report(run_date=date(2026, 5, 31), markets=[_market_report()])
    md = render_markdown(report)
    assert "Excluded (no ask): 59°F or below" in md


def test_render_shows_venue():
    poly = Report(run_date=date(2026, 5, 31), markets=[_market_report()])
    assert "polymarket" in render_terminal(poly)
    assert "venue: polymarket" in render_markdown(poly)
    kalshi_report = _market_report().model_copy(update={"venue": "kalshi"})
    report = Report(run_date=date(2026, 5, 31), markets=[kalshi_report])
    assert "kalshi" in render_terminal(report)
    assert "venue: kalshi" in render_markdown(report)


def test_render_handles_empty_samples_market():
    empty = MarketReport(
        market_id="m2",
        title="Highest temperature in NYC on May 31?",
        city="NYC",
        station="KLGA",
        variable="TMAX",
        settlement_date=date(2026, 5, 31),
        mu=None,
        sigma=None,
        n_sources=0,
        calibrated="uncalibrated",
        coverage=[SourceCoverage(source="nws", ok=False, n_samples=0, error="down")],
        outcomes=[],
        excluded_no_ask=[],
    )
    report = Report(run_date=date(2026, 5, 31), markets=[empty])
    text = render_terminal(report)
    md = render_markdown(report)
    assert "no tradeable outcomes" in text
    assert "no tradeable outcomes" in md
    assert "sources" in text  # source count still shown for a failed-data market


def test_render_multi_market_report():
    report = Report(run_date=date(2026, 5, 31), markets=[_market_report(), _market_report()])
    text = render_terminal(report)
    # both market detail sections rendered (the forecast line is per-market, not
    # in the recommended-bets summary, so it is a stable count of two)
    assert text.count("forecast: mu=70.5F") == 2


def test_render_leads_with_recommended_bets_summary():
    report = Report(run_date=date(2026, 5, 31), markets=[_market_report()])
    text = render_terminal(report)
    md = render_markdown(report)
    assert "Recommended bets" in text
    assert "Recommended bets" in md
    # the summary comes before the per-market detail
    assert text.index("Recommended bets") < text.index("Highest temperature in NYC")
    assert md.index("Recommended bets") < md.index("## Highest temperature in NYC")
    # the one recommended bet is listed in the summary
    assert "70-71°F" in md


def _rec_market(city: str, title: str, edge: float) -> MarketReport:
    return MarketReport(
        market_id=city,
        title=title,
        city=city,
        station="X",
        variable="TMAX",
        settlement_date=date(2026, 5, 31),
        mu=70.0,
        sigma=2.0,
        n_sources=2,
        calibrated="uncalibrated",
        coverage=[],
        outcomes=[
            RankedOutcome(
                bucket_label="70-71°F",
                side="NO",
                p_win=0.90,
                best_ask=round(0.90 - edge, 2),
                edge=edge,
                recommended=True,
            )
        ],
        excluded_no_ask=[],
    )


def test_recommended_summary_groups_by_city_ordered_by_best_edge():
    report = Report(
        run_date=date(2026, 5, 31),
        markets=[
            _rec_market("Seattle", "Highest temperature in Seattle on May 31?", 0.40),
            _rec_market("NYC", "Highest temperature in NYC on May 31?", 0.50),
        ],
    )
    for render in (render_terminal, render_markdown):
        out = render(report)
        # the per-market detail starts at the first full title; the summary precedes it
        summary = out.split("Highest temperature in Seattle")[0]
        assert "NYC" in summary and "Seattle" in summary  # both cities head a group
        assert summary.index("NYC") < summary.index("Seattle")  # bigger edge leads
        assert "Highest temperature in NYC" not in summary  # city dropped from the bet label


def test_render_shows_side_for_no_bet():
    rep = MarketReport(
        market_id="m4",
        title="Highest temperature in NYC on May 31?",
        city="NYC",
        station="KLGA",
        variable="TMAX",
        settlement_date=date(2026, 5, 31),
        mu=70.5,
        sigma=2.0,
        n_sources=2,
        calibrated="full",
        coverage=[SourceCoverage(source="nws", ok=True, n_samples=1)],
        outcomes=[
            RankedOutcome(
                bucket_label="80-81°F",
                side="NO",
                p_win=0.95,
                best_ask=0.70,
                edge=0.25,
                recommended=True,
            )
        ],
        excluded_no_ask=[],
    )
    report = Report(run_date=date(2026, 5, 31), markets=[rep])
    text = render_terminal(report)
    md = render_markdown(report)
    assert "80-81°F NO" in text  # the summary line names the side
    assert "| NO |" in md  # the per-market table has a side column


def test_render_uses_inch_unit_for_precip():
    rep = MarketReport(
        market_id="p1",
        title="Precipitation in NYC in June?",
        city="NYC",
        station="Central Park NY",
        variable="PRCP",
        settlement_date=date(2026, 6, 30),
        mu=3.06,
        sigma=2.0,
        n_sources=2,
        calibrated="uncalibrated",
        coverage=[SourceCoverage(source="open-meteo", ok=True, n_samples=40)],
        outcomes=[
            RankedOutcome(
                bucket_label='2-3"', p_win=0.40, best_ask=0.29, edge=0.11, recommended=False
            )
        ],
        excluded_no_ask=[],
    )
    report = Report(run_date=date(2026, 6, 6), markets=[rep])
    text = render_terminal(report)
    md = render_markdown(report)
    assert "mu=3.06in sigma=2.00in" in text
    assert "mu=3.06in sigma=2.00in" in md
    assert "Central Park NY" in text


def test_render_summary_says_none_when_no_recommendations():
    no_rec = MarketReport(
        market_id="m3",
        title="Highest temperature in NYC on May 31?",
        city="NYC",
        station="KLGA",
        variable="TMAX",
        settlement_date=date(2026, 5, 31),
        mu=70.5,
        sigma=2.0,
        n_sources=2,
        calibrated="uncalibrated",
        coverage=[SourceCoverage(source="nws", ok=True, n_samples=1)],
        outcomes=[
            RankedOutcome(
                bucket_label="72-73°F", p_win=0.04, best_ask=0.30, edge=-0.26, recommended=False
            )
        ],
        excluded_no_ask=[],
    )
    report = Report(run_date=date(2026, 5, 31), markets=[no_rec])
    assert "No bets pass the gates today." in render_terminal(report)
    assert "No bets pass the gates today." in render_markdown(report)


def test_coverage_str_includes_error_for_failed_source():
    """A failed source must show its error reason, not just FAILED(n)."""
    failed = MarketReport(
        market_id="m5",
        title="Highest temperature in NYC on May 31?",
        city="NYC",
        station="KLGA",
        variable="TMAX",
        settlement_date=date(2026, 5, 31),
        mu=None,
        sigma=None,
        n_sources=0,
        calibrated="uncalibrated",
        coverage=[
            SourceCoverage(source="nws", ok=False, n_samples=0, error="timeout"),
            SourceCoverage(source="open-meteo", ok=True, n_samples=80),
        ],
        outcomes=[],
        excluded_no_ask=[],
    )
    report = Report(run_date=date(2026, 5, 31), markets=[failed])
    text = render_terminal(report)
    md = render_markdown(report)
    # error reason must appear in coverage string in both renderers
    assert "timeout" in text
    assert "timeout" in md
    # ok source is not affected
    assert "open-meteo=ok" in text
    assert "open-meteo=ok" in md


def test_coverage_str_error_none_still_renders_gracefully():
    """A failed source with no error string set should not crash."""
    no_error = MarketReport(
        market_id="m6",
        title="Highest temperature in NYC on May 31?",
        city="NYC",
        station="KLGA",
        variable="TMAX",
        settlement_date=date(2026, 5, 31),
        mu=None,
        sigma=None,
        n_sources=0,
        calibrated="uncalibrated",
        coverage=[SourceCoverage(source="nws", ok=False, n_samples=0, error=None)],
        outcomes=[],
        excluded_no_ask=[],
    )
    report = Report(run_date=date(2026, 5, 31), markets=[no_error])
    text = render_terminal(report)
    md = render_markdown(report)
    assert "nws=FAILED" in text
    assert "nws=FAILED" in md
    # a buggy FAILED(None)(0) would pass the assertion above; pin that no literal
    # "None" leaks into either renderer.
    assert "None" not in text
    assert "None" not in md


def test_render_shows_partial_data_note_when_only_mu_set():
    """If mu is set but sigma is None, render a partial data note instead of silently skipping."""
    partial = MarketReport(
        market_id="m7",
        title="Highest temperature in NYC on May 31?",
        city="NYC",
        station="KLGA",
        variable="TMAX",
        settlement_date=date(2026, 5, 31),
        mu=70.5,
        sigma=None,
        n_sources=1,
        calibrated="uncalibrated",
        coverage=[SourceCoverage(source="nws", ok=True, n_samples=1)],
        outcomes=[],
        excluded_no_ask=[],
    )
    report = Report(run_date=date(2026, 5, 31), markets=[partial])
    text = render_terminal(report)
    md = render_markdown(report)
    assert "partial" in text
    assert "partial" in md


def test_render_shows_partial_data_note_when_only_sigma_set():
    """If sigma is set but mu is None, render a partial data note."""
    partial = MarketReport(
        market_id="m8",
        title="Highest temperature in NYC on May 31?",
        city="NYC",
        station="KLGA",
        variable="TMAX",
        settlement_date=date(2026, 5, 31),
        mu=None,
        sigma=2.0,
        n_sources=1,
        calibrated="uncalibrated",
        coverage=[SourceCoverage(source="nws", ok=True, n_samples=1)],
        outcomes=[],
        excluded_no_ask=[],
    )
    report = Report(run_date=date(2026, 5, 31), markets=[partial])
    text = render_terminal(report)
    md = render_markdown(report)
    assert "partial" in text
    assert "partial" in md


def test_render_partial_data_precip_path():
    """Partial data note also appears in the precipitation path (variable=PRCP)."""
    partial = MarketReport(
        market_id="m9",
        title="Precipitation in NYC in June?",
        city="NYC",
        station="Central Park NY",
        variable="PRCP",
        settlement_date=date(2026, 6, 30),
        mu=3.06,
        sigma=None,
        n_sources=1,
        calibrated="uncalibrated",
        coverage=[SourceCoverage(source="open-meteo", ok=True, n_samples=40)],
        outcomes=[],
        excluded_no_ask=[],
    )
    report = Report(run_date=date(2026, 6, 6), markets=[partial])
    text = render_terminal(report)
    md = render_markdown(report)
    assert "partial" in text
    assert "partial" in md


def test_render_bias_corrected_label():
    """bias_only state renders as 'bias-corrected', not 'calibrated' or 'uncalibrated'."""
    rep = MarketReport(
        market_id="m10",
        title="Highest temperature in NYC on May 31?",
        city="NYC",
        station="KLGA",
        variable="TMAX",
        settlement_date=date(2026, 5, 31),
        mu=70.5,
        sigma=2.5,
        n_sources=2,
        calibrated="bias_only",
        coverage=[SourceCoverage(source="nws", ok=True, n_samples=1)],
        outcomes=[],
        excluded_no_ask=[],
    )
    report = Report(run_date=date(2026, 5, 31), markets=[rep])
    text = render_terminal(report)
    md = render_markdown(report)
    assert "bias-corrected" in text
    assert "bias-corrected" in md
    assert "uncalibrated" not in text
    assert "uncalibrated" not in md
