"""Exercise scripts/notify_failure.sh against a stub `gh` on PATH.

No real GitHub issue is created or touched: `gh` itself is replaced with a
fake executable that logs every invocation and returns canned output
controlled by env vars, so the script's calls (title, dedupe search, jobs
API lookup, log fetch, issue create/comment) can be asserted without any
network access.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_SCRIPT = str(REPO_ROOT / "scripts" / "notify_failure.sh")
# Overridable so a manual check can point this at an older script revision.
SCRIPT = Path(os.environ.get("NOTIFY_FAILURE_SCRIPT", _DEFAULT_SCRIPT))

DEFAULT_ARGS = ("scheduled-run", "https://github.com/o/r/actions/runs/1", "1")

# Args within one gh call are NUL-separated and calls are separated by 0x01,
# so an argument containing embedded newlines (the issue body) can never be
# mistaken for a call boundary by the parser below.
FAKE_GH = r"""#!/usr/bin/env bash
set -u
CALL_LOG="${FAKE_GH_CALL_LOG:?FAKE_GH_CALL_LOG not set}"
{
  for arg in "$@"; do
    printf '%s\0' "$arg"
  done
  printf '\x01'
} >> "$CALL_LOG"

case "${1:-}" in
  api)
    if [ "${FAKE_GH_JOBS_FAIL:-0}" = "1" ]; then
      echo "gh: log not found (HTTP 404)" >&2
      exit 1
    fi
    printf '%s\n' "${FAKE_GH_JOBS_TSV:-}"
    ;;
  run)
    if [ "${FAKE_GH_LOG_FAIL:-0}" = "1" ]; then
      echo "log not found" >&2
      exit 1
    fi
    printf '%s\n' "${FAKE_GH_LOG_OUTPUT:-}"
    ;;
  issue)
    case "${2:-}" in
      list)
        printf '%s\n' "${FAKE_GH_EXISTING_ISSUE:-}"
        ;;
      comment|create)
        exit 0
        ;;
      *)
        exit 1
        ;;
    esac
    ;;
  *)
    exit 1
    ;;
esac
"""


def _parse_calls(call_log: Path) -> list[list[str]]:
    """Split the append-only call log into one argv list per gh invocation."""
    if not call_log.exists():
        return []
    raw = call_log.read_bytes()
    calls = []
    for chunk in raw.split(b"\x01"):
        if not chunk:
            continue
        parts = chunk.split(b"\0")[:-1]  # drop the empty tail after the last NUL
        calls.append([p.decode() for p in parts])
    return calls


@pytest.fixture
def gh_stub(tmp_path: Path) -> tuple[dict[str, str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh_path = bin_dir / "gh"
    gh_path.write_text(FAKE_GH)
    gh_path.chmod(gh_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    call_log = tmp_path / "calls.log"
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["GH_TOKEN"] = "fake-token-for-tests"
    env["FAKE_GH_CALL_LOG"] = str(call_log)
    env["FAKE_GH_EXISTING_ISSUE"] = ""
    return env, call_log


def _run(
    env: dict[str, str], args: tuple[str, ...] = DEFAULT_ARGS
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


def _creates(call_log: Path) -> list[list[str]]:
    return [c for c in _parse_calls(call_log) if c[:2] == ["issue", "create"]]


def _comments(call_log: Path) -> list[list[str]]:
    return [c for c in _parse_calls(call_log) if c[:2] == ["issue", "comment"]]


def test_two_workflows_create_separate_issues(gh_stub: tuple[dict[str, str], Path]) -> None:
    env, call_log = gh_stub

    result_a = _run(env, args=("workflow-a", "https://example/actions/runs/1", "1"))
    result_b = _run(env, args=("workflow-b", "https://example/actions/runs/2", "2"))

    assert result_a.returncode == 0, result_a.stderr
    assert result_b.returncode == 0, result_b.stderr

    creates = _creates(call_log)
    assert len(creates) == 2
    titles = [c[c.index("--title") + 1] for c in creates]
    assert titles == [
        "ops: workflow-a workflow failing",
        "ops: workflow-b workflow failing",
    ]


def test_same_workflow_second_failure_comments_on_existing_issue(
    gh_stub: tuple[dict[str, str], Path],
) -> None:
    env, call_log = gh_stub
    env["FAKE_GH_EXISTING_ISSUE"] = "42"

    result = _run(env, args=("scheduled-run", "https://example/actions/runs/9", "9"))

    assert result.returncode == 0, result.stderr
    comments = _comments(call_log)
    assert len(comments) == 1
    assert comments[0][2] == "42"
    assert _creates(call_log) == []


def test_body_contains_job_step_and_log_tail(gh_stub: tuple[dict[str, str], Path]) -> None:
    env, call_log = gh_stub
    env["FAKE_GH_JOBS_TSV"] = "run\tRun the bot"
    env["FAKE_GH_LOG_OUTPUT"] = "Traceback (most recent call last):\nValueError: boom"

    result = _run(env)

    assert result.returncode == 0, result.stderr
    creates = _creates(call_log)
    assert len(creates) == 1
    body = creates[0][creates[0].index("--body") + 1]
    assert "Job: run" in body
    assert "Step: Run the bot" in body
    assert "ValueError: boom" in body


def test_log_fetch_failure_still_completes_with_placeholder(
    gh_stub: tuple[dict[str, str], Path],
) -> None:
    env, call_log = gh_stub
    env["FAKE_GH_JOBS_TSV"] = "run\tRun the bot"
    env["FAKE_GH_LOG_FAIL"] = "1"

    result = _run(env)

    assert result.returncode == 0, result.stderr
    creates = _creates(call_log)
    assert len(creates) == 1
    body = creates[0][creates[0].index("--body") + 1]
    assert "(log unavailable)" in body


def test_jobs_api_failure_falls_back_to_unknown(gh_stub: tuple[dict[str, str], Path]) -> None:
    env, call_log = gh_stub
    env["FAKE_GH_JOBS_FAIL"] = "1"
    env["FAKE_GH_LOG_OUTPUT"] = "some log line"

    result = _run(env)

    assert result.returncode == 0, result.stderr
    creates = _creates(call_log)
    assert len(creates) == 1
    body = creates[0][creates[0].index("--body") + 1]
    assert "Job: unknown" in body
    assert "Step: unknown" in body
