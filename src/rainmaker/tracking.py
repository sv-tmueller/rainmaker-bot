"""Score the bot against settled outcomes: hypothetical P&L and calibration.

Computed on read from predictions + prices + outcomes. One one-unit bet per
(market, UTC day): the best-edge recommended side/bucket from that day's latest
run. Buckets on one market describe the same temperature, so correlated
same-market bets collapse to one; the intraday runs (#77) that re-price a market
many times a day are correlated too, so they collapse to the latest run per UTC
day (#63, #78). Tracking only covers rows with a bucket recorded.
"""

import json
import math
from collections import defaultdict
from datetime import UTC, date, datetime
from typing import Any

import httpx
import numpy as np
from pydantic import ValidationError
from scipy.stats import norm

from rainmaker.backtest import COVERAGE_LEVELS, crps_gaussian, reliability_bins
from rainmaker.config import KALSHI_STATIONS, STATIONS
from rainmaker.domain import BucketKind, Market, parse_bucket_label, parse_precip_bracket_label
from rainmaker.polymarket.prices import fetch_price_history, last_before
from rainmaker.probability.calibration import (
    Accuracy,
    CalibrationPair,
    compute_accuracy,
    numeric_crps,
    std_cdf_for,
)
from rainmaker.probability.outcomes import settles
from rainmaker.probability.precip_outcomes import precip_settles
from rainmaker.store.db import Conn
from rainmaker.store.record import save_accuracy

# Cap on how many Student-t rows share one numeric_crps call: bounds the (N, grid)
# matrix numeric_crps builds per call, on top of numeric_crps's own internal cap.
_CRPS_BATCH_SIZE = 1000


def _finite_number(x: Any) -> float | None:
    """Coerce x to a finite float, or None if it isn't one.

    Rejects bool explicitly: JSON true/false decode to Python bool, which is an
    int subclass, so isinstance(x, (int, float)) alone would silently accept it
    as 1.0/0.0. Rejects NaN/Inf too, since comparisons against them (e.g. the
    sigma<=0 guard) are always False and let a poisoned value slip through.
    """
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    value = float(x)
    return value if math.isfinite(value) else None


def _parse_df(params: dict[str, Any]) -> tuple[float | None, bool]:
    """Read dist_params["df"]: (df, ok). df=None means Gaussian (absent key or
    explicit null); ok=False means the row must be skipped (present but
    non-numeric, non-finite, bool, or <= 0), mirroring the existing sigma<=0
    guard -- a row never silently scores as Gaussian just because its df was
    unusable.
    """
    df_raw = params.get("df")
    if df_raw is None:
        return None, True
    df = _finite_number(df_raw)
    if df is None or df <= 0:
        return None, False
    return df, True


def _cdf_at(mu: float, sigma: float, df: float | None, actual: float) -> float:
    """Predictive CDF at `actual`, dispatched by family (df)."""
    if df is None:
        return float(norm.cdf(actual, loc=mu, scale=sigma))
    z = np.array([(actual - mu) / sigma])
    return float(std_cdf_for(df)(z)[0])


def _crps_family_aware(
    samples: list[tuple[float, float, float | None, float]],
) -> list[float]:
    """CRPS per (mu, sigma, df, actual) sample, dispatched by each row's own family.

    Gaussian rows (df=None) use the closed-form crps_gaussian. Student-t rows are
    grouped by their exact df value and scored in one vectorized numeric_crps call
    per group (chunked at _CRPS_BATCH_SIZE), instead of one Python-level call per
    row: with ~15k historical rows this keeps the diagnostic well inside the
    daily-diagnostics timeout.
    """
    out = [0.0] * len(samples)
    t_idx_by_df: dict[float, list[int]] = defaultdict(list)
    for i, (mu, sigma, df, actual) in enumerate(samples):
        if df is None:
            out[i] = crps_gaussian(mu, sigma, actual)
        else:
            t_idx_by_df[df].append(i)

    for df, idxs in t_idx_by_df.items():
        std_cdf = std_cdf_for(df)
        for start in range(0, len(idxs), _CRPS_BATCH_SIZE):
            chunk = idxs[start : start + _CRPS_BATCH_SIZE]
            mu_arr = np.array([samples[i][0] for i in chunk])
            sigma_arr = np.array([samples[i][1] for i in chunk])
            actual_arr = np.array([samples[i][3] for i in chunk])
            scores = numeric_crps(std_cdf, mu_arr, sigma_arr, actual_arr)
            for j, i in enumerate(chunk):
                out[i] = float(scores[j])
    return out


def _won(
    variable: str,
    bucket_label: str,
    actual_value: float,
    outcome_spec: str | None = None,
) -> bool | None:
    """Grade a settled bucket, or return None when it cannot be graded.

    Try the structured spec stored at record time first. This handles Kalshi
    labels ("74° to 75°", '2" to 3"') that the Polymarket-style parsers cannot
    read. Fall back to the label parsers for legacy rows (NULL spec) or rows
    where the label is absent from the spec. None (rather than raising) is the
    single seam every caller checks: a label that matches no spec entry and that
    the fallback parser also cannot read (#333, e.g. a bare Kalshi unit string
    like 'inches') is ungradable, not a crash.
    """
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
    try:
        if variable == "PRCP":
            return precip_settles(*parse_precip_bracket_label(bucket_label), actual_value)
        return settles(*parse_bucket_label(bucket_label), actual_value)
    except (ValueError, KeyError):
        return None  # no spec match and the label parser can't read it either


def _bet_won(row: dict[str, Any]) -> bool | None:
    """A YES bet wins when the bucket settles; a NO bet wins when it does not.

    None (ungradable, see _won) propagates unchanged: a NO bet on an ungradable
    bucket is still ungradable, never silently flipped to a win.
    """
    settled = _won(row["variable"], row["bucket"], row["actual_value"], row.get("outcome_spec"))
    if settled is None:
        return None
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


def settled_rows(conn: Conn) -> list[dict[str, Any]]:
    # Match the price to the prediction's side; legacy rows with a null side are YES.
    # city and settlement_date are carried for compute_attribution; compute_pnl and
    # compute_calibration ignore these extra keys. markets.raw is NOT selected here:
    # it duplicates the full market JSON onto every row (#277: egress). compute_clv
    # is the only caller that needs it, and it fetches raw separately, bounded to
    # the deduped bet market ids, via _raw_by_market_id.
    rows = conn.execute(
        "SELECT p.market_id AS market_id, p.run_id AS run_id, p.bucket AS bucket, "
        "p.side AS side, p.p_win AS p_win, p.edge AS edge, "
        "p.recommended AS recommended, m.variable AS variable, m.venue AS venue, "
        "m.outcome_spec AS outcome_spec, m.city AS city, "
        "m.settlement_date AS settlement_date, r.started_at AS started_at, "
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


_RAW_FETCH_CHUNK_SIZE = 500  # stay under SQLite's default bound-variable limit


def _raw_by_market_id(conn: Conn, market_ids: list[str]) -> dict[str, str | None]:
    """Fetch markets.raw for exactly the given market ids, chunked and bounded.

    Used only by compute_clv, so a full-history call never carries this payload
    (#277). An empty id list issues no query.
    """
    out: dict[str, str | None] = {}
    unique_ids = list(dict.fromkeys(market_ids))  # de-dupe, preserve order
    for start in range(0, len(unique_ids), _RAW_FETCH_CHUNK_SIZE):
        chunk = unique_ids[start : start + _RAW_FETCH_CHUNK_SIZE]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT id, raw FROM markets WHERE id IN ({placeholders})", chunk
        ).fetchall()
        for r in rows:
            out[r["id"]] = r["raw"]
    return out


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


def compute_pnl(
    conn: Conn, venue: str | None = None, *, rows: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Hypothetical P&L over recommended bets at a flat one-unit stake.

    With venue set ("polymarket" / "kalshi"), restrict to that venue's markets.
    Pass rows= a pre-fetched settled_rows(conn) result to skip the query (#277:
    lets one process share one full-history read across several calls); conn is
    then only used if rows is None.
    """
    total_pnl = 0.0
    total_staked = 0.0
    wins = 0
    n = 0
    skipped = 0
    all_rows = rows if rows is not None else settled_rows(conn)
    for r in _best_per_market_run(_filter_venue(all_rows, venue)):
        won = _bet_won(r)
        if won is None:
            skipped += 1
            continue
        n += 1
        ask = r["ask"]
        total_staked += ask
        if won:
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
        "skipped": skipped,
    }


def _cell_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Brier over YES rows plus recommended hit rate, for an already-filtered row set.

    Shared by compute_calibration (pooled) and compute_calibration_by_cell (per
    variable/lead), so the two can never silently drift apart on the arithmetic.
    An ungradable row (_won/_bet_won return None, see their docstrings) is
    excluded from both n and hit_rate's denominator and counted into "skipped".
    One physical row can feed both the brier population (yes_rows) and the hit-rate
    population (bets); id(r) dedupes so it is only counted once.
    """
    if not rows:
        return {"n": 0, "brier": None, "hit_rate": None, "skipped": 0}
    # Brier measures forecast calibration over the YES bucket-predictions; each NO
    # row's contribution is identical to its YES twin, so including it would only
    # double n. Hit rate is over the one best-edge bet per (market, run), either side.
    yes_rows = [r for r in rows if (r.get("side") or "YES") == "YES"]
    skipped_ids: set[int] = set()
    brier_terms = []
    for r in yes_rows:
        won = _won(r["variable"], r["bucket"], r["actual_value"], r.get("outcome_spec"))
        if won is None:
            skipped_ids.add(id(r))
            continue
        brier_terms.append((r["p_win"] - (1.0 if won else 0.0)) ** 2)
    brier = sum(brier_terms) / len(brier_terms) if brier_terms else None
    bets = _best_per_market_run(rows)
    hit_results: list[bool] = []
    for r in bets:
        won = _bet_won(r)
        if won is None:
            skipped_ids.add(id(r))
            continue
        hit_results.append(won)
    hit_rate = sum(1 for w in hit_results if w) / len(hit_results) if hit_results else None
    return {
        "n": len(brier_terms),
        "brier": brier,
        "hit_rate": hit_rate,
        "skipped": len(skipped_ids),
    }


def compute_calibration(
    conn: Conn, venue: str | None = None, *, rows: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Brier over the settled YES bucket-predictions, plus recommended hit rate.

    With venue set, restrict to that venue's markets. Pass rows= a pre-fetched
    settled_rows(conn) result to skip the query (#277); conn is then only used
    if rows is None.
    """
    all_rows = rows if rows is not None else settled_rows(conn)
    return _cell_stats(_filter_venue(all_rows, venue))


def compute_calibration_by_cell(
    rows: list[dict[str, Any]], since: str | None = None
) -> list[dict[str, Any]]:
    """Brier and hit rate per (variable, lead), computed from an already-fetched rows list.

    Takes rows= only, never a conn: the caller passes the same settled_rows()
    result it already shares with compute_pnl/compute_calibration (#277), so this
    adds no additional database query.

    since (an ISO "YYYY-MM-DD" string) restricts to runs.started_at on or after
    that date. It is applied here in Python, against the rows settled_rows() has
    already deduped to the latest run per (market, UTC day). That is safe because
    _latest_run_per_market_day picks the per-group argmax of (started_at, run_id)
    and since is a monotone on-or-after threshold: filtering the argmax-selected
    rows gives the same surviving winners as filtering before the dedup would
    have (see the #323 sub-plan for the full argument). started_at and since are
    both "YYYY-MM-DD[...]" text, so Python's >= matches since_clause's SQL >=.

    Lead is the raw integer (settlement_date - started_at date).days, the same
    formula compute_tail_calibration uses (not the bucketed _lead_bucket), so a
    cell here is directly comparable to a tail-check lead. Rows with lead < 0
    (a run after settlement, a catch-up rather than a forecast) are dropped.
    """
    if since is not None:
        rows = [r for r in rows if r["started_at"] >= since]
    groups: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        try:
            lead = (
                date.fromisoformat(r["settlement_date"]) - date.fromisoformat(r["started_at"][:10])
            ).days
        except ValueError:
            continue  # unparsable date (e.g. test sentinel "t"): skip
        if lead < 0:
            continue
        groups[(r["variable"], lead)].append(r)
    return [
        {"variable": variable, "lead_time": lead, **_cell_stats(cell_rows)}
        for (variable, lead), cell_rows in sorted(groups.items())
    ]


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
    """Group rows by key, compute per-segment stats, return sorted by segment label.

    An ungradable row (_bet_won returns None) is skipped before it touches
    n/wins/staked and counted into the segment's "skipped" instead.
    """
    groups: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"n": 0, "wins": 0, "losses": 0, "pnl": 0.0, "staked": 0.0, "skipped": 0}
    )
    for r in rows:
        seg = r[key]
        g = groups[seg]
        won = _bet_won(r)
        if won is None:
            g["skipped"] += 1
            continue
        ask = r["ask"]
        g["n"] += 1
        g["staked"] += ask
        if won:
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
                "skipped": g["skipped"],
            }
        )
    return out


def compute_attribution(
    conn: Conn, since: str | None = None, *, rows: list[dict[str, Any]] | None = None
) -> dict[str, list[dict[str, Any]]]:
    """Per-segment P&L attribution across six dimensions.

    Built from a single deduplicated bet list (same population as compute_pnl).
    Each dimension is an exhaustive partition, so per-dimension totals reconcile
    with compute_pnl's headline n/wins/losses/pnl/roi.

    Pass rows= a pre-fetched settled_rows(conn) result to skip the query (#277);
    conn is then only used if rows is None.

    since (an ISO "YYYY-MM-DD" string) restricts to runs.started_at on or after
    that date, applied to the rows settled_rows() already deduped to the latest
    run per (market, UTC day), the same mechanism and equivalence argument as
    compute_calibration_by_cell's since (see #323): since is a monotone
    on-or-after threshold, so filtering the argmax-selected rows here gives the
    same surviving winners as filtering before the dedup would have.
    """
    all_rows = rows if rows is not None else settled_rows(conn)
    if since is not None:
        all_rows = [r for r in all_rows if r["started_at"] >= since]
    bets = _best_per_market_run(all_rows)
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
    except (ValidationError, json.JSONDecodeError, TypeError):
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


def compute_clv(conn: Conn, client: httpx.Client, lead_hours: int = 24) -> dict[str, Any]:
    """Closing-line value for recommended Polymarket bets at a fixed pre-settlement lead.

    Population: same deduped bets as compute_pnl with venue='polymarket'.
    Advised price: r['ask'] (stored YES ask for YES bets, NO ask for NO bets).

    The "closing price" is the last CLOB mid point strictly before reference_ts,
    where reference_ts = settlement_ts - lead_hours * 3600. The default lead of
    24h measures how our advised price compares to the day-ahead market.

    Settlement timestamp: synthesized as settlement_date at 12:00:00 UTC. This is
    faithful because Polymarket daily temperature markets publish endDate ~12:00 UTC,
    enforced by the 6<=hour<=18 guard in polymarket/markets.py. At a 24h reference
    that +-6h window shifts the actual read to 18-30h before settlement, firmly in
    the flat day-ahead region of the CLOB time series where prices are stable.
    (Persisting endDate per market is correctly deferred; see #219.)

    Fetch window: [reference_ts - 6 days, reference_ts). The upper bound is
    reference_ts rather than settlement_ts; this prevents accidentally including
    convergence-phase prices that land between reference_ts and settlement.

    CLV signs: YES bet -> yes_close - ask; NO bet -> (1 - yes_close) - ask.
    Positive CLV means we bought below the closing line (edge captured).

    n_coincident: count of bets where abs(CLV) < 1e-9 (the yes_close == advised on
    the YES scale). A high count signals that our advised price was captured at the
    same moment the reference price was recorded (e.g. lead-1 bet recorded ~24h out).
    The epsilon (1e-9) is chosen to be smaller than any meaningful price difference.

    Caveat: the advised price is an ask; the closing price is the CLOB mid (the
    only price the prices-history endpoint returns). A symmetric half-spread haircut
    applies equally to both sides, so the sign is not biased -- same caveat as
    pnl_backtest.

    Returns:
        n_bets: total deduped Polymarket bets (must equal compute_pnl(conn, 'polymarket')['n_bets'])
        n_clv: subset with a successful closing-price fetch. A bet drops out (never
            crashes the command) on a transport/status error, an empty series, or a
            malformed 200 body (bad JSON, a missing "history" key, or an unparseable
            point), any of which fetch_price_history or its json parsing can raise.
        n_coincident: bets where abs(CLV) < 1e-9 (advised == close on YES scale)
        mean_clv: mean CLV over n_clv bets (None when n_clv == 0)
        by_segment: per-dimension mean CLV over n_clv bets, keyed by dim name
    """
    _EPS = 1e-9  # threshold for "advised == close" (see n_coincident in docstring)

    bets = _best_per_market_run(_filter_venue(settled_rows(conn), "polymarket"))
    n_bets = len(bets)
    raw_by_market = _raw_by_market_id(conn, [r["market_id"] for r in bets])

    clv_rows: list[dict[str, Any]] = []
    for r in bets:
        token = _yes_token_for_bucket(raw_by_market.get(r["market_id"]), r["bucket"])
        if token is None:
            continue
        settlement_date = r["settlement_date"]
        # Synthesize settlement at 12:00 UTC: daily temp endDate is guaranteed ~12:00 UTC
        # by the 6<=hour<=18 guard in polymarket/markets.py; only local_date is persisted.
        y, mo, d = (int(x) for x in settlement_date.split("-"))
        settlement_ts = int(datetime(y, mo, d, 12, 0, 0, tzinfo=UTC).timestamp())
        reference_ts = settlement_ts - lead_hours * 3600
        # Anchor window start to reference_ts to exclude convergence-phase prices.
        start_ts = reference_ts - 6 * 24 * 3600
        try:
            points = fetch_price_history(token, start_ts, reference_ts, client)
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            continue
        yes_close = last_before(points, reference_ts)
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
    n_coincident = sum(1 for r in clv_rows if abs(r["_clv"]) < _EPS)
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
        "n_coincident": n_coincident,
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
        mu, sigma = _finite_number(params.get("mu")), _finite_number(params.get("sigma"))
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

    # (variable, lead) -> list of (mu, sigma, df, actual) for CRPS + coverage.
    # PRCP is excluded: its mu/sigma describe a gamma (mean/sqrt-var), not a Gaussian
    # or Student-t, so this family-aware scoring is methodologically wrong for it.
    dist_groups: dict[tuple[str, int], list[tuple[float, float, float | None, float]]] = (
        defaultdict(list)
    )
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
        mu, sigma = _finite_number(params.get("mu")), _finite_number(params.get("sigma"))
        if mu is None or sigma is None or sigma <= 0:
            continue
        df, df_ok = _parse_df(params)
        if not df_ok:
            continue
        dist_groups[(r["variable"], lead)].append((mu, sigma, df, r["actual_value"]))

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
        won = _won(r["variable"], r["bucket"], r["actual_value"], r.get("outcome_spec"))
        if won is None:
            continue  # ungradable bucket label: skip, never fail the snapshot
        rel_groups[(r["variable"], lead)].append((r["p_win"], won))

    # Combine into result rows; only emit groups that have dist samples.
    out: list[dict[str, Any]] = []
    for (variable, lead), samples in sorted(dist_groups.items()):
        crps_vals = _crps_family_aware(samples)
        coverages: dict[float, list[bool]] = {q: [] for q in COVERAGE_LEVELS}
        for mu, sigma, df, actual in samples:
            cdf_actual = _cdf_at(mu, sigma, df, actual)
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


MIN_TAIL_N = 20  # minimum cell population before a claimed-vs-realized cell gets a verdict


def _tail_bin(value: float) -> str:
    """Map a claimed probability to a tail bin. Sub-.75 values share a body-context bin."""
    if value < 0.75:
        return "<0.75"
    if value < 0.85:
        return "[0.75,0.85)"
    if value < 0.90:
        return "[0.85,0.90)"
    if value < 0.95:
        return "[0.90,0.95)"
    return "[0.95,1.0]"


def _pit_tail_ratios(pits: list[float]) -> dict[str, float | int]:
    """Tail-occurrence ratios P(PIT > 1-q)/q and P(PIT < q)/q at q = 0.10 and 0.05.

    Takes precomputed PITs (each already dispatched to its row's own family by
    the caller via _cdf_at) rather than (mu, sigma, actual) triples, so this stays
    bucket-geometry-free and family-agnostic: it isolates distribution-tail
    miscalibration from the bucket-width artifact the claimed-vs-realized table
    can show (a narrow bucket inflates p_win regardless of tail thickness).

    Alongside each ratio, also returns the observed hit count and the expected
    count (n * q) it was computed from, keyed "<cell>_obs" / "<cell>_exp": a
    printed ratio alone forces the reader to re-derive the counts from n and q.
    """
    n = len(pits)

    def tail_count(q: float, upper: bool) -> int:
        if upper:
            return sum(1 for p in pits if p > 1 - q)
        return sum(1 for p in pits if p < q)

    out: dict[str, float | int] = {"n": n}
    for label, q, upper in (
        ("upper_10", 0.10, True),
        ("lower_10", 0.10, False),
        ("upper_05", 0.05, True),
        ("lower_05", 0.05, False),
    ):
        count = tail_count(q, upper)
        out[label] = (count / n) / q
        out[f"{label}_obs"] = count
        out[f"{label}_exp"] = n * q
    return out


def _latest_run_per_market_day_hour(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Like _latest_run_per_market_day, but keyed by (market, UTC day, hour).

    Under --by-hour, compute_tail_calibration groups by the run's UTC hour; deduping
    per (market, day) alone would collapse every hour to the day's single latest run
    and erase the hour dimension, so the key gains started_at's hour.
    """
    latest: dict[tuple[str, str, str], tuple[str, str]] = {}
    for r in rows:
        started = r["started_at"]
        key = (r["market_id"], started[:10], started[11:13])
        marker = (started, r["run_id"])
        if key not in latest or marker > latest[key]:
            latest[key] = marker
    keep = {(market_id, run_id) for (market_id, _, _), (_, run_id) in latest.items()}
    return [r for r in rows if (r["market_id"], r["run_id"]) in keep]


def compute_tail_calibration(
    conn: Conn, by_hour: bool = False, since: str | None = None
) -> dict[str, list[dict[str, Any]]]:
    """Claimed-vs-realized tail calibration plus PIT tail ratios, per (variable, lead).

    Read-only diagnostic: no refit, no gate change, no persistence. Temperature
    only (m.variable != 'PRCP'); same population and dedup as compute_live_calibration
    (see its docstring) unless by_hour is set, which dedups per (market, UTC day,
    hour) instead of per (market, UTC day) so the hour split survives the
    intraday-rerun collapse, and adds hour to every group key. When since is set
    (an ISO date string), both populations are restricted to predictions whose
    run started on or after that date (runs.started_at, not settlement time),
    for regime-clean readouts after a calibration or code change.

    Primary ("primary"): a YES bucket row is a YES-tail claim at its own p_win
    when p_win >= 0.5, else a NO-tail claim at 1 - p_win (event: the bucket did
    not settle) -- both tails are reported separately via the side column. Both
    sides bin into <0.75 (body context), [0.75,0.85), [0.85,0.90), [0.90,0.95),
    [0.95,1.0]. A cell gets an OVER/UNDER verdict only when n >= MIN_TAIL_N and
    the claimed mean falls outside the Wilson 95% CI of the realized frequency;
    thinner cells report thin=True and no verdict.

    Secondary ("pit"): P(PIT > 1-q)/q and P(PIT < q)/q at q = 0.10 and 0.05 from
    the stored (mu, sigma, actual) triples, one row per (variable, lead[, hour]).
    """
    dedup = _latest_run_per_market_day_hour if by_hour else _latest_run_per_market_day

    # started_at is TEXT ISO-8601 on both backends, so lexicographic comparison
    # of the "YYYY-MM-DD" cutoff against a full timestamp gives on-or-after
    # semantics ("2026-07-06" < "2026-07-06T00:...") without a cast.
    since_clause = " AND r.started_at >= ?" if since is not None else ""
    since_params = (since,) if since is not None else ()

    dist_rows = conn.execute(
        "SELECT DISTINCT p.run_id AS run_id, p.market_id AS market_id, "
        "p.dist_params AS dist_params, m.variable AS variable, "
        "m.settlement_date AS settlement_date, r.started_at AS started_at, "
        "o.actual_value AS actual_value "
        "FROM predictions p "
        "JOIN outcomes o ON o.market_id = p.market_id "
        "JOIN markets m ON m.id = p.market_id "
        "JOIN runs r ON r.id = p.run_id "
        "WHERE p.dist_params IS NOT NULL AND o.actual_value IS NOT NULL "
        "AND m.variable != 'PRCP'" + since_clause,
        since_params,
    ).fetchall()

    # Each PIT is computed at collection time with the row's own family (df):
    # pit_groups holds precomputed floats, not (mu, sigma, actual) triples, so
    # _pit_tail_ratios stays family-agnostic (see its docstring).
    pit_groups: dict[tuple[str, int, int | None], list[float]] = defaultdict(list)
    for r in dedup([dict(row) for row in dist_rows]):
        try:
            lead = (
                date.fromisoformat(r["settlement_date"]) - date.fromisoformat(r["started_at"][:10])
            ).days
        except ValueError:
            continue  # unparsable date (e.g. test sentinel "t"): skip
        if lead < 0:
            continue
        try:
            params = json.loads(r["dist_params"])
        except json.JSONDecodeError:
            continue
        mu, sigma = _finite_number(params.get("mu")), _finite_number(params.get("sigma"))
        if mu is None or sigma is None or sigma <= 0:
            continue
        df, df_ok = _parse_df(params)
        if not df_ok:
            continue
        hour = int(r["started_at"][11:13]) if by_hour else None
        pit_groups[(r["variable"], lead, hour)].append(_cdf_at(mu, sigma, df, r["actual_value"]))

    yes_rows_raw = conn.execute(
        "SELECT p.run_id AS run_id, p.market_id AS market_id, "
        "p.p_win AS p_win, p.bucket AS bucket, "
        "m.variable AS variable, m.settlement_date AS settlement_date, "
        "m.outcome_spec AS outcome_spec, "
        "r.started_at AS started_at, o.actual_value AS actual_value "
        "FROM predictions p "
        "JOIN outcomes o ON o.market_id = p.market_id "
        "JOIN markets m ON m.id = p.market_id "
        "JOIN runs r ON r.id = p.run_id "
        "WHERE p.bucket IS NOT NULL AND o.actual_value IS NOT NULL "
        "AND COALESCE(p.side, 'YES') = 'YES' "
        "AND m.variable != 'PRCP'" + since_clause,
        since_params,
    ).fetchall()

    tail_groups: dict[tuple[str, int, int | None, str, str], list[tuple[float, bool]]] = (
        defaultdict(list)
    )
    for r in dedup([dict(row) for row in yes_rows_raw]):
        try:
            lead = (
                date.fromisoformat(r["settlement_date"]) - date.fromisoformat(r["started_at"][:10])
            ).days
        except ValueError:
            continue
        if lead < 0:
            continue
        won = _won(r["variable"], r["bucket"], r["actual_value"], r.get("outcome_spec"))
        if won is None:
            continue  # ungradable bucket label: skip, never fail the tail check
        p_win = r["p_win"]
        if p_win >= 0.5:
            side, claim, event_won = "YES", p_win, won
        else:
            side, claim, event_won = "NO", 1 - p_win, not won
        hour = int(r["started_at"][11:13]) if by_hour else None
        tail_groups[(r["variable"], lead, hour, side, _tail_bin(claim))].append((claim, event_won))

    primary: list[dict[str, Any]] = []
    for (variable, lead, hour, side, bin_label), items in sorted(tail_groups.items()):
        n = len(items)
        wins = sum(1 for _, event_won in items if event_won)
        claimed_mean = sum(c for c, _ in items) / n
        realized_freq = wins / n
        lo, hi = _wilson_interval(wins, n)
        thin = n < MIN_TAIL_N
        verdict = None
        if not thin:
            if claimed_mean > hi:
                verdict = "OVER"
            elif claimed_mean < lo:
                verdict = "UNDER"
        primary.append(
            {
                "variable": variable,
                "lead_time": lead,
                "hour": hour,
                "side": side,
                "bin": bin_label,
                "n": n,
                "wins": wins,
                "claimed_mean": claimed_mean,
                "realized_freq": realized_freq,
                "wilson_lo": lo,
                "wilson_hi": hi,
                "thin": thin,
                "verdict": verdict,
            }
        )

    pit: list[dict[str, Any]] = []
    for (variable, lead, hour), pits in sorted(pit_groups.items()):
        pit.append(
            {"variable": variable, "lead_time": lead, "hour": hour, **_pit_tail_ratios(pits)}
        )

    return {"primary": primary, "pit": pit}


_SNAPSHOT_VENUES: tuple[tuple[str, str | None], ...] = (
    ("all", None),
    ("polymarket", "polymarket"),
    ("kalshi", "kalshi"),
)


def _upsert_snapshot_row(
    conn: Conn,
    on_date: str,
    venue_label: str,
    pnl: dict[str, Any],
    cal: dict[str, Any],
    created_at: str,
) -> None:
    conn.execute(
        "INSERT INTO tracking_snapshot "
        "(snapshot_date, venue, n_bets, wins, losses, total_pnl, roi, brier, hit_rate, "
        "n_scored, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(snapshot_date, venue) DO UPDATE SET "
        "n_bets = excluded.n_bets, wins = excluded.wins, losses = excluded.losses, "
        "total_pnl = excluded.total_pnl, roi = excluded.roi, brier = excluded.brier, "
        "hit_rate = excluded.hit_rate, n_scored = excluded.n_scored, "
        "created_at = excluded.created_at",
        (
            on_date,
            venue_label,
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


def write_snapshot(conn: Conn, on_date: str, created_at: str) -> dict[str, Any]:
    """Compute the current P&L/calibration and upsert a snapshot row per venue.

    Writes one row per (on_date, venue) for venue in (all, polymarket, kalshi):
    "all" is the aggregate across every venue, unfiltered. A venue with no
    settled bets still writes its row (n_bets=0), so the dashboard can always
    find a row for the day rather than treating an absent venue as missing data.
    """
    # One full-history read shared by every compute_pnl/compute_calibration call
    # below (#277: this halved the settled-history reads a snapshot run issues).
    rows = settled_rows(conn)
    per_venue: dict[str, dict[str, Any]] = {}
    for venue_label, venue in _SNAPSHOT_VENUES:
        pnl = compute_pnl(conn, venue=venue, rows=rows)
        cal = compute_calibration(conn, venue=venue, rows=rows)
        per_venue[venue_label] = {"pnl": pnl, "calibration": cal}
    # save_accuracy commits internally after each row; insert the snapshot rows only
    # after the loop so a mid-loop failure cannot leave committed snapshot rows
    # without their corresponding accuracy rows.
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
    for venue_label, stats in per_venue.items():
        _upsert_snapshot_row(
            conn, on_date, venue_label, stats["pnl"], stats["calibration"], created_at
        )
    conn.commit()
    aggregate = per_venue["all"]
    return {
        "pnl": aggregate["pnl"],
        "calibration": aggregate["calibration"],
        "venues": {k: v for k, v in per_venue.items() if k != "all"},
    }
