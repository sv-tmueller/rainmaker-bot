#!/usr/bin/env bash
# Notify on workflow failure by creating or updating one deduplicated GitHub issue.
# Usage: notify_failure.sh <workflow-name> <run-url>
# Reads GH_TOKEN from env (set to GITHUB_TOKEN in the calling workflow step).
set -euo pipefail

WORKFLOW_NAME="${1:?workflow name required}"
RUN_URL="${2:?run URL required}"
ISSUE_TITLE="ops: scheduled workflow failing"
BODY="Workflow **${WORKFLOW_NAME}** failed.\n\nRun: ${RUN_URL}"

# Search for an existing open issue with the exact title. The server-side
# title search keeps the ops issue in the result set even past the default
# 30-item page; the exact-title select guards against a substring match.
ISSUE_NUMBER=$(gh issue list \
  --state open \
  --search "${ISSUE_TITLE} in:title" \
  --json number,title \
  --jq ".[] | select(.title == \"${ISSUE_TITLE}\") | .number" \
  | head -1)

if [ -n "$ISSUE_NUMBER" ]; then
  gh issue comment "$ISSUE_NUMBER" --body "$(printf '%b' "$BODY")"
  echo "Added comment to existing issue #${ISSUE_NUMBER}"
else
  gh issue create \
    --title "$ISSUE_TITLE" \
    --body "$(printf '%b' "$BODY")"
  echo "Created new issue: ${ISSUE_TITLE}"
fi
