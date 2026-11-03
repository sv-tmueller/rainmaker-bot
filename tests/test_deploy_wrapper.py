"""Tests for the host deploy wrapper and failure scripts under deploy/ (#480).

Each test builds a temp tree that mirrors the install layout (deploy/ scripts,
a stub venv console script, scripts/notify_failure.sh) and a temp
STATE_DIRECTORY, then runs the real shell script against it. Nothing here
reaches a network, a database or systemd.
"""

import fcntl
import os
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DEPLOY = REPO / "deploy"
FAKE_DSN = "postgresql://user:secret@example.invalid:5432/db"
PING_URL = "https://hc.example.invalid/ping/abc"

# The stub appends one line per call: the subcommand, the DSN prefix and args.
STUB_CONSOLE_SCRIPT = """#!/usr/bin/env bash
echo "$1 db=${DATABASE_URL:0:10} args=$*" >> "$STUB_LOG"
if [ "${STUB_FAIL_ON:-}" = "$1" ]; then exit 3; fi
"""

# A curl stand-in that records the URL it was asked to hit (the last argument).
STUB_CURL = """#!/usr/bin/env bash
echo "${@: -1}" >> "$STUB_LOG.curl"
if [ -n "${STUB_CURL_FAIL:-}" ]; then exit 22; fi
"""

STUB_NOTIFY = """#!/usr/bin/env bash
{
  echo "args:$*"
  echo "token:${GH_TOKEN:-}"
  echo "repo:${GH_REPO:-}"
  echo "dsn:${DATABASE_URL:-}"
} > "$STUB_LOG.notify"
"""


def _executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def tree(tmp_path: Path) -> dict[str, Path]:
    root = tmp_path / "opt"
    (root / "deploy").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / ".venv" / "bin").mkdir(parents=True)
    for name in ("rainmaker-run.sh", "rainmaker-run-failure.sh"):
        src = DEPLOY / name
        if src.exists():
            shutil.copy(src, root / "deploy" / name)
    _executable(root / ".venv" / "bin" / "rainmaker", STUB_CONSOLE_SCRIPT)
    _executable(root / "scripts" / "notify_failure.sh", STUB_NOTIFY)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _executable(bindir / "curl", STUB_CURL)
    state = tmp_path / "state"
    state.mkdir()
    return {"root": root, "state": state, "log": tmp_path / "stub.log", "bin": bindir}


def _run(
    tree: dict[str, Path], script: str, extra: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": f"{tree['bin']}:{os.environ['PATH']}",
        "STATE_DIRECTORY": str(tree["state"]),
        "STUB_LOG": str(tree["log"]),
        "HOME": str(tree["state"]),
        **extra,
    }
    return subprocess.run(
        ["bash", str(tree["root"] / "deploy" / script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def run_wrapper(tree: dict[str, Path], **extra: str) -> subprocess.CompletedProcess[str]:
    return _run(tree, "rainmaker-run.sh", extra)


def run_failure(tree: dict[str, Path], **extra: str) -> subprocess.CompletedProcess[str]:
    return _run(tree, "rainmaker-run-failure.sh", extra)


def calls(tree: dict[str, Path]) -> list[str]:
    log = tree["log"]
    if not log.exists():
        return []
    return [line.split()[0] for line in log.read_text().splitlines()]


def curl_urls(tree: dict[str, Path]) -> list[str]:
    log = Path(f"{tree['log']}.curl")
    return log.read_text().split() if log.exists() else []


def notify_record(tree: dict[str, Path]) -> str:
    return Path(f"{tree['log']}.notify").read_text()


@pytest.mark.parametrize(
    "dsn",
    [None, "", "sqlite:///rainmaker.db", "rainmaker.db", "mysql://u:p@h/db"],
)
def test_non_postgres_dsn_is_refused_without_running_anything(tree, dsn):
    extra = {} if dsn is None else {"DATABASE_URL": dsn}
    result = run_wrapper(tree, **extra)
    assert result.returncode == 1
    assert "refusing to run" in result.stdout + result.stderr
    assert calls(tree) == []


@pytest.mark.parametrize("scheme", ["postgres", "postgresql"])
def test_postgres_dsn_runs_run_trade_settle_prune_in_order(tree, scheme):
    dsn = FAKE_DSN.replace("postgresql", scheme)
    result = run_wrapper(tree, DATABASE_URL=dsn)
    assert result.returncode == 0, result.stderr
    assert calls(tree) == ["run", "trade", "settle", "prune"]
    assert f"db={dsn[:10]}" in tree["log"].read_text()


def test_run_gets_the_state_dir_reports_path(tree):
    run_wrapper(tree, DATABASE_URL=FAKE_DSN)
    first = tree["log"].read_text().splitlines()[0]
    assert f"--reports-dir {tree['state']}/reports" in first


def test_failing_step_stops_the_chain_and_fails_the_job(tree):
    result = run_wrapper(tree, DATABASE_URL=FAKE_DSN, STUB_FAIL_ON="run")
    assert result.returncode == 3
    assert calls(tree) == ["run"]


def test_failing_trade_stops_settle_and_prune_and_fails_the_job(tree):
    result = run_wrapper(tree, DATABASE_URL=FAKE_DSN, STUB_FAIL_ON="trade")
    assert result.returncode == 3
    assert calls(tree) == ["run", "trade"]


def test_trade_runs_with_no_arguments_so_the_mode_stays_in_the_env_file(tree):
    run_wrapper(tree, DATABASE_URL=FAKE_DSN)
    trade_line = tree["log"].read_text().splitlines()[1]
    assert trade_line.endswith("args=trade")


def test_the_trade_step_is_never_wired_into_the_scheduled_workflow():
    workflow = (REPO / ".github" / "workflows" / "daily-run.yml").read_text()
    assert re.search(r"rainmaker\s+trade", workflow) is None


def test_held_lock_exits_75_without_running_anything(tree):
    with open(tree["state"] / "run.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = run_wrapper(tree, DATABASE_URL=FAKE_DSN)
    assert result.returncode == 75
    assert calls(tree) == []


def test_lock_is_released_after_a_run(tree):
    assert run_wrapper(tree, DATABASE_URL=FAKE_DSN).returncode == 0
    assert run_wrapper(tree, DATABASE_URL=FAKE_DSN).returncode == 0
    assert calls(tree) == ["run", "trade", "settle", "prune"] * 2


def test_old_reports_are_pruned_and_recent_ones_kept(tree):
    reports = tree["state"] / "reports"
    reports.mkdir()
    old, fresh = reports / "old.json", reports / "fresh.json"
    old.write_text("{}")
    fresh.write_text("{}")
    stale = time.time() - 9 * 86400
    os.utime(old, (stale, stale))
    assert run_wrapper(tree, DATABASE_URL=FAKE_DSN).returncode == 0
    assert not old.exists()
    assert fresh.exists()


def test_pings_start_then_success_when_configured(tree):
    result = run_wrapper(tree, DATABASE_URL=FAKE_DSN, RAINMAKER_HEALTHCHECK_URL=PING_URL)
    assert result.returncode == 0
    assert curl_urls(tree) == [f"{PING_URL}/start", PING_URL]


def test_no_pings_when_url_unset(tree):
    run_wrapper(tree, DATABASE_URL=FAKE_DSN)
    assert curl_urls(tree) == []


def test_failed_step_sends_start_ping_but_no_success_ping(tree):
    run_wrapper(
        tree,
        DATABASE_URL=FAKE_DSN,
        RAINMAKER_HEALTHCHECK_URL=PING_URL,
        STUB_FAIL_ON="settle",
    )
    assert curl_urls(tree) == [f"{PING_URL}/start"]


def test_ping_failure_never_fails_the_job(tree):
    result = run_wrapper(
        tree,
        DATABASE_URL=FAKE_DSN,
        RAINMAKER_HEALTHCHECK_URL=PING_URL,
        STUB_CURL_FAIL="1",
    )
    assert result.returncode == 0
    assert calls(tree) == ["run", "trade", "settle", "prune"]


def test_failure_script_pings_fail_and_files_an_issue_with_a_pointer(tree):
    result = run_failure(
        tree,
        RAINMAKER_HEALTHCHECK_URL=PING_URL,
        RAINMAKER_ALERT_GH_TOKEN="tok-123",
        MONITOR_INVOCATION_ID="inv-42",
        MONITOR_SERVICE_RESULT="exit-code",
        MONITOR_EXIT_STATUS="3",
        DATABASE_URL=FAKE_DSN,
    )
    assert result.returncode == 0, result.stderr
    assert curl_urls(tree) == [f"{PING_URL}/fail"]
    notify = notify_record(tree)
    assert "args:host-run " in notify
    assert "inv-42" in notify
    assert "exit-code" in notify
    assert "token:tok-123" in notify
    assert "repo:sv-tmueller/rainmaker-bot" in notify
    assert "secret" not in notify


def test_failure_script_never_names_the_host(tree):
    unique_host = "unique-box-7f3a9c"
    result = run_failure(
        tree,
        RAINMAKER_ALERT_GH_TOKEN="tok-123",
        MONITOR_INVOCATION_ID="inv-42",
        HOSTNAME=unique_host,
    )
    assert result.returncode == 0, result.stderr
    assert unique_host not in notify_record(tree)
    source = (DEPLOY / "rainmaker-run-failure.sh").read_text().lower()
    assert "hostname" not in source
    assert "uname" not in source


def test_failure_script_without_token_logs_and_exits_1(tree):
    result = run_failure(tree, MONITOR_INVOCATION_ID="inv-42")
    assert result.returncode == 1
    assert "RAINMAKER_ALERT_GH_TOKEN" in result.stdout + result.stderr
    assert not Path(f"{tree['log']}.notify").exists()
