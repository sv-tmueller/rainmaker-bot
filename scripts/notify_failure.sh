#!/usr/bin/env bash
# Notify on workflow failure by creating or updating one deduplicated GitHub
# issue per workflow. The body carries the failing job and step names plus a
# log tail, so triage does not require opening the run.
# Usage: notify_failure.sh <workflow-name> <run-url> <run-id>
# Reads GH_TOKEN from env (set to GITHUB_TOKEN in the calling workflow step).
set -euo pipefail

WORKFLOW_NAME="${1:?workflow name required}"
RUN_URL="${2:?run URL required}"
RUN_ID="${3:?run id required}"
ISSUE_TITLE="ops: ${WORKFLOW_NAME} workflow failing"
LOG_TAIL_LINES=40

# Job and step names come from the jobs API, a surface that is always
# populated once a run exists. Filter to the first failing job and, within
# it, the first failing step; fall back to "unknown" for either if the job
# array is empty (for example a lookup made before the API has caught up).
JOB_STEP_FILTER='([.jobs[] | select(.conclusion=="failure")][0]) as $job
  | if $job == null then ["unknown","unknown"]
    else [$job.name, (([$job.steps[]? | select(.conclusion=="failure") | .name])[0] // "unknown")]
    end
  | @tsv'
JOB_STEP_TSV=$(gh api "repos/{owner}/{repo}/actions/runs/${RUN_ID}/jobs" --jq "$JOB_STEP_FILTER" 2>/dev/null) || true
IFS=$'\t' read -r JOB_NAME STEP_NAME <<< "$JOB_STEP_TSV" || true
JOB_NAME="${JOB_NAME:-unknown}"
STEP_NAME="${STEP_NAME:-unknown}"

# The log fetch is a separate and less reliable call: job logs can 404 with
# "log not found" shortly after a run fails, independent of whether the jobs
# API above succeeded (#313). Never let this abort the alert.
LOG_TAIL=$(gh run view "${RUN_ID}" --log-failed 2>/dev/null | tail -n "$LOG_TAIL_LINES") || true
if [ -z "$LOG_TAIL" ]; then
  LOG_TAIL="(log unavailable)"
fi

BODY=$(cat <<EOF
Workflow **${WORKFLOW_NAME}** failed.

Run: ${RUN_URL}
Job: ${JOB_NAME}
Step: ${STEP_NAME}

Log tail (last ${LOG_TAIL_LINES} lines):
\`\`\`
${LOG_TAIL}
\`\`\`
EOF
)

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
  gh issue comment "$ISSUE_NUMBER" --body "$BODY"
  echo "Added comment to existing issue #${ISSUE_NUMBER}"
else
  gh issue create \
    --title "$ISSUE_TITLE" \
    --body "$BODY"
  echo "Created new issue: ${ISSUE_TITLE}"
fi
