from datetime import date

from pydantic import BaseModel

from rainmaker.ranking.edge import MarketReport, RankedOutcome


class Report(BaseModel):
    run_date: date
    markets: list[MarketReport]


def _coverage_str(report: MarketReport) -> str:
    return ", ".join(
        f"{c.source}={'ok' if c.ok else 'FAILED'}({c.n_samples})" for c in report.coverage
    )


def _recommended_pairs(report: Report) -> list[tuple[MarketReport, RankedOutcome]]:
    """Every gate-passing outcome across all markets, best edge first."""
    pairs = [(m, o) for m in report.markets for o in m.outcomes if o.recommended]
    pairs.sort(key=lambda mo: mo[1].edge, reverse=True)
    return pairs


def render_terminal(report: Report) -> str:
    lines: list[str] = [
        f"Rainmaker report {report.run_date.isoformat()}",
        "",
        "P(win)=our probability  ask=price paid  edge=P(win)-ask  "
        "side YES=buy bucket, NO=sell bucket  (all 0-1)  REC=passes gates",
        "",
    ]
    bets = _recommended_pairs(report)
    lines.append("Recommended bets (ranked by edge):")
    if bets:
        for m, o in bets:
            lines.append(
                f"  {m.title}  {o.bucket_label} {o.side}  "
                f"P(win)={o.p_win:.2f} ask={o.best_ask:.2f} edge={o.edge:+.2f}"
            )
    else:
        lines.append("  No bets pass the gates today.")
    lines.append("")
    for m in report.markets:
        lines.append(f"{m.title}  [{m.station} {m.variable} {m.settlement_date.isoformat()}]")
        lines.append(f"  sources: {m.n_sources}")
        if m.mu is not None and m.sigma is not None:
            cal = "calibrated" if m.calibrated else "uncalibrated"
            lines.append(f"  forecast: mu={m.mu:.1f}F sigma={m.sigma:.1f}F ({cal})")
        lines.append(f"  coverage: {_coverage_str(m)}")
        if not m.outcomes:
            lines.append("  no tradeable outcomes (insufficient forecast data)")
        else:
            lines.append(f"  {'bucket':16} {'side':4} {'P(win)':>7} {'ask':>6} {'edge':>7}  rec")
            for o in m.outcomes:
                marker = "REC" if o.recommended else ""
                line = (
                    f"  {o.bucket_label:16} {o.side:4} {o.p_win:>7.2f}"
                    f" {o.best_ask:>6.2f} {o.edge:>+7.2f}  {marker}"
                )
                lines.append(line)
        if m.excluded_no_ask:
            lines.append(f"  excluded (no ask): {', '.join(m.excluded_no_ask)}")
        lines.append("")
    return "\n".join(lines)


def render_markdown(report: Report) -> str:
    lines: list[str] = [
        f"# Rainmaker report {report.run_date.isoformat()}",
        "",
        "_P(win) = our probability  ask = price paid"
        "  edge = P(win)-ask  side YES = buy bucket, NO = sell bucket"
        "  (all 0-1)  rec = passes gates_",
        "",
    ]
    bets = _recommended_pairs(report)
    lines.append("## Recommended bets (ranked by edge)")
    lines.append("")
    if bets:
        lines.append("| market | bucket | side | P(win) | ask | edge |")
        lines.append("|--------|--------|------|--------|-----|------|")
        for m, o in bets:
            lines.append(
                f"| {m.title} | {o.bucket_label} | {o.side} | {o.p_win:.2f}"
                f" | {o.best_ask:.2f} | {o.edge:+.2f} |"
            )
    else:
        lines.append("_No bets pass the gates today._")
    lines.append("")
    for m in report.markets:
        lines.append(f"## {m.title}")
        lines.append("")
        lines.append(
            f"- station: {m.station}  variable: {m.variable}"
            f"  settlement: {m.settlement_date.isoformat()}"
        )
        lines.append(f"- sources: {m.n_sources}")
        if m.mu is not None and m.sigma is not None:
            cal = "calibrated" if m.calibrated else "uncalibrated"
            lines.append(f"- forecast: mu={m.mu:.1f}F sigma={m.sigma:.1f}F ({cal})")
        lines.append(f"- coverage: {_coverage_str(m)}")
        lines.append("")
        if m.outcomes:
            lines.append("| bucket | side | P(win) | ask | edge | rec |")
            lines.append("|--------|------|--------|-----|------|-----|")
            for o in m.outcomes:
                rec = "yes" if o.recommended else ""
                lines.append(
                    f"| {o.bucket_label} | {o.side} | {o.p_win:.2f}"
                    f" | {o.best_ask:.2f} | {o.edge:+.2f} | {rec} |"
                )
        else:
            lines.append("_no tradeable outcomes (insufficient forecast data)_")
        if m.excluded_no_ask:
            lines.append("")
            lines.append(f"Excluded (no ask): {', '.join(m.excluded_no_ask)}")
        lines.append("")
    return "\n".join(lines)
