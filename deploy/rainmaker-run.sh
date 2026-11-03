#!/usr/bin/env bash
# The 3h job on a host: run, trade, settle, prune against Supabase Postgres.
# Started by rainmaker-run.service; also safe to run by hand as the service
# user. trade is a no-op unless RAINMAKER_TRADE_MODE=paper is set in the env
# file (unset, off or unknown does nothing, live is refused).
#
# Exit codes: 0 done, 1 refused (bad DSN) or a step failed, 75 another run
# holds the lock (the unit treats 75 as success, so a skip raises no alert).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="${STATE_DIRECTORY:-/var/lib/rainmaker}"
REPORT_RETENTION_DAYS=7

# Same guard as daily-run.yml: without a Postgres DSN the CLI would write to a
# local SQLite file and the run would look green while saving nothing shared.
case "${DATABASE_URL:-}" in
  postgres://*|postgresql://*) ;;
  *)
    echo "DATABASE_URL is not a Postgres DSN; refusing to run (would silently write to a local SQLite file)" >&2
    exit 1
    ;;
esac

# Overlap lock. Replaces the Actions concurrency group, with one difference:
# the new run is skipped and the in-flight run is left alone. The deploy
# procedure takes the same lock (flock -w) so a deploy never swaps the venv
# under a running job.
exec 9>"$STATE_DIR/run.lock"
if ! flock -n 9; then
  echo "another run holds $STATE_DIR/run.lock; skipping this run" >&2
  exit 75
fi

# Dead-man ping (healthchecks.io conventions). Optional, and a failed ping
# never fails the job. The URL is a credential, so it is never echoed.
ping_monitor() {
  [ -n "${RAINMAKER_HEALTHCHECK_URL:-}" ] || return 0
  curl -fsS -m 10 --retry 3 "${RAINMAKER_HEALTHCHECK_URL%/}${1:-}" >/dev/null \
    || echo "healthcheck ping failed (ignored)" >&2
  return 0
}

ping_monitor /start

echo "rainmaker version: $(git -C "$REPO_DIR" describe --tags --always 2>/dev/null || echo unknown)"

if [ -d "$STATE_DIR/reports" ]; then
  find "$STATE_DIR/reports" -type f -mtime "+$REPORT_RETENTION_DAYS" -delete
fi

RAINMAKER="$REPO_DIR/.venv/bin/rainmaker"
"$RAINMAKER" run --reports-dir "$STATE_DIR/reports"
"$RAINMAKER" trade
"$RAINMAKER" settle
"$RAINMAKER" prune

ping_monitor
