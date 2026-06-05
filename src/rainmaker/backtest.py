"""Backtest the forecast model over history: calibration and win-rate.

No betting P/L; that needs historical market prices we do not have. This measures
whether the forecast distributions are honest: how often the actual lands in the
modal bucket, whether claimed probabilities match realized frequencies
(reliability), and whether the central predictive intervals cover at their
nominal rate. The evidence base for the confidence-floor question (#58).

Lead time is nominal: the historical-forecast archive returns one series per
date, so this reports at a single horizon (~lead 1), the same data backfill uses.
"""

from collections import defaultdict
from datetime import date
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict
from scipy.stats import norm

from rainmaker.backfill import fetch_actuals, fetch_historical_forecasts
from rainmaker.config import Station
from rainmaker.polymarket.markets import Bucket, BucketKind, Market, parse_market
from rainmaker.probability.distribution import Gaussian
from rainmaker.probability.outcomes import bucket_probability, settles

COVERAGE_LEVELS = (0.50, 0.80, 0.90)


def _bucket(
    label: str,
    kind: BucketKind,
    lo: int | None = None,
    hi: int | None = None,
    threshold: int | None = None,
) -> Bucket:
    return Bucket(
        label=label,
        kind=kind,
        lo=lo,
        hi=hi,
        threshold=threshold,
        yes_token_id="",
        best_ask=None,
        best_bid=None,
        yes_price=0.0,
    )


def standard_buckets(center: float, *, width: int = 2, span: int = 10) -> list[Bucket]:
    """A market-style ladder centered on round(center).

    A below-tail, contiguous width-degree range buckets covering center +/- span,
    and an above-tail. Range los are aligned to multiples of width (so 60-61,
    62-63, ...), matching real markets, and the tails close the partition so the
    bucket probabilities sum to 1.
    """
    c = round(center)
    lo_start = (c - span) - ((c - span) % width)
    n_ranges = (2 * span) // width
    ranges: list[tuple[int, int]] = []
    lo = lo_start
    for _ in range(n_ranges):
        hi = lo + width - 1
        ranges.append((lo, hi))
        lo = hi + 1
    below_t = lo_start - 1
    above_t = ranges[-1][1] + 1
    buckets = [_bucket(f"{below_t}°F or below", "below", threshold=below_t)]
    buckets += [_bucket(f"{lo}-{hi}°F", "range", lo=lo, hi=hi) for lo, hi in ranges]
    buckets.append(_bucket(f"{above_t}°F or higher", "above", threshold=above_t))
    return buckets


class DayScore(BaseModel):
    model_config = ConfigDict(frozen=True)

    modal_p: float
    modal_won: bool
    brier: float
    coverage: dict[float, bool]
    pairs: list[tuple[float, bool]]  # (p_win, won) per bucket, for reliability


class ReliabilityBin(BaseModel):
    model_config = ConfigDict(frozen=True)

    lo: float
    hi: float
    predicted_mean: float
    observed_freq: float
    count: int


class BacktestResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    n: int
    modal_hit_rate: float
    mean_modal_p: float
    mean_brier: float
    coverage: dict[float, float]
    reliability: list[ReliabilityBin]


def score_day(g: Gaussian, buckets: list[Bucket], actual: float) -> DayScore:
    """Score one (forecast, actual) day over a bucket ladder."""
    probs = [bucket_probability(g, b) for b in buckets]
    wins = [settles(b.kind, b.lo, b.hi, b.threshold, actual) for b in buckets]
    modal_i = max(range(len(buckets)), key=lambda i: probs[i])
    brier = sum((p - (1.0 if w else 0.0)) ** 2 for p, w in zip(probs, wins, strict=True))
    cdf_actual = float(norm.cdf(actual, loc=g.mu, scale=g.sigma))
    coverage = {q: abs(cdf_actual - 0.5) <= q / 2 for q in COVERAGE_LEVELS}
    return DayScore(
        modal_p=probs[modal_i],
        modal_won=wins[modal_i],
        brier=brier,
        coverage=coverage,
        pairs=list(zip(probs, wins, strict=True)),
    )


def reliability_bins(pairs: list[tuple[float, bool]], *, n_bins: int = 10) -> list[ReliabilityBin]:
    """Bin (predicted prob, won) pairs and report observed frequency per bin."""
    buckets: list[list[tuple[float, bool]]] = [[] for _ in range(n_bins)]
    for p, won in pairs:
        idx = min(int(p * n_bins), n_bins - 1)
        buckets[idx].append((p, won))
    out: list[ReliabilityBin] = []
    for i, group in enumerate(buckets):
        if not group:
            continue
        count = len(group)
        out.append(
            ReliabilityBin(
                lo=i / n_bins,
                hi=(i + 1) / n_bins,
                predicted_mean=sum(p for p, _ in group) / count,
                observed_freq=sum(1 for _, w in group if w) / count,
                count=count,
            )
        )
    return out


def aggregate(days: list[DayScore]) -> BacktestResult:
    """Roll day scores up into one result."""
    n = len(days)
    if n == 0:
        raise ValueError("cannot aggregate an empty backtest")
    all_pairs = [pair for d in days for pair in d.pairs]
    return BacktestResult(
        n=n,
        modal_hit_rate=sum(1 for d in days if d.modal_won) / n,
        mean_modal_p=sum(d.modal_p for d in days) / n,
        mean_brier=sum(d.brier for d in days) / n,
        coverage={q: sum(1 for d in days if d.coverage[q]) / n for q in COVERAGE_LEVELS},
        reliability=reliability_bins(all_pairs),
    )


def backtest_synthetic(
    station: Station,
    variable: str,
    start: date,
    end: date,
    client: httpx.Client,
    *,
    width: int = 2,
    span: int = 10,
) -> BacktestResult | None:
    """Backtest one station over history with a synthetic bucket ladder per day.

    Returns None if no day has both a forecast and an actual.
    """
    forecasts = fetch_historical_forecasts(station, start, end, client)
    actuals = fetch_actuals(station.ghcnd_id, start, end, client, variable)
    days = [
        score_day(g, standard_buckets(g.mu, width=width, span=span), actuals[d])
        for d, g in sorted(forecasts.items())
        if d in actuals
    ]
    return aggregate(days) if days else None


def combine(results: list[BacktestResult]) -> BacktestResult:
    """Merge per-city results into one, n-weighting metrics and reliability bins."""
    if not results:
        raise ValueError("cannot combine zero results")
    n = sum(r.n for r in results)

    def weighted(attr: str) -> float:
        return float(sum(getattr(r, attr) * r.n for r in results)) / n

    by_lo: dict[float, list[ReliabilityBin]] = defaultdict(list)
    for r in results:
        for b in r.reliability:
            by_lo[b.lo].append(b)
    reliability: list[ReliabilityBin] = []
    for lo in sorted(by_lo):
        group = by_lo[lo]
        c = sum(b.count for b in group)
        reliability.append(
            ReliabilityBin(
                lo=lo,
                hi=group[0].hi,
                predicted_mean=sum(b.predicted_mean * b.count for b in group) / c,
                observed_freq=sum(b.observed_freq * b.count for b in group) / c,
                count=c,
            )
        )
    return BacktestResult(
        n=n,
        modal_hit_rate=weighted("modal_hit_rate"),
        mean_modal_p=weighted("mean_modal_p"),
        mean_brier=weighted("mean_brier"),
        coverage={q: sum(r.coverage[q] * r.n for r in results) / n for q in COVERAGE_LEVELS},
        reliability=reliability,
    )


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def _row(label: str, r: BacktestResult) -> str:
    return (
        f"| {label} | {r.n} | {_pct(r.modal_hit_rate)} | {_pct(r.mean_modal_p)} | "
        f"{r.mean_brier:.3f} | {_pct(r.coverage[0.5])} | {_pct(r.coverage[0.8])} | "
        f"{_pct(r.coverage[0.9])} |"
    )


def render_report(
    synthetic: dict[str, BacktestResult], real: BacktestResult | None
) -> tuple[str, dict[str, Any]]:
    """Markdown report plus a JSON-able payload. Terminal prints the markdown."""
    overall = combine(list(synthetic.values()))
    header = "| {} | n | Modal hit | Mean modal p | Brier | Cov50 | Cov80 | Cov90 |"
    rule = "| --- | --- | --- | --- | --- | --- | --- | --- |"
    lines = [
        "# Forecast backtest",
        "",
        "Calibration and win-rate over history at the archive horizon (~lead 1). "
        "No betting P/L; that needs historical market prices (#59 Part 2).",
        "",
        "## Synthetic ladder (long history)",
        "",
        header.format("City"),
        rule,
    ]
    lines += [_row(city, synthetic[city]) for city in sorted(synthetic)]
    lines.append(_row("ALL", overall))
    lines += [
        "",
        "### Reliability (overall): does a claimed probability happen that often?",
        "",
        "| Predicted bin | Predicted mean | Observed | n |",
        "| --- | --- | --- | --- |",
    ]
    for b in overall.reliability:
        lines.append(
            f"| {_pct(b.lo)}-{_pct(b.hi)} | {_pct(b.predicted_mean)} | "
            f"{_pct(b.observed_freq)} | {b.count} |"
        )
    if real is not None:
        lines += [
            "",
            "## Real closed-market reality check",
            "",
            header.format(""),
            rule,
            _row("real markets", real),
        ]
    md = "\n".join(lines) + "\n"
    payload = {
        "synthetic": {c: r.model_dump(mode="json") for c, r in synthetic.items()},
        "overall": overall.model_dump(mode="json"),
        "real": real.model_dump(mode="json") if real is not None else None,
    }
    return md, payload


def backtest_real(
    events: list[dict[str, Any]], client: httpx.Client, *, on_or_after: date
) -> BacktestResult | None:
    """Reality check over real closed markets: score their actual buckets.

    Parses each closed event, keeps TMAX markets settling on or after the cutoff
    (the archive forecast fetch is TMAX-only), then scores each against the
    regenerated forecast and the NCEI actual using the market's real buckets.
    Returns None if nothing scorable remains.
    """
    markets: list[Market] = []
    for ev in events:
        try:
            market = parse_market(ev)
        except (ValueError, KeyError):
            continue  # not a US-city temperature market, or unmapped station
        if market.target.variable == "TMAX" and market.target.local_date >= on_or_after:
            markets.append(market)
    if not markets:
        return None

    by_station: dict[str, list[Market]] = defaultdict(list)
    for market in markets:
        by_station[market.target.station.icao].append(market)

    days: list[DayScore] = []
    for group in by_station.values():
        station = group[0].target.station
        dates = [m.target.local_date for m in group]
        forecasts = fetch_historical_forecasts(station, min(dates), max(dates), client)
        actuals = fetch_actuals(station.ghcnd_id, min(dates), max(dates), client, "TMAX")
        for market in group:
            g = forecasts.get(market.target.local_date)
            actual = actuals.get(market.target.local_date)
            if g is None or actual is None:
                continue
            days.append(score_day(g, market.buckets, actual))
    return aggregate(days) if days else None
