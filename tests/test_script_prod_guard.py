"""Tests for the ad hoc analysis scripts' prod-read guard (#469).

Ad hoc scripts under scripts/ used to read whatever DATABASE_URL pointed at,
so a hand run spent prod Supabase egress by default. They now refuse a Postgres
DSN unless --allow-prod is passed. The DSN below is fake (example.invalid never
resolves); no test here may reach a real database.
"""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from rainmaker.store.db import refuse_prod_dsn

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
FAKE_PASSWORD = "hunter2-topsecret"
FAKE_DSN = f"postgresql://guard-user:{FAKE_PASSWORD}@example.invalid/db"
GUARD_MESSAGE = "refusing to read a Postgres DSN without --allow-prod"


class _Connected(Exception):
    """Raised by the connect stub so a script stops right after the guard."""


# --- helper -----------------------------------------------------------------


@pytest.mark.parametrize("scheme", ["postgres", "postgresql"])
def test_helper_refuses_postgres_without_flag(scheme: str) -> None:
    dsn = f"{scheme}://u:{FAKE_PASSWORD}@example.invalid/db"
    with pytest.raises(SystemExit) as exc:
        refuse_prod_dsn(dsn, allow_prod=False)
    message = str(exc.value)
    assert GUARD_MESSAGE in message
    assert "--allow-prod" in message
    assert FAKE_PASSWORD not in message
    assert "example.invalid" not in message


def test_helper_passes_postgres_with_flag() -> None:
    refuse_prod_dsn(FAKE_DSN, allow_prod=True)


@pytest.mark.parametrize("dsn", ["rainmaker.db", ":memory:", "/tmp/local.sqlite"])
def test_helper_passes_sqlite_without_flag(dsn: str) -> None:
    refuse_prod_dsn(dsn, allow_prod=False)


# --- the five scripts -------------------------------------------------------

SCRIPT_NAMES = [
    "sigma_statistic_gap",
    "refit_v2_replay",
    "regime_refit_v3_dryrun",
    "regime_refit_v3_preflip",
    "tmax_bias_audit",
]


def _load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"_guard_test_{name}", SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_main(
    monkeypatch: pytest.MonkeyPatch, name: str, dsn: str, flags: list[str]
) -> tuple[list[str], BaseException | None]:
    """Run a script's main() with connect stubbed; return (connect DSNs, raised)."""
    module = _load_script(name)
    seen: list[str] = []

    def fake_connect(received: str, **_kw: object) -> None:
        seen.append(received)
        raise _Connected

    monkeypatch.setattr(module, "connect", fake_connect)
    monkeypatch.setenv("DATABASE_URL", dsn)
    monkeypatch.setattr(sys, "argv", [f"{name}.py", *flags])
    try:
        module.main()
    except (SystemExit, _Connected) as exc:
        return seen, exc
    return seen, None


@pytest.mark.parametrize("name", SCRIPT_NAMES)
def test_script_refuses_postgres_dsn_without_flag(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    seen, raised = _run_main(monkeypatch, name, FAKE_DSN, [])
    assert isinstance(raised, SystemExit)
    assert GUARD_MESSAGE in str(raised)
    assert FAKE_PASSWORD not in str(raised)
    assert seen == []


@pytest.mark.parametrize("name", SCRIPT_NAMES)
def test_script_connects_to_postgres_with_flag(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    seen, raised = _run_main(monkeypatch, name, FAKE_DSN, ["--allow-prod"])
    assert isinstance(raised, _Connected)
    assert seen == [FAKE_DSN]


@pytest.mark.parametrize("name", SCRIPT_NAMES)
def test_script_connects_to_sqlite_without_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str
) -> None:
    local = str(tmp_path / "local.db")
    seen, raised = _run_main(monkeypatch, name, local, [])
    assert isinstance(raised, _Connected)
    assert seen == [local]


def test_dryrun_subprocess_refuses_without_leaking_dsn() -> None:
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    env["DATABASE_URL"] = FAKE_DSN
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "regime_refit_v3_dryrun.py")],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode != 0
    assert GUARD_MESSAGE in proc.stderr
    assert proc.stdout == ""
    assert FAKE_PASSWORD not in proc.stdout + proc.stderr
