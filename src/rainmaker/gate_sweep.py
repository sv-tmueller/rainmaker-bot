"""Replay the recommendation gate over already-settled history (read-only diagnostic).

No refit, no gate change, no persistence: this module recomputes the
`recommended` decision at alternate min_edge/floor/lead/venue values from rows
already scored and settled, so a candidate gate change can be judged against
real history before it is ever shipped to `ranking/edge.py`.

Pinned semantics (the load-bearing decision; see the #336 sub-plan)
---------------------------------------------------------------------------
Four gates are inherited as-was rather than re-derived: the full-calibration
requirement (the `calibrated` tier is not stored per row), the min-sources
gate (`n_sources` is recorded only inside `dist_params`, which the
`settled_rows` shape does not carry), the station policy, and the
uncalibratable guard (neither is stored per row).

A (market, run) is "sweep-eligible" iff it has at least one stored
`recommended=1` row: the live run's own verdict that the market cleared those
four inherited gates in its era. Within an eligible (market, run), only the
swept axes are re-derived from the stored p_win/edge/side/lead: min_edge base
(with today's STATION_EDGE_DELTA re-added on top, since the stored `edge` is
raw -- see ranking/edge.py), the YES/NO confidence floors, the lead filter,
and venue. The best-edge bet is then re-picked via tracking's
`_best_per_market_run` tie-break and scored at the stored ask via
`_bet_won`/`compute_pnl`'s exact arithmetic.

Consequence (also a report footnote): loosening a gate (e.g. the NO floor)
only ever surfaces markets that produced at least one recommended bet at run
time, so a loosened row's bet count is a lower bound, not the true count a
permanently-loosened policy would have produced. Every named candidate in the
originating discussion (raising min_edge, excluding lead 0, a venue tilt) is a
tightening, so this only matters for the one loosening row in the NO-floor
grid (0.70 < the live 0.75).

Two anchor rows reconcile the sweep with `tracking.compute_pnl`:
- "live (as recorded)" (`as_recorded`): the stored recommended flags, exactly
  `compute_pnl`'s bet set. Matches the track headline by construction.
- "live (replayed)" (`replay_policy(rows, LIVE_POLICY)`): the same grid
  function at today's constants. Any delta from the as-recorded anchor is era
  drift (a pre-full-calibration recommendation, a pre-STATION_EDGE_DELTA KSFO
  TMAX bet, a rate recorded under a since-changed floor), not a bug.

Ungradable rows (an unparseable bucket label, the #333 crash class) are
skipped and counted in `skipped`, never raised.
"""

from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from rainmaker.config import (
    CONFIDENCE_FLOOR,
    CONFIDENCE_FLOOR_NO,
    KALSHI_STATIONS,
    MIN_EDGE,
    STATION_EDGE_DELTA,
    STATIONS,
)
from rainmaker.tracking import _best_per_market_run, _bet_won, _filter_venue, _wilson_interval

LeadFilter = Literal["all", "exclude_0", "exclude_le_0"]
VenueFilter = Literal["all", "polymarket", "kalshi"]

# One-factor-at-a-time grid: each axis varies alone, the rest held at live policy.
MIN_EDGE_GRID: tuple[float, ...] = (0.05, 0.07, 0.10, 0.12, 0.15)
YES_FLOOR_GRID: tuple[float, ...] = (0.80, 0.85, 0.90)
NO_FLOOR_GRID: tuple[float, ...] = (0.70, 0.75, 0.80, 0.85)
LEAD_GRID: tuple[LeadFilter, ...] = ("all", "exclude_0", "exclude_le_0")
VENUE_GRID: tuple[VenueFilter, ...] = ("all", "polymarket", "kalshi")


@dataclass(frozen=True)
class Policy:
    """One grid point: the swept gate values, held at live policy unless overridden."""

    label: str
    min_edge: float = MIN_EDGE
    floor: float = CONFIDENCE_FLOOR
    floor_no: float = CONFIDENCE_FLOOR_NO
    lead: LeadFilter = "all"
    venue: VenueFilter = "all"

    @property
    def is_loosening(self) -> bool:
        """True when any swept gate is looser than live: a population-pin lower bound."""
        return (
            self.min_edge < MIN_EDGE
            or self.floor < CONFIDENCE_FLOOR
            or self.floor_no < CONFIDENCE_FLOOR_NO
        )


LIVE_POLICY = Policy(label="live")


def _icao_for(row: dict[str, Any]) -> str | None:
    registry = KALSHI_STATIONS if (row.get("venue") == "kalshi") else STATIONS
    station = registry.get(row["city"])
    return station.icao if station is not None else None


def _lead_for(row: dict[str, Any]) -> int | None:
    try:
        return (
            date.fromisoformat(row["settlement_date"]) - date.fromisoformat(row["started_at"][:10])
        ).days
    except (ValueError, KeyError):
        return None


def _lead_survives(row: dict[str, Any], lead: LeadFilter) -> bool:
    if lead == "all":
        return True
    computed = _lead_for(row)
    if computed is None:
        return False
    if lead == "exclude_0":
        return computed != 0
    return computed > 0  # exclude_le_0: drop 0 and negative (catch-up) leads


def _recompute_recommended(row: dict[str, Any], policy: Policy) -> bool:
    """Re-derive `recommended` for one row at policy's min_edge/floor, side-aware.

    edge is the stored raw value (p_win - ask / p_no - no_ask, no station delta
    folded in -- see ranking/edge.py); today's STATION_EDGE_DELTA is re-added
    here so the effective threshold matches what evaluate_market would apply
    now, even though `policy.min_edge` sweeps only the base.
    """
    edge = row.get("edge")
    if edge is None:
        return False
    icao = _icao_for(row)
    delta = STATION_EDGE_DELTA.get((icao, row["variable"]), 0.0) if icao is not None else 0.0
    effective_min_edge = policy.min_edge + delta
    side = row.get("side") or "YES"
    floor = policy.floor_no if side == "NO" else policy.floor
    return bool(row["p_win"] >= floor and edge >= effective_min_edge)


def _score(bets: list[dict[str, Any]]) -> dict[str, Any]:
    """Grade an already-picked bet list: wins/pnl/staked, skipping ungradable rows."""
    n = 0
    wins = 0
    pnl = 0.0
    staked = 0.0
    skipped = 0
    for r in bets:
        # _bet_won returns None for an ungradable row since #333; the except
        # stays as a belt for spec shapes the seam does not cover.
        try:
            won = _bet_won(r)
        except (ValueError, KeyError):
            skipped += 1
            continue
        if won is None:
            skipped += 1
            continue
        n += 1
        ask = r["ask"]
        staked += ask
        if won:
            wins += 1
            pnl += 1 - ask
        else:
            pnl -= ask
    lo, hi = _wilson_interval(wins, n)
    return {
        "n_bets": n,
        "wins": wins,
        "losses": n - wins,
        "hit_rate": wins / n if n else None,
        "wilson_lo": lo,
        "wilson_hi": hi,
        "pnl": pnl,
        "staked": staked,
        "roi": pnl / staked if staked else 0.0,
        "skipped": skipped,
    }


def as_recorded(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The live bet set exactly as stored: no re-derivation, stored recommended flags.

    Reconciles with `tracking.compute_pnl`'s headline by construction (same
    tie-break, same arithmetic); the only difference is ungradable rows are
    skipped and counted here instead of raising.
    """
    bets = _best_per_market_run(rows)
    return {"label": "live (as recorded)", "lower_bound": False, **_score(bets)}


def replay_policy(rows: list[dict[str, Any]], policy: Policy) -> dict[str, Any]:
    """Re-derive recommended bets at `policy` over sweep-eligible (market, run) pairs.

    See the module docstring for the sweep-eligibility and re-derivation rules.
    """
    eligible = {(r["market_id"], r["run_id"]) for r in rows if r.get("recommended")}
    pop = [r for r in rows if (r["market_id"], r["run_id"]) in eligible]
    pop = _filter_venue(pop, None if policy.venue == "all" else policy.venue)
    pop = [r for r in pop if _lead_survives(r, policy.lead)]

    recomputed = [{**r, "recommended": _recompute_recommended(r, policy)} for r in pop]
    bets = _best_per_market_run(recomputed)
    return {"label": policy.label, "lower_bound": policy.is_loosening, **_score(bets)}


def run_sweep(rows: list[dict[str, Any]], since: str | None = None) -> dict[str, Any]:
    """Build the two anchors, five OFAT grids, and one combined table.

    since (an ISO "YYYY-MM-DD" string) restricts to rows whose run started on
    or after that date, applied post-dedup exactly as
    tracking.compute_calibration_by_cell's --since (#323): `rows` is already
    settled_rows()'s latest-run-per-(market, UTC day) result, and since is a
    monotone on-or-after threshold, so filtering here gives the same
    surviving winners as filtering before the dedup would have.
    """
    if since is not None:
        rows = [r for r in rows if r["started_at"] >= since]

    anchors = {
        "as_recorded": as_recorded(rows),
        "replayed_live": replay_policy(rows, LIVE_POLICY),
    }
    min_edge_grid = [
        replay_policy(rows, Policy(label=f"{v:.2f}", min_edge=v)) for v in MIN_EDGE_GRID
    ]
    floor_yes_grid = [
        replay_policy(rows, Policy(label=f"{v:.2f}", floor=v)) for v in YES_FLOOR_GRID
    ]
    floor_no_grid = [
        replay_policy(rows, Policy(label=f"{v:.2f}", floor_no=v)) for v in NO_FLOOR_GRID
    ]
    lead_grid = [replay_policy(rows, Policy(label=v, lead=v)) for v in LEAD_GRID]
    venue_grid = [replay_policy(rows, Policy(label=v, venue=v)) for v in VENUE_GRID]
    combined = [
        replay_policy(rows, Policy(label=f"{v:.2f}/excl0", min_edge=v, lead="exclude_0"))
        for v in MIN_EDGE_GRID
    ]

    return {
        "anchors": anchors,
        "min_edge": min_edge_grid,
        "floor_yes": floor_yes_grid,
        "floor_no": floor_no_grid,
        "lead": lead_grid,
        "venue": venue_grid,
        "combined_min_edge_lead0": combined,
    }
