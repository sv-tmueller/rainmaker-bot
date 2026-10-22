"""Dry-run: routing table for the #462 regime refit, read-only against prod.

Emits per-(station, variable, lead) recon counts and the recon/archive route
(split at MIN_CAL_SAMPLES), plus the skip counters that gate CP3 (containment:
widespread NULL lead_time or pool decode gaps park CP3). Never echoes the DSN.

A Postgres DSN is refused unless --allow-prod is passed (#469). Usage:
    uv run python scripts/regime_refit_v3_dryrun.py [DSN_FILE] [--allow-prod]
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rainmaker.backfill_recon import build_recon_pairs, routing_table  # noqa: E402
from rainmaker.config import MIN_CAL_SAMPLES  # noqa: E402
from rainmaker.store.db import connect, refuse_prod_dsn  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("dsn_file", nargs="?", default="/tmp/batch462/dsn.env")
    ap.add_argument(
        "--allow-prod",
        action="store_true",
        help="permit reading a Postgres (prod) DSN; refused by default (#469)",
    )
    args = ap.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn and Path(args.dsn_file).exists():
        dsn = Path(args.dsn_file).read_text().strip()
    if not dsn:
        print("no DSN available", file=sys.stderr)
        return 2
    refuse_prod_dsn(dsn, allow_prod=args.allow_prod)
    conn = connect(dsn)
    try:
        pairs, counts = build_recon_pairs(conn)
    finally:
        conn.close()

    table = routing_table(pairs, min_cal_samples=MIN_CAL_SAMPLES)
    n_recon = sum(1 for r in table if r["route"] == "recon")
    print(
        f"cells with recon pairs: {len(table)};"
        f" routed recon: {n_recon}; archive: {len(table) - n_recon}"
    )
    print("skip counters:", json.dumps(counts, sort_keys=True))
    print()
    print(f"{'station':<7} {'var':<5} {'lead':>4} {'n_recon':>8}  route")
    for r in table:
        print(
            f"{r['station']:<7} {r['variable']:<5} {r['lead']:>4} {r['n_recon']:>8}  {r['route']}"
        )
    return 0


if __name__ == "__main__":
    from rainmaker.store.db import READ_METER, report_reads

    try:
        raise SystemExit(main())
    finally:
        if READ_METER.connections:
            report_reads()
