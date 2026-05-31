from datetime import date

from pydantic import BaseModel

from rainmaker.ranking.edge import MarketReport


class Report(BaseModel):
    run_date: date
    markets: list[MarketReport]


def _coverage_str(report: MarketReport) -> str:
    return ", ".join(
        f"{c.source}={'ok' if c.ok else 'FAILED'}({c.n_samples})" for c in report.coverage
    )


def render_terminal(report: Report) -> str:
    lines: list[str] = [f"Rainmaker report {report.run_date.isoformat()}", ""]
    for m in report.markets:
        lines.append(f"{m.title}  [{m.station} {m.variable} {m.settlement_date.isoformat()}]")
        if m.mu is not None and m.sigma is not None:
            lines.append(f"  forecast: mu={m.mu:.1f}F sigma={m.sigma:.1f}F  sources={m.n_sources}")
        lines.append(f"  coverage: {_coverage_str(m)}")
        if not m.outcomes:
            lines.append("  no tradeable outcomes (insufficient forecast data)")
        else:
            lines.append(f"  {'bucket':16} {'P(win)':>7} {'ask':>6} {'edge':>7}  rec")
            for o in m.outcomes:
                marker = "REC" if o.recommended else ""
                line = (
                    f"  {o.bucket_label:16} {o.p_win:>7.2f}"
                    f" {o.best_ask:>6.2f} {o.edge:>+7.2f}  {marker}"
                )
                lines.append(line)
        if m.excluded_no_ask:
            lines.append(f"  excluded (no ask): {', '.join(m.excluded_no_ask)}")
        lines.append("")
    return "\n".join(lines)


def render_markdown(report: Report) -> str:
    lines: list[str] = [f"# Rainmaker report {report.run_date.isoformat()}", ""]
    for m in report.markets:
        lines.append(f"## {m.title}")
        lines.append("")
        lines.append(
            f"- station: {m.station}  variable: {m.variable}"
            f"  settlement: {m.settlement_date.isoformat()}"
        )
        if m.mu is not None and m.sigma is not None:
            lines.append(f"- forecast: mu={m.mu:.1f}F sigma={m.sigma:.1f}F  sources: {m.n_sources}")
        lines.append(f"- coverage: {_coverage_str(m)}")
        lines.append("")
        if m.outcomes:
            lines.append("| bucket | P(win) | ask | edge | rec |")
            lines.append("|--------|--------|-----|------|-----|")
            for o in m.outcomes:
                rec = "yes" if o.recommended else ""
                lines.append(
                    f"| {o.bucket_label} | {o.p_win:.2f}"
                    f" | {o.best_ask:.2f} | {o.edge:+.2f} | {rec} |"
                )
        else:
            lines.append("_no tradeable outcomes (insufficient forecast data)_")
        if m.excluded_no_ask:
            lines.append("")
            lines.append(f"Excluded (no ask): {', '.join(m.excluded_no_ask)}")
        lines.append("")
    return "\n".join(lines)
