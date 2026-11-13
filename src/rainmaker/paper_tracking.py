"""Grade paper orders and report on them against the go-live criterion (#492).

Grading uses the advisory grader (tracking._bet_won), so a paper order and an
advisory prediction on the same bucket and side never disagree on a winner. The
report is computed on read from paper_orders, outcomes and markets. It never
selects book_snapshot (#469).

The go-live criterion is in docs/superpowers/specs/2026-11-01-mvp3-trading-design.md.
"""

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from rainmaker.store.db import Conn
from rainmaker.tracking import (
    ROI_INTERVAL_LEVEL as ROI_INTERVAL_LEVEL,
)
from rainmaker.tracking import (
    ROI_INTERVAL_MIN_DATES,
    RoiInterval,
    _best_per_market_run,
    _bet_won,
    bootstrap_roi_interval,
)
from rainmaker.tracking import (
    ROI_INTERVAL_RESAMPLES as ROI_INTERVAL_RESAMPLES,
)

GO_LIVE_MIN_ORDERS = 400
GO_LIVE_MIN_DATES = 60
GO_LIVE_MIN_NET_ROI = 0.01
GO_LIVE_MAX_GAP_USDC = 0.005

# 0.5 - 0.495 is 0.0050000000000000044 in floating point, so a gap that is exactly
# at the bar on paper would otherwise fail it.
_THRESHOLD_TOL = 1e-9

_FILLED = ("filled", "partial")


@dataclass(frozen=True)
class PaperEra:
    start: datetime  # timezone-aware UTC
    label: str


# One entry per policy era, oldest first. An order belongs to the latest era whose
# start is at or before its created_at; the go-live verdict uses the latest era only.
# Add an entry whenever the gate or calibration changes (the era log is in
# docs/architecture/recommendation-gate.md). A prod calibration refit through
# backfill.yml has no PR but is an era change too, so it needs an entry as well.
PAPER_ERAS: tuple[PaperEra, ...] = (
    PaperEra(datetime(2026, 11, 3, 7, 36, 10, tzinfo=UTC), "fee-aware gate (#491)"),
)


def paper_pnl(shares: float, fill_price: float, fee_usdc: float | None, won: bool) -> float:
    """Realized P&L after the fee. fill_price is the price of the token bought, so
    this holds for both sides: a winning share pays 1 USDC."""
    outlay = _outlay(shares, fill_price, fee_usdc)
    return shares - outlay if won else -outlay


def _outlay(shares: float, fill_price: float, fee_usdc: float | None) -> float:
    return shares * fill_price + (fee_usdc or 0.0)


def _grade(row: Any, actual_value: float) -> tuple[int, float] | None:
    won = _bet_won(
        {
            "variable": row["variable"],
            "bucket": row["outcome"],
            "actual_value": actual_value,
            "outcome_spec": row["outcome_spec"],
            "side": row["side"],
        }
    )
    if won is None:
        return None
    return int(won), paper_pnl(row["shares"], row["fill_price"], row["fee_usdc"], won)


_GRADE_COLS = (
    "SELECT p.id, p.outcome, p.side, p.shares, p.fill_price, p.fee_usdc, m.variable, m.outcome_spec"
)
_GRADE_FROM = (
    " FROM paper_orders p JOIN markets m ON m.id = p.market_id "
    "JOIN outcomes o ON o.market_id = p.market_id "
)
_GRADE_WHERE = (
    "WHERE p.status IN ('filled', 'partial') AND p.shares IS NOT NULL "
    "AND p.fill_price IS NOT NULL AND o.actual_value IS NOT NULL "
)


def grade_paper_orders(conn: Conn) -> int:
    """Fill won and pnl_usdc for filled or partial orders whose market has settled.

    Idempotent: only rows with won IS NULL are selected. A row the grader cannot
    read stays NULL (the report counts it as settled but ungradable). Returns the
    number of rows graded.
    """
    rows = conn.execute(
        _GRADE_COLS
        + ", o.actual_value AS actual_value"
        + _GRADE_FROM
        + _GRADE_WHERE
        + "AND p.won IS NULL"
    ).fetchall()
    graded = 0
    for row in rows:
        result = _grade(row, row["actual_value"])
        if result is None:
            continue
        conn.execute(
            "UPDATE paper_orders SET won = ?, pnl_usdc = ? WHERE id = ?",
            (result[0], result[1], row["id"]),
        )
        graded += 1
    conn.commit()
    return graded


def regrade_paper_orders_for_market(conn: Conn, market_id: str, actual_value: float) -> None:
    """Re-grade every filled or partial order on one market against a new actual.

    Overwrites won and pnl_usdc unconditionally, and clears them when the new
    value leaves the row ungradable. Used by regrade_polymarket_settlements after
    the outcome is overwritten.
    """
    rows = conn.execute(
        _GRADE_COLS
        + " FROM paper_orders p JOIN markets m ON m.id = p.market_id "
        + "WHERE p.market_id = ? AND p.status IN ('filled', 'partial') "
        + "AND p.shares IS NOT NULL AND p.fill_price IS NOT NULL",
        (market_id,),
    ).fetchall()
    for row in rows:
        result = _grade(row, actual_value)
        won, pnl = result if result is not None else (None, None)
        conn.execute(
            "UPDATE paper_orders SET won = ?, pnl_usdc = ? WHERE id = ?", (won, pnl, row["id"])
        )
    conn.commit()


def load_paper_rows(conn: Conn) -> list[dict[str, Any]]:
    """Every paper order with its market's settlement date and settled flag.

    Columns are listed explicitly: book_snapshot is a large JSON blob the report
    never needs, and the table is never pruned (#469).
    """
    rows = conn.execute(
        "SELECT p.id AS id, p.run_id AS run_id, p.market_id AS market_id, "
        "p.outcome AS outcome, p.side AS side, p.status AS status, p.shares AS shares, "
        "p.fill_price AS fill_price, p.fee_usdc AS fee_usdc, p.p_win AS p_win, "
        "p.edge AS edge, p.quoted_ask AS quoted_ask, p.won AS won, "
        "p.pnl_usdc AS pnl_usdc, p.created_at AS created_at, "
        "m.settlement_date AS settlement_date, "
        "CASE WHEN o.market_id IS NULL THEN 0 ELSE 1 END AS settled "
        "FROM paper_orders p JOIN markets m ON m.id = p.market_id "
        "LEFT JOIN outcomes o ON o.market_id = p.market_id "
        "ORDER BY p.id"
    ).fetchall()
    return [dict(r) for r in rows]


@dataclass(frozen=True)
class EraCounts:
    label: str
    orders: int
    sample: int


@dataclass(frozen=True)
class BestView:
    """The sample collapsed to one order per (market, run): the highest-edge one.

    Orders on one market's buckets are correlated, so this is the stricter read.
    It is shown next to the order view and never feeds the verdict.
    """

    n: int
    pnl: float
    outlay: float
    roi: float | None


@dataclass(frozen=True)
class Check:
    name: str
    value: float | None
    threshold: float
    passed: bool
    detail: str


@dataclass(frozen=True)
class PaperReport:
    era: PaperEra | None
    status_counts: dict[str, int]
    fill_rate: float | None
    sample: int
    open_fills: int
    ungradable: int
    dates: int
    pnl: float
    outlay: float
    net_roi: float | None
    gap: float | None
    gap_n: int
    pre_era_orders: int
    eras: tuple[EraCounts, ...]
    best: BestView
    checks: tuple[Check, ...]
    verdict: str
    roi_interval: RoiInterval | None = None


def _era_index(created_at: str, eras: tuple[PaperEra, ...]) -> int | None:
    """Index of the latest era starting at or before created_at, else None (pre-era)."""
    created = datetime.fromisoformat(created_at)
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    found: int | None = None
    for i, era in enumerate(eras):
        if era.start <= created:
            found = i
    return found


def _roi(pnl: float, outlay: float) -> float | None:
    return pnl / outlay if outlay else None


def _best_view(sample: list[dict[str, Any]]) -> BestView:
    candidates = [
        {
            **r,
            "recommended": 1,
            "bucket": r["outcome"],
            "p_win": r["p_win"] if r["p_win"] is not None else -math.inf,
        }
        for r in sample
    ]
    best = _best_per_market_run(candidates)
    pnl = sum(r["pnl_usdc"] for r in best)
    outlay = sum(_outlay(r["shares"], r["fill_price"], r["fee_usdc"]) for r in best)
    return BestView(n=len(best), pnl=pnl, outlay=outlay, roi=_roi(pnl, outlay))


def _bootstrap_roi_interval(sample: list[dict[str, Any]]) -> RoiInterval | None:
    """Net-ROI interval over the sample, with settlement dates as the clusters.

    The numeric core is tracking.bootstrap_roi_interval, shared with `track`.
    """
    by_date: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for r in sample:
        # settlement_date is nullable in the schema; every app writer supplies
        # it, but a row without one clusters under "" rather than crashing
        # the sort (a robustness regression the #513 review asked to close).
        acc = by_date[r["settlement_date"] or ""]
        acc[0] += r["pnl_usdc"]
        acc[1] += _outlay(r["shares"], r["fill_price"], r["fee_usdc"])
    return bootstrap_roi_interval({d: (v[0], v[1]) for d, v in by_date.items()})


def _checks(sample: int, dates: int, roi: float | None, gap: float | None) -> tuple[Check, ...]:
    def fmt(v: float | None, spec: str) -> str:
        return "n/a" if v is None else format(v, spec)

    return (
        Check(
            "sample",
            float(sample),
            float(GO_LIVE_MIN_ORDERS),
            sample >= GO_LIVE_MIN_ORDERS,
            f"{sample} filled and settled orders, need >= {GO_LIVE_MIN_ORDERS}",
        ),
        Check(
            "dates",
            float(dates),
            float(GO_LIVE_MIN_DATES),
            dates >= GO_LIVE_MIN_DATES,
            f"{dates} settlement dates, need >= {GO_LIVE_MIN_DATES}",
        ),
        Check(
            "net ROI",
            roi,
            GO_LIVE_MIN_NET_ROI,
            roi is not None and roi >= GO_LIVE_MIN_NET_ROI - _THRESHOLD_TOL,
            f"{fmt(roi, '+.2%')}, need >= {GO_LIVE_MIN_NET_ROI:+.2%}",
        ),
        Check(
            "execution gap",
            gap,
            GO_LIVE_MAX_GAP_USDC,
            gap is not None and gap <= GO_LIVE_MAX_GAP_USDC + _THRESHOLD_TOL,
            f"{fmt(gap, '+.4f')} USDC/share, need <= {GO_LIVE_MAX_GAP_USDC:.4f}",
        ),
    )


def _verdict(checks: tuple[Check, ...]) -> str:
    passed = {c.name: c.passed for c in checks}
    if not (passed["sample"] and passed["dates"]):
        return "PENDING"
    return "GO" if passed["net ROI"] and passed["execution gap"] else "NO-GO"


def compute_paper_report(
    rows: list[dict[str, Any]], eras: tuple[PaperEra, ...] = PAPER_ERAS
) -> PaperReport:
    """Pure: rows from load_paper_rows in, the go-live report out.

    The headline numbers cover the latest era only. ROI, fill rate and the gap
    filter on status, never on fill_price: a below-minimum no_fill row carries a
    price and walked shares but is not a position.
    """
    by_era: dict[int | None, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_era[_era_index(r["created_at"], eras)].append(r)

    def sample_of(era_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            r for r in era_rows if r["status"] in _FILLED and r["settled"] and r["won"] is not None
        ]

    latest = len(eras) - 1 if eras else None
    era_rows = by_era.get(latest, []) if latest is not None else []
    sample = sample_of(era_rows)

    status_counts: dict[str, int] = {"filled": 0, "partial": 0, "no_fill": 0}
    for r in era_rows:
        status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1
    n_orders = len(era_rows)
    n_filled = sum(1 for r in era_rows if r["status"] in _FILLED)

    pnl = sum(r["pnl_usdc"] for r in sample)
    outlay = sum(_outlay(r["shares"], r["fill_price"], r["fee_usdc"]) for r in sample)
    net_roi = _roi(pnl, outlay)

    gaps = [
        r["fill_price"] - r["quoted_ask"]
        for r in sample
        if r["fill_price"] is not None and r["quoted_ask"] is not None
    ]
    gap = sum(gaps) / len(gaps) if gaps else None
    dates = len({r["settlement_date"] for r in sample})

    checks = _checks(len(sample), dates, net_roi, gap)
    return PaperReport(
        era=eras[latest] if latest is not None else None,
        status_counts=status_counts,
        fill_rate=n_filled / n_orders if n_orders else None,
        sample=len(sample),
        open_fills=sum(1 for r in era_rows if r["status"] in _FILLED and not r["settled"]),
        ungradable=sum(
            1 for r in era_rows if r["status"] in _FILLED and r["settled"] and r["won"] is None
        ),
        dates=dates,
        pnl=pnl,
        outlay=outlay,
        net_roi=net_roi,
        gap=gap,
        gap_n=len(gaps),
        pre_era_orders=len(by_era.get(None, [])),
        eras=tuple(
            EraCounts(era.label, len(by_era.get(i, [])), len(sample_of(by_era.get(i, []))))
            for i, era in enumerate(eras)
        ),
        best=_best_view(sample),
        checks=checks,
        verdict=_verdict(checks),
        roi_interval=_bootstrap_roi_interval(sample),
    )


def render_paper_report(report: PaperReport) -> str:
    lines = ["Paper-trade go-live report"]
    era = report.era
    lines.append(
        f"era: {era.label} (since {era.start.date().isoformat()})" if era else "era: none defined"
    )
    sc = report.status_counts
    fill = "n/a" if report.fill_rate is None else f"{report.fill_rate:.1%}"
    lines.append(
        f"orders: {sum(sc.values())}  (filled {sc['filled']}, partial {sc['partial']}, "
        f"no_fill {sc['no_fill']})  fill rate {fill}"
    )
    lines.append(
        f"sample: {report.sample} filled and settled  "
        f"(open {report.open_fills}, settled but ungradable {report.ungradable})"
    )
    lines.append(f"settlement dates: {report.dates}")
    roi = "n/a" if report.net_roi is None else f"{report.net_roi:+.2%}"
    lines.append(
        f"net P&L {report.pnl:+.4f} USDC on outlay {report.outlay:.4f} USDC  net ROI {roi}"
    )
    gap = "n/a" if report.gap is None else f"{report.gap:+.4f}"
    lines.append(f"mean execution gap (fill - quoted ask): {gap} USDC/share over {report.gap_n}")
    b = report.best
    best_roi = "n/a" if b.roi is None else f"{b.roi:+.2%}"
    lines.append(
        f"best per market (not in the verdict): {b.n} orders, P&L {b.pnl:+.4f} on outlay "
        f"{b.outlay:.4f}, ROI {best_roi}"
    )
    lines.append("")
    lines.append("per era (orders, filled-and-settled sample):")
    for e in report.eras:
        lines.append(f"  {e.label}: {e.orders} orders, {e.sample} in sample")
    lines.append(f"  pre-era (never counted): {report.pre_era_orders} orders")
    lines.append("")
    lines.append("go-live checks:")
    for c in report.checks:
        lines.append(f"  [{'pass' if c.passed else 'fail'}] {c.name}: {c.detail}")
    lines.append("")
    iv = report.roi_interval
    if iv is None:
        lines.append(
            f"too few settlement dates ({report.dates}) for a bootstrap ROI interval "
            f"(need >= {ROI_INTERVAL_MIN_DATES})"
        )
    else:
        lines.append(
            f"net ROI {iv.level:.0%} bootstrap interval: {iv.lo:+.1%} to {iv.hi:+.1%} "
            f"({iv.resamples} resamples over {iv.dates} settlement dates; "
            f"informational, not a go-live check)"
        )
    lines.append(f"verdict: {report.verdict}")
    return "\n".join(lines)
