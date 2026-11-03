"""Static checks on the systemd units under deploy/ (#480).

`systemd-analyze verify` exits 0 even for unknown keys and bad values, and
only prints "ignoring ..." for them. So this test fails on any output, not just
a nonzero exit. It skips only where the tool is absent (CI's ubuntu runner has
it, so the skip is a local-only case).
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
UNITS = [
    DEPLOY / "rainmaker-run.service",
    DEPLOY / "rainmaker-run-failure.service",
    DEPLOY / "rainmaker-run.timer",
]


def _directives(path: Path) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for line in path.read_text().splitlines():
        match = re.match(r"^([A-Za-z_]+)=(.*)$", line)
        if match:
            found.setdefault(match.group(1), []).append(match.group(2))
    return found


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd-analyze not installed")
def test_units_pass_systemd_analyze_verify_with_no_output():
    result = subprocess.run(
        ["systemd-analyze", "verify", "--man=no", *map(str, UNITS)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout + result.stderr == ""


def test_all_unit_files_exist():
    assert [u.name for u in UNITS if not u.exists()] == []


def test_run_service_contract():
    d = _directives(DEPLOY / "rainmaker-run.service")
    assert d["Type"] == ["oneshot"]
    assert d["User"] == ["rainmaker"]
    assert d["EnvironmentFile"] == ["/etc/rainmaker/rainmaker.env"]
    assert d["SuccessExitStatus"] == ["75"]
    assert d["OnFailure"] == ["rainmaker-run-failure.service"]
    assert d["TimeoutStartSec"] == ["20min"]
    assert d["StateDirectory"] == ["rainmaker"]
    assert d["ExecStart"] == ["/usr/bin/bash /opt/rainmaker/deploy/rainmaker-run.sh"]
    assert "RAINMAKER_ALERT_GH_TOKEN" in d["UnsetEnvironment"][0]


def test_failure_service_drops_db_credentials_and_has_no_failure_hook():
    d = _directives(DEPLOY / "rainmaker-run-failure.service")
    assert d["Type"] == ["oneshot"]
    assert d["EnvironmentFile"] == ["/etc/rainmaker/rainmaker.env"]
    assert d["ExecStart"] == ["/usr/bin/bash /opt/rainmaker/deploy/rainmaker-run-failure.sh"]
    unset = d["UnsetEnvironment"][0]
    assert "DATABASE_URL" in unset
    assert "RAINYDAY_SYNOPTIC_TOKEN" in unset
    assert "OnFailure" not in d


@pytest.mark.parametrize("name", ["rainmaker-run.service", "rainmaker-run-failure.service"])
def test_services_are_sandboxed(name):
    d = _directives(DEPLOY / name)
    assert d["NoNewPrivileges"] == ["true"]
    assert d["ProtectSystem"] == ["strict"]
    assert d["PrivateTmp"] == ["true"]
    assert d["CapabilityBoundingSet"] == [""]
    assert d["UMask"] == ["0027"]


def test_timer_fires_every_3h_in_utc_and_catches_up_once():
    d = _directives(DEPLOY / "rainmaker-run.timer")
    assert d["OnCalendar"] == ["*-*-* 00/3:00:00 UTC"]
    assert d["Persistent"] == ["true"]
    assert d["RandomizedDelaySec"] == ["10min"]
    assert d["WantedBy"] == ["timers.target"]


def test_env_example_holds_placeholders_only():
    text = (DEPLOY / "env.example").read_text()
    d = _directives(DEPLOY / "env.example")
    assert d["DATABASE_URL"] == ["'postgresql://...'"]
    assert d["RAINMAKER_TRADE_MODE"] == ["off"]
    assert d["RAINMAKER_HEALTHCHECK_URL"] == [""]
    assert d["RAINMAKER_ALERT_GH_TOKEN"] == [""]
    assert "RAINYDAY_SYNOPTIC_TOKEN" in text
    assert not re.search(r"^RAINYDAY_SYNOPTIC_TOKEN=", text, re.MULTILINE)
