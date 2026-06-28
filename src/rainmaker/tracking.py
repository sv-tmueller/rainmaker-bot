"""Score the bot against settled outcomes: hypothetical P&L and calibration.

Computed on read from predictions + prices + outcomes. One one-unit bet per
(market, UTC day): the best-edge recommended side/bucket from that day's latest
run. Buckets on one market describe the same temperature, so correlated
same-market bets collapse to one; the intraday runs (#77) that re-price a market
many times a day are correlated too, so they collapse to the latest run per UTC
day (#63, #78). Tracking only covers rows with a bucket recorded.
"""

import json
from collections import defaultdict
from datetime import UTC, date, datetime
from typing import Any

import httpx
from scipy.stats import norm

from rainmaker.backtest import COVERAGE_LEVELS, crps_gaussian, reliability_bins
from rainmaker.config import KALSHI_STATIONS, STATIONS
from rainmaker.domain import BucketKind, Market, parse_bucket_label, parse_precip_bracket_label
from rainmaker.polymarket.prices import fetch_price_history, last_before
from rainmaker.probability.calibration import Accuracy, CalibrationPair, compute_accuracy
from rainmaker.probability.outcomes import settles
from rainmaker.probability.precip_outcomes import precip_settles
from rainmaker.store.db import Conn
from rainmaker.store.record import save_accuracy


def _won(
    variable: str,
    bucket_label: str,
    actual_value: float,
    outcome_spec: str | None = None,
) -> bool:
    # Try to grade from the structured spec stored at record time. This handles
    # Kalshi labels ("74° to 75°", '2" to 3"') that the Polymarket-style parsers
    # cannot read. Fall back to the label parsers for legacy rows (NULL spec) or
    # rows where the label is absent from the spec.
    if outcome_spec:
        try:
            spec_list: list[dict[str, Any]] = json.loads(outcome_spec)
            for entry in spec_list:
                if entry.get("label") == bucket_label:
                    kind: BucketKind = entry["kind"]
                    if variable == "PRCP":
                        lo: float | None = entry["lo"]
                        hi: float | None = entry["hi"]
                        threshold: float | None = entry["threshold"]
                        return precip_settles(kind, lo, hi, threshold, actual_value)
                    else:
                        lo_i: int | None = entry["lo"]
                        hi_i: int | None = entry["hi"]
                        threshold_i: int | None = entry["threshold"]
                        return settles(kind, lo_i, hi_i, threshold_i, actual_value)
        except (json.JSONDecodeError, KeyError, TypeError):
            pass  # unparseable spec: fall through to label parser
    if variable == "PRCP":
        return precip_settles(*parse_precip_bracket_label(bucket_label), actual_value)
    return settles(*parse_bucket_label(bucket_label), actual_value)


def _bet_won(row: dict[str, Any]) -> bool:
    """A YES bet wins when the bucket settles; a NO bet wins when it does not."""
    settled = _won(row["variable"], row["bucket"], row["actual_value"], row.get("outcome_spec"))
    return (not settled) if (row.get("side") or "YES") == "NO" else settled


def _latest_run_per_market_day(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only the latest run's rows per (market, UTC day).

    The intraday runs (#77) re-price a market many times a day; their bets are
    correlated, so counting each run separately inflates P&L and calibration
    (#63). started_at[:10] is the UTC day (same grain as compute_live_accuracy).
    Among rows sharing a (market_id, UTC day), keep only those whose run started
    latest; (started_at, run_id) breaks an exact-timestamp tie deterministically.
    """
    latest: dict[tuple[str, str], tuple[str, str]] = {}
    for r in rows:
        key = (r["market_id"], r["started_at"][:10])
        marker = (r["started_at"], r["run_id"])
        if key not in latest or marker > latest[key]:
            latest[key] = marker
    keep = {(market_id, run_id) for (market_id, _), (_, run_id) in latest.items()}
    return [r for r in rows if (r["market_id"], r["run_id"]) in keep]


def _settled_rows(conn: Conn) -> list[dict[str, Any]]:
    # Match the price to the prediction's side; legacy rows with a null side are YES.
    # city, settlement_date, and raw are carried for compute_attribution and compute_clv;
    # compute_pnl and compute_calibration ignore these extra keys.
    rows = conn.execute(
        "SELECT p.market_id AS market_id, p.run_id AS run_id, p.bucket AS bucket, "
        "p.side AS side, p.p_win AS p_win, p.edge AS edge, "
        "p.recommended AS recommended, m.variable AS variable, m.venue AS venue, "
        "m.outcome_spec AS outcome_spec, m.city AS city, "
        "m.settlement_date AS settlement_date, m.raw AS raw, r.started_at AS started_at, "
        "pr.price AS ask, o.actual_value AS actual_value "
        "FROM predictions p "
        "JOIN markets m ON m.id = p.market_id "
        "JOIN outcomes o ON o.market_id = p.market_id "
        "JOIN runs r ON r.id = p.run_id "
        "JOIN prices pr ON pr.run_id = p.run_id AND pr.market_id = p.market_id "
        "AND pr.outcome = p.bucket "
        "AND COALESCE(pr.side, 'YES') = COALESCE(p.side, 'YES') "
        "WHERE p.bucket IS NOT NULL AND pr.price IS NOT NULL"
    ).fetchall()
    return _latest_run_per_market_day([dict(r) for r in rows])


def _edge_key(r: dict[str, Any]) -> tuple[float, float, str, str]:
    edge = r["edge"] if r["edge"] is not None else float("-inf")
    return (edge, r["p_win"], r["bucket"], r.get("side") or "YES")


def _best_per_market_run(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse recommended bets to one per (market, run): the highest-edge bet.

    Buckets on one market all describe the same temperature, so NO bets across
    buckets win or lose together. Counting each separately would inflate P&L and
    hit rate, so keep only the best-edge bet per (market, run). Tie-break on
    (edge, p_win, bucket, side) for a deterministic pick.
    """
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        if not r["recommended"]:
            continue
        key = (r["market_id"], r["run_id"])
        if key not in best or _edge_key(r) > _edge_key(best[key]):
            best[key] = r
    return list(best.values())


def _filter_venue(rows: list[dict[str, Any]], venue: str | None) -> list[dict[str, Any]]:
    """Keep rows for one venue; legacy rows with a null venue count as polymarket."""
    if venue is None:
        return rows
    return [r for r in rows if (r.get("venue") or "polymarket") == venue]


def compute_pnl(conn: Conn, venue: str | None = None) -> dict[str, Any]:
    """Hypothetical P&L over recommended bets at a flat one-unit stake.

    With venue set ("polymarket" / "kalshi"), restrict to that venue's markets."""
    total_pnl = 0.0
    total_staked = 0.0
    wins = 0
    n = 0
    for r in _best_per_market_run(_filter_venue(_settled_rows(conn), venue)):
        n += 1
        ask = r["ask"]
        total_staked += ask
        if _bet_won(r):
            wins += 1
            total_pnl += 1 - ask
        else:
            total_pnl -= ask
    roi = total_pnl / total_staked if total_staked else 0.0
    return {
        "n_bets": n,
        "wins": wins,
        "losses": n - wins,
        "total_pnl": total_pnl,
        "roi": roi,
    }


def compute_calibration(conn: Conn, venue: str | None = None) -> dict[str, Any]:
    """Brier over the settled YES bucket-predictions, plus recommended hit rate.

    With venue set, restrict to that venue's markets."""
    rows = _filter_venue(_settled_rows(conn), venue)
    if not rows:
        return {"n": 0, "brier": None, "hit_rate": None}
    # Brier measures forecast calibration over the YES bucket-predictions; each NO
    # row's contribution is identical to its YES twin, so including it would only
    # double n. Hit rate is over the one best-edge bet per (market, run), either side.
    yes_rows = [r for r in rows if (r.get("side") or "YES") == "YES"]
    brier = (
        sum(
            (
                r["p_win"]
                - (
                    1.0
                    if _won(r["variable"], r["bucket"], r["actual_value"], r.get("outcome_spec"))
                    else 0.0
                )
            )
            ** 2
            for r in yes_rows
        )
        / len(yes_rows)
        if yes_rows
        else None
    )
    bets = _best_per_market_run(rows)
    hit_rate = sum(1 for r in bets if _bet_won(r)) / len(bets) if bets else None
    return {"n": len(yes_rows), "brier": brier, "hit_rate": hit_rate}


def _wilson_interval(wins: int, n: int) -> tuple[float, float]:
    """Wilson score 95% confidence interval for a proportion.

    Returns (lo, hi). When n=0, returns (0.0, 1.0) to signal full uncertainty.
    """
    if n == 0:
        return (0.0, 1.0)
    z = 1.96
    p = wins / n
    denom = 1 + z**2 / n
    center = (p + z**2 / (2 * n)) / denom
    margin = z * (p * (1 - p) / n + z**2 / (4 * n**2)) ** 0.5 / denom
    return (center - margin, center + margin)


def _lead_bucket(settlement_date: str, started_at: str) -> str:
    """Map (settlement_date, started_at) to a lead-time bucket label.

    Buckets: 0, 1, 2, 3+ (3 or more days), <0 (catch-up run after settlement).
    Negatives fold into '<0 (catch-up)' rather than being dropped so every bet
    lands in exactly one bucket and dimension totals reconcile with compute_pnl.
    """
    lead = (date.fromisoformat(settlement_date) - date.fromisoformat(started_at[:10])).days
    if lead < 0:
        return "<0 (catch-up)"
    if lead <= 2:
        return str(lead)
    return "3+"


def _edge_bucket(edge: float | None) -> str:
    """Map edge to a half-open bucket label. NULL or sub-0.05 edges share one bucket."""
    if edge is None or edge < 0.05:
        return "<.05"
    if edge < 0.10:
        return "[.05,.10)"
    if edge < 0.20:
        return "[.10,.20)"
    return "[.20,inf)"


def _p_win_bucket(p_win: float) -> str:
    """Map p_win to a half-open bucket label. Sub-0.75 values share the lowest bucket."""
    if p_win < 0.75:
        return "<.75"
    if p_win < 0.80:
        return "[.75,.80)"
    if p_win < 0.90:
        return "[.80,.90)"
    if p_win < 0.95:
        return "[.90,.95)"
    return "[.95,1.0]"


def _segment_stats(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    """Group rows by key, compute per-segment stats, return sorted by segment label."""
    groups: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"n": 0, "wins": 0, "losses": 0, "pnl": 0.0, "staked": 0.0}
    )
    for r in rows:
        seg = r[key]
        ask = r["ask"]
        g = groups[seg]
        g["n"] += 1
        g["staked"] += ask
        if _bet_won(r):
            g["wins"] += 1
            g["pnl"] += 1 - ask
        else:
            g["losses"] += 1
            g["pnl"] -= ask

    out: list[dict[str, Any]] = []
    for seg, g in sorted(groups.items()):
        n = g["n"]
        wins = g["wins"]
        lo, hi = _wilson_interval(wins, n)
        out.append(
            {
                "segment": seg,
                "n": n,
                "wins": wins,
                "losses": g["losses"],
                "win_pct": wins / n if n else 0.0,
                "wilson_lo": lo,
                "wilson_hi": hi,
                "pnl": g["pnl"],
                "staked": g["staked"],
                "roi": g["pnl"] / g["staked"] if g["staked"] else 0.0,
            }
        )
    return out


def compute_attribution(conn: Conn) -> dict[str, list[dict[str, Any]]]:
    """Per-segment P&L attribution across six dimensions.

    Built from a single deduplicated bet list (same population as compute_pnl).
    Each dimension is an exhaustive partition, so per-dimension totals reconcile
    with compute_pnl's headline n/wins/losses/pnl/roi.
    """
    bets = _best_per_market_run(_settled_rows(conn))
    # Attach bucketed keys for each attribution dimension
    tagged: list[dict[str, Any]] = []
    for r in bets:
        t = dict(r)
        t["_venue"] = r.get("venue") or "polymarket"
        t["_lead"] = _lead_bucket(r["settlement_date"], r["started_at"])
        t["_edge"] = _edge_bucket(r.get("edge"))
        t["_p_win"] = _p_win_bucket(r["p_win"])
        tagged.append(t)

    return {
        "city": _segment_stats(tagged, "city"),
        "venue": _segment_stats(tagged, "_venue"),
        "variable": _segment_stats(tagged, "variable"),
        "lead": _segment_stats(tagged, "_lead"),
        "edge": _segment_stats(tagged, "_edge"),
        "p_win": _segment_stats(tagged, "_p_win"),
    }


def _yes_token_for_bucket(raw: str | None, bucket_label: str) -> str | None:
    """Recover the YES CLOB token id for a bucket from the markets.raw column.

    raw holds market.model_dump(mode='json') written by record.py. We use
    model_validate (not parse_market) because raw is already the parsed model
    shape, not a Gamma API event JSON.

    Returns None when raw is NULL, unparsable, or is a PrecipMonthlyMarket (which
    does not validate as a Market -- different station type). Those bets fall out
    of n_clv as a coverage gap; they never crash.
    """
    if not raw:
        return None
    try:
        market = Market.model_validate(json.loads(raw))
    except Exception:
        # ValidationError (precip market, wrong shape), JSONDecodeError, TypeError
        return None
    for b in market.buckets:
        if b.label == bucket_label:
            return b.yes_token_id
    return None


def _clv_segment_stats(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    """Group by key, compute mean CLV per segment, return sorted by segment label."""
    groups: dict[str, dict[str, Any]] = defaultdict(lambda: {"n": 0, "clv_sum": 0.0})
    for r in rows:
        seg = r[key]
        g = groups[seg]
        g["n"] += 1
        g["clv_sum"] += r["_clv"]
    return sorted(
        [
            {"segment": seg, "n": g["n"], "mean_clv": g["clv_sum"] / g["n"]}
            for seg, g in groups.items()
        ],
        key=lambda s: s["segment"],
    )


def compute_clv(conn: Conn, client: httpx.Client) -> dict[str, Any]:
    """Closing-line value for recommended Polymarket bets.

    Population: same deduped bets as compute_pnl with venue='polymarket'.
    Advised price: r['ask'] (stored YES ask for YES bets, NO ask for NO bets).
    Closing price: last CLOB mid point strictly before the synthesized settlement
    timestamp (settlement_date at 12:00:00 UTC).

    Note: the settlement timestamp is synthesized as settlement_date at 12:00 UTC
    because daily temp markets publish endDate ~12:00 UTC (enforced by the 6<=hour<=18
    guard in polymarket/markets.py) but the persisted Market stores only the local_date.

    CLV signs: YES bet -> yes_close - ask; NO bet -> (1 - yes_close) - ask.
    Positive CLV means we bought below the closing line (edge captured).

    Caveat: the advised price is an ask; the closing price is the CLOB mid (the
    only price the prices-history endpoint returns). A symmetric half-spread haircut
    applies equally to both sides, so the sign is not biased -- same caveat as
    pnl_backtest.

    Returns:
        n_bets: total deduped Polymarket bets (must equal compute_pnl(conn, 'polymarket')['n_bets'])
        n_clv: subset with a successful closing-price fetch
        mean_clv: mean CLV over n_clv bets (None when n_clv == 0)
        by_segment: per-dimension mean CLV over n_clv bets, keyed by dim name
    """
    bets = _best_per_market_run(_filter_venue(_settled_rows(conn), "polymarket"))
    n_bets = len(bets)

    clv_rows: list[dict[str, Any]] = []
    for r in bets:
        token = _yes_token_for_bucket(r.get("raw"), r["bucket"])
        if token is None:
            continue
        settlement_date = r["settlement_date"]
        # Synthesize settlement at 12:00 UTC: daily temp endDate is guaranteed ~12:00 UTC
        # by the 6<=hour<=18 guard in polymarket/markets.py; only local_date is persisted.
        y, mo, d = (int(x) for x in settlement_date.split("-"))
        settlement_ts = int(datetime(y, mo, d, 12, 0, 0, tzinfo=UTC).timestamp())
        start_ts = settlement_ts - 7 * 24 * 3600
        try:
            points = fetch_price_history(token, start_ts, settlement_ts, client)
        except httpx.HTTPError:
            continue
        yes_close = last_before(points, settlement_ts)
        if yes_close is None:
            continue
        side = r.get("side") or "YES"
        ask = r["ask"]
        clv = yes_close - ask if side == "YES" else (1.0 - yes_close) - ask
        tagged = dict(r)
        tagged["_clv"] = clv
        tagged["_venue"] = r.get("venue") or "polymarket"
        tagged["_lead"] = _lead_bucket(r["settlement_date"], r["started_at"])
        tagged["_edge"] = _edge_bucket(r.get("edge"))
        tagged["_p_win"] = _p_win_bucket(r["p_win"])
        clv_rows.append(tagged)

    n_clv = len(clv_rows)
    mean_clv = sum(r["_clv"] for r in clv_rows) / n_clv if n_clv else None

    by_segment: dict[str, list[dict[str, Any]]] = {}
    if clv_rows:
        by_segment["city"] = _clv_segment_stats(
            [{**r, "_key": r["city"]} for r in clv_rows], "_key"
        )
        by_segment["venue"] = _clv_segment_stats(
            [{**r, "_key": r["_venue"]} for r in clv_rows], "_key"
        )
        by_segment["variable"] = _clv_segment_stats(
            [{**r, "_key": r["variable"]} for r in clv_rows], "_key"
        )
        by_segment["lead"] = _clv_segment_stats(
            [{**r, "_key": r["_lead"]} for r in clv_rows], "_key"
        )
        by_segment["edge"] = _clv_segment_stats(
            [{**r, "_key": r["_edge"]} for r in clv_rows], "_key"
        )
        by_segment["p_win"] = _clv_segment_stats(
            [{**r, "_key": r["_p_win"]} for r in clv_rows], "_key"
        )

    return {
        "n_bets": n_bets,
        "n_clv": n_clv,
        "mean_clv": mean_clv,
        "by_segment": by_segment,
    }


def compute_live_accuracy(conn: Conn) -> list[dict[str, Any]]:
    """Degrees-space accuracy of the bot's own forecasts over settled markets.

    One sample per (market, UTC day): the latest run's predicted mu against the
    settled actual, grouped per (station, variable, lead). DISTINCT collapses the
    per-bucket prediction rows, which share one dist_params, to one row per (run,
    market); _latest_run_per_market_day then keeps the latest run per (market, UTC
    day) so correlated intraday runs (#77) count once (#63, #78). This relies on
    _record_predictions writing an identical dist_params string for every bucket
    row of one (run, market); if that changes, replace DISTINCT with a subquery.
    Rows with an unknown city, unparsable dist_params, a null actual, or no usable
    mu/sigma are skipped.
    """
    rows = conn.execute(
        "SELECT DISTINCT p.run_id AS run_id, p.market_id AS market_id, "
        "p.dist_params AS dist_params, m.city AS city, m.variable AS variable, "
        "m.venue AS venue, m.settlement_date AS settlement_date, r.started_at AS started_at, "
        "o.actual_value AS actual_value "
        "FROM predictions p "
        "JOIN outcomes o ON o.market_id = p.market_id "
        "JOIN markets m ON m.id = p.market_id "
        "JOIN runs r ON r.id = p.run_id "
        "WHERE p.dist_params IS NOT NULL AND o.actual_value IS NOT NULL"
    ).fetchall()
    groups: dict[tuple[str, str, str, int], list[CalibrationPair]] = defaultdict(list)
    for r in _latest_run_per_market_day([dict(row) for row in rows]):
        # Attribute to the market's own station: the Kalshi registry for Kalshi
        # markets (NYC = Central Park, not LaGuardia), else the Polymarket one.
        registry = KALSHI_STATIONS if (r.get("venue") == "kalshi") else STATIONS
        station = registry.get(r["city"])
        if station is None:
            continue
        try:
            params = json.loads(r["dist_params"])
        except json.JSONDecodeError:
            continue  # unparsable dist_params: skip, never fail the snapshot
        mu, sigma = params.get("mu"), params.get("sigma")
        if mu is None or sigma is None or sigma <= 0:
            continue
        lead = (
            date.fromisoformat(r["settlement_date"]) - date.fromisoformat(r["started_at"][:10])
        ).days
        if lead < 0:
            continue  # a run after settlement is a catch-up, not a forecast: not accuracy
        key = (station.icao, r["city"], r["variable"], lead)
        groups[key].append(
            CalibrationPair(mu=mu, sigma=sigma, ensemble_var=sigma**2, actual=r["actual_value"])
        )
    return [
        {
            "station": station,
            "city": city,
            "variable": variable,
            "lead_time": lead,
            "accuracy": compute_accuracy(pairs),
        }
        for (station, city, variable, lead), pairs in sorted(groups.items())
    ]


def compute_live_calibration(conn: Conn) -> list[dict[str, Any]]:
    """Probability-calibration metrics pooled per (variable, lead) across all cities.

    Three metrics, all from the stored mu/sigma/p_win against settled actuals:
    - CRPS: one sample per (market, UTC day) from dist_params.
    - Coverage at 50/80/90: same (market, UTC day) population.
    - Reliability: (p_win, won) per YES bucket-prediction row.

    Same deduplication as compute_live_accuracy: _latest_run_per_market_day
    collapses intraday runs to the latest per (market, UTC day).

    Pooled across cities: keyed by (variable, lead) only. No per-city split.
    No price or recommended filter: calibration is a property of the forecast,
    not of whether a bet was placed.
    """
    rows = conn.execute(
        "SELECT DISTINCT p.run_id AS run_id, p.market_id AS market_id, "
        "p.dist_params AS dist_params, m.variable AS variable, "
        "m.settlement_date AS settlement_date, r.started_at AS started_at, "
        "o.actual_value AS actual_value "
        "FROM predictions p "
        "JOIN outcomes o ON o.market_id = p.market_id "
        "JOIN markets m ON m.id = p.market_id "
        "JOIN runs r ON r.id = p.run_id "
        "WHERE p.dist_params IS NOT NULL AND o.actual_value IS NOT NULL "
        "AND m.variable != 'PRCP'"
    ).fetchall()

    # (variable, lead) -> list of (mu, sigma, actual) for CRPS + coverage
    # PRCP is excluded: its mu/sigma describe a gamma (mean/sqrt-var), not a Gaussian,
    # so crps_gaussian and norm.cdf coverage are methodologically wrong for it.
    dist_groups: dict[tuple[str, int], list[tuple[float, float, float]]] = defaultdict(list)
    for r in _latest_run_per_market_day([dict(row) for row in rows]):
        lead = (
            date.fromisoformat(r["settlement_date"]) - date.fromisoformat(r["started_at"][:10])
        ).days
        if lead < 0:
            continue
        try:
            params = json.loads(r["dist_params"])
        except json.JSONDecodeError:
            continue
        mu, sigma = params.get("mu"), params.get("sigma")
        if mu is None or sigma is None or sigma <= 0:
            continue
        dist_groups[(r["variable"], lead)].append((mu, sigma, r["actual_value"]))

    # Reliability: YES bucket rows (all buckets, not just best-edge). No dedup here:
    # each (run, market, bucket) is one (p_win, won) data point for the reliability diagram.
    # We do apply _latest_run_per_market_day per market to avoid counting intraday
    # re-runs twice -- collect YES rows first, then deduplicate at (market, UTC day).
    yes_rows_raw = conn.execute(
        "SELECT p.run_id AS run_id, p.market_id AS market_id, "
        "p.p_win AS p_win, p.bucket AS bucket, "
        "m.variable AS variable, m.settlement_date AS settlement_date, "
        "m.outcome_spec AS outcome_spec, m.variable AS market_variable, "
        "r.started_at AS started_at, o.actual_value AS actual_value "
        "FROM predictions p "
        "JOIN outcomes o ON o.market_id = p.market_id "
        "JOIN markets m ON m.id = p.market_id "
        "JOIN runs r ON r.id = p.run_id "
        "WHERE p.bucket IS NOT NULL AND o.actual_value IS NOT NULL "
        "AND COALESCE(p.side, 'YES') = 'YES' "
        "AND m.variable != 'PRCP'"
    ).fetchall()

    # Apply _latest_run_per_market_day to YES rows for deduplication.
    yes_deduped = _latest_run_per_market_day([dict(row) for row in yes_rows_raw])

    # (variable, lead) -> list of (p_win, won)
    rel_groups: dict[tuple[str, int], list[tuple[float, bool]]] = defaultdict(list)
    for r in yes_deduped:
        try:
            lead = (
                date.fromisoformat(r["settlement_date"]) - date.fromisoformat(r["started_at"][:10])
            ).days
        except ValueError:
            continue  # unparsable date (e.g. test sentinel "t"): skip
        if lead < 0:
            continue
        try:
            won = _won(r["variable"], r["bucket"], r["actual_value"], r.get("outcome_spec"))
        except (ValueError, KeyError):
            continue  # malformed bucket label: skip, never fail the snapshot
        rel_groups[(r["variable"], lead)].append((r["p_win"], won))

    # Combine into result rows; only emit groups that have dist samples.
    out: list[dict[str, Any]] = []
    for (variable, lead), samples in sorted(dist_groups.items()):
        crps_vals = [crps_gaussian(mu, sigma, actual) for mu, sigma, actual in samples]
        coverages: dict[float, list[bool]] = {q: [] for q in COVERAGE_LEVELS}
        for mu, sigma, actual in samples:
            cdf_actual = float(norm.cdf(actual, loc=mu, scale=sigma))
            for q in COVERAGE_LEVELS:
                coverages[q].append(abs(cdf_actual - 0.5) <= q / 2)
        n = len(samples)
        rel_pairs = rel_groups.get((variable, lead), [])
        bins = reliability_bins(rel_pairs) if rel_pairs else []
        out.append(
            {
                "variable": variable,
                "lead_time": lead,
                "n_samples": n,
                "crps": sum(crps_vals) / n,
                "coverage_50": sum(coverages[0.50]) / n,
                "coverage_80": sum(coverages[0.80]) / n,
                "coverage_90": sum(coverages[0.90]) / n,
                "reliability_bins": [b.model_dump(mode="json") for b in bins],
            }
        )
    return out


def write_snapshot(conn: Conn, on_date: str, created_at: str) -> dict[str, Any]:
    """Compute the current P&L/calibration and upsert a snapshot row for on_date."""
    pnl = compute_pnl(conn)
    cal = compute_calibration(conn)
    # save_accuracy commits internally after each row; insert the snapshot only
    # after the loop so a mid-loop failure cannot leave a committed snapshot row
    # without its corresponding accuracy rows.
    for row in compute_live_accuracy(conn):
        save_accuracy(
            conn,
            station=row["station"],
            city=row["city"],
            variable=row["variable"],
            lead_time=row["lead_time"],
            kind="live",
            accuracy=row["accuracy"],
            updated_at=created_at,
        )
    for row in compute_live_calibration(conn):
        save_accuracy(
            conn,
            station="ALL",
            city=None,
            variable=row["variable"],
            lead_time=row["lead_time"],
            kind="calibration",
            accuracy=Accuracy(
                n=row["n_samples"],
                mae_f=0.0,  # not applicable for calibration rows
                bias_f=0.0,  # not applicable for calibration rows
                crps=row["crps"],
                coverage_50=row["coverage_50"],
                coverage_80=row["coverage_80"],
                coverage_90=row["coverage_90"],
                reliability_bins=row["reliability_bins"],
            ),
            updated_at=created_at,
        )
    conn.execute(
        "INSERT INTO tracking_snapshot "
        "(snapshot_date, n_bets, wins, losses, total_pnl, roi, brier, hit_rate, "
        "n_scored, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(snapshot_date) DO UPDATE SET "
        "n_bets = excluded.n_bets, wins = excluded.wins, losses = excluded.losses, "
        "total_pnl = excluded.total_pnl, roi = excluded.roi, brier = excluded.brier, "
        "hit_rate = excluded.hit_rate, n_scored = excluded.n_scored, "
        "created_at = excluded.created_at",
        (
            on_date,
            pnl["n_bets"],
            pnl["wins"],
            pnl["losses"],
            pnl["total_pnl"],
            pnl["roi"],
            cal["brier"],
            cal["hit_rate"],
            cal["n"],
            created_at,
        ),
    )
    conn.commit()
    return {"pnl": pnl, "calibration": cal}
