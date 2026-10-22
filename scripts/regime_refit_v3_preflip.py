"""Pre-flip replay for #462: compute the post-refit cells read-only, no save.

Routes every (station, variable, lead) cell exactly as run_backfill(conn=...)
would (recon pairs where dense and regime-eligible, archive fallback
otherwise), fits each cell with the production fitters, and compares the
resulting var_b against the #459 apply-arm table (the var_b the live path's
statistic implies). Broad reproduction of those values is the checkpoint's
green light; large unexplained divergence stops the checkpoint.

Read-only SELECTs; DSN never echoed. Usage:
    uv run python scripts/regime_refit_v3_preflip.py [--dsn-file PATH]
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rainmaker.backfill import run_backfill, season_window  # noqa: E402
from rainmaker.backfill_recon import build_recon_pairs  # noqa: E402
from rainmaker.config import (  # noqa: E402
    CALIBRATION_FAMILY,
    MIN_CAL_SAMPLES,
    STATIONS,
)
from rainmaker.httpclient import build_backfill_client  # noqa: E402
from rainmaker.probability.calibration import (  # noqa: E402
    fit_calibration,
    fit_student_t_free_df,
)
from rainmaker.store.db import connect  # noqa: E402


def main() -> int:
    dsn = os.environ.get("DATABASE_URL")
    idx = sys.argv.index("--dsn-file") + 1 if "--dsn-file" in sys.argv else None
    dsn_file = sys.argv[idx] if idx is not None else "/tmp/batch462/dsn.env"
    if not dsn and Path(dsn_file).exists():
        dsn = Path(dsn_file).read_text().strip()
    if not dsn:
        print("no DSN available", file=sys.stderr)
        return 2

    conn = connect(dsn)
    client = build_backfill_client()
    try:
        window = season_window(date.today())
        if window is None:
            print("season window empty; aborting", file=sys.stderr)
            return 2
        start, end = window
        recon_pairs, counts = build_recon_pairs(conn, since=start.isoformat())
        print("recon cells:", len(recon_pairs), "| skip counters:", counts)

        # Which cells exist in the STORED calibration table (the refit's scope)?
        stored = {
            (r["station"], r["variable"], r["lead"])
            for r in conn.execute(
                "SELECT station, variable, lead_time AS lead FROM calibration"
            ).fetchall()
        }

        # Group the stored cells per (station, variable) for iteration.
        by_station: dict[str, list[str]] = {}
        for station, variable, _lead in stored:
            by_station.setdefault(station, []).append(variable)
        printed_header = False
        for station_icao in sorted(by_station):
            station = next((s for s in STATIONS.values() if s.icao == station_icao), None)
            if station is None:
                continue
            for variable in sorted(set(by_station[station_icao])):
                leads = tuple(
                    sorted(le for (s, v, le) in stored if s == station_icao and v == variable)
                )
                # Route per cell, mirroring run_backfill's rule.
                for lead in leads:
                    cell_recon = recon_pairs.get((station_icao, variable, lead), [])
                    if len(cell_recon) >= MIN_CAL_SAMPLES:
                        fitter = (
                            fit_student_t_free_df
                            if CALIBRATION_FAMILY.get(variable) == "student_t"
                            else fit_calibration
                        )
                        cal = fitter(station_icao, variable, lead, cell_recon)
                        src = f"recon n={len(cell_recon)}"
                    else:
                        results = run_backfill(
                            station, variable, (lead,), start, end, client, conn=None
                        )
                        if lead not in results:
                            print(f"{station_icao} {variable} {lead}: no fit (archive)")
                            continue
                        cal = results[lead][0]
                        src = f"archive n={cal.n_samples}"
                    if not printed_header:
                        print("\nstation var lead var_a var_b df n source")
                        printed_header = True
                    df = "" if cal.df is None else f"{cal.df:.1f}"
                    print(
                        f"{station_icao} {variable} {lead} {cal.var_a:.3f} {cal.var_b:.3f} "
                        f"{df} {cal.n_samples} {src}"
                    )
    finally:
        client.close()
        conn.close()
    return 0


if __name__ == "__main__":
    from rainmaker.store.db import READ_METER, report_reads

    try:
        raise SystemExit(main())
    finally:
        if READ_METER.connections:
            report_reads()
