"""Dry-run: routing table for the #462 regime refit, read-only against prod.

Emits per-(station, variable, lead) recon counts and the recon/archive route
(split at MIN_CAL_SAMPLES), plus the skip counters that gate CP3 (containment:
widespread NULL lead_time or pool decode gaps park CP3). Never echoes the DSN.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rainmaker.backfill_recon import build_recon_pairs, routing_table  # noqa: E402
from rainmaker.config import MIN_CAL_SAMPLES  # noqa: E402
from rainmaker.store.db import connect  # noqa: E402


def main() -> int:
    dsn = os.environ.get("DATABASE_URL")
    dsn_file = sys.argv[1] if len(sys.argv) > 1 else "/tmp/batch462/dsn.env"
    if not dsn and Path(dsn_file).exists():
        dsn = Path(dsn_file).read_text().strip()
    if not dsn:
        print("no DSN available", file=sys.stderr)
        return 2
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
    raise SystemExit(main())
