#!/usr/bin/env bash
# OnFailure hook for rainmaker-run.service. Pings the dead-man monitor's /fail
# endpoint, then files (or updates) one GitHub issue through the repo's
# scripts/notify_failure.sh, unchanged.
#
# The repo is public, so the issue body is world-readable. It carries only a
# pointer to the journal entry: no journal text and no host name.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# The alert path never needs these. The unit unsets them as well.
unset DATABASE_URL RAINYDAY_SYNOPTIC_TOKEN

if [ -n "${RAINMAKER_HEALTHCHECK_URL:-}" ]; then
  curl -fsS -m 10 --retry 3 "${RAINMAKER_HEALTHCHECK_URL%/}/fail" >/dev/null \
    || echo "healthcheck fail ping failed (ignored)" >&2
fi

if [ -z "${RAINMAKER_ALERT_GH_TOKEN:-}" ]; then
  echo "RAINMAKER_ALERT_GH_TOKEN is unset; no GitHub alert sent" >&2
  exit 1
fi

INVOCATION="${MONITOR_INVOCATION_ID:-unknown}"
POINTER="rainmaker-run.service ended with result=${MONITOR_SERVICE_RESULT:-unknown} status=${MONITOR_EXIT_STATUS:-unknown}. On the host: journalctl -u rainmaker-run.service _SYSTEMD_INVOCATION_ID=${INVOCATION}"

GH_TOKEN="$RAINMAKER_ALERT_GH_TOKEN" \
GH_REPO="sv-tmueller/rainmaker-bot" \
GH_NO_UPDATE_NOTIFIER=1 \
GH_PROMPT_DISABLED=1 \
  bash "$REPO_DIR/scripts/notify_failure.sh" host-run "$POINTER" "$INVOCATION"
