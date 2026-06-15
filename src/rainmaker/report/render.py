from datetime import date

from pydantic import BaseModel

from rainmaker.ranking.edge import MarketReport, RankedOutcome

RecommendedPair = tuple[MarketReport, RankedOutcome]


class Report(BaseModel):
    run_date: date
    markets: list[MarketReport]


def _coverage_str(report: MarketReport) -> str:
    parts = []
    for c in report.coverage:
        if c.ok:
            parts.append(f"{c.source}=ok({c.n_samples})")
        else:
            reason = f"({c.error})" if c.error else ""
            parts.append(f"{c.source}=FAILED{reason}({c.n_samples})")
    return ", ".join(parts)


def _recommended_pairs(report: Report) -> list[RecommendedPair]:
    """Every gate-passing outcome across all markets, best edge first."""
    pairs = [(m, o) for m in report.markets for o in m.outcomes if o.recommended]
    pairs.sort(key=lambda mo: mo[1].edge, reverse=True)
    return pairs


def _recommended_by_city(report: Report) -> list[tuple[str, list[RecommendedPair]]]:
    """Recommended outcomes grouped by city. Cities are ordered by their best edge
    and bets within a city by edge (both descending), so the strongest call leads."""
    groups: dict[str, list[RecommendedPair]] = {}
    for m, o in _recommended_pairs(report):  # already edge-sorted desc
        groups.setdefault(m.city, []).append((m, o))
    return sorted(groups.items(), key=lambda kv: kv[1][0][1].edge, reverse=True)


def _bet_label(m: MarketReport) -> str:
    """The market descriptor for a per-bet summary line, with the city dropped (it
    is redundant under the city header). Falls back to the full title if absent."""
    needle = f" in {m.city}"
    return m.title.replace(needle, "", 1) if needle in m.title else m.title


def render_terminal(report: Report) -> str:
    lines: list[str] = [
        f"Rainmaker report {report.run_date.isoformat()}",
        "",
        "P(win)=our probability  ask=price paid  edge=P(win)-ask  "
        "side YES=buy bucket, NO=sell bucket  (all 0-1)  REC=passes gates",
        "",
    ]
    lines.append("Recommended bets (grouped by city, best edge first):")
    grouped = _recommended_by_city(report)
    if grouped:
        for city, city_pairs in grouped:
            lines.append(f"  {city}")
            for m, o in city_pairs:
                lines.append(
                    f"    {_bet_label(m)}  {o.bucket_label} {o.side}  "
                    f"P(win)={o.p_win:.2f} ask={o.best_ask:.2f} edge={o.edge:+.2f}"
                )
    else:
        lines.append("  No bets pass the gates today.")
    lines.append("")
    for m in report.markets:
        lines.append(
            f"{m.title}  [{m.station} {m.variable} {m.settlement_date.isoformat()} - {m.venue}]"
        )
        lines.append(f"  sources: {m.n_sources}")
        if m.mu is not None and m.sigma is not None:
            cal = "calibrated" if m.calibrated else "uncalibrated"
            if m.variable == "PRCP":
                lines.append(f"  forecast: mu={m.mu:.2f}in sigma={m.sigma:.2f}in ({cal})")
            else:
                unit = getattr(m, "unit", "F")
                lines.append(f"  forecast: mu={m.mu:.1f}{unit} sigma={m.sigma:.1f}{unit} ({cal})")
        elif (m.mu is None) != (m.sigma is None):
            lines.append("  forecast: partial data (only one of mu/sigma available)")
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
    lines.append("## Recommended bets (grouped by city, best edge first)")
    lines.append("")
    grouped = _recommended_by_city(report)
    if grouped:
        for city, city_pairs in grouped:
            lines.append(f"### {city}")
            lines.append("")
            lines.append("| market | bucket | side | P(win) | ask | edge |")
            lines.append("|--------|--------|------|--------|-----|------|")
            for m, o in city_pairs:
                lines.append(
                    f"| {_bet_label(m)} | {o.bucket_label} | {o.side} | {o.p_win:.2f}"
                    f" | {o.best_ask:.2f} | {o.edge:+.2f} |"
                )
            lines.append("")
    else:
        lines.append("_No bets pass the gates today._")
        lines.append("")
    for m in report.markets:
        lines.append(f"## {m.title}")
        lines.append("")
        lines.append(
            f"- station: {m.station}  variable: {m.variable}"
            f"  settlement: {m.settlement_date.isoformat()}  venue: {m.venue}"
        )
        lines.append(f"- sources: {m.n_sources}")
        if m.mu is not None and m.sigma is not None:
            cal = "calibrated" if m.calibrated else "uncalibrated"
            if m.variable == "PRCP":
                lines.append(f"- forecast: mu={m.mu:.2f}in sigma={m.sigma:.2f}in ({cal})")
            else:
                unit = getattr(m, "unit", "F")
                lines.append(f"- forecast: mu={m.mu:.1f}{unit} sigma={m.sigma:.1f}{unit} ({cal})")
        elif (m.mu is None) != (m.sigma is None):
            lines.append("- forecast: partial data (only one of mu/sigma available)")
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
