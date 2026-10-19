"""Regression tests for the pytest DATABASE_URL refusal guard (#448).

The guard lives in tests/conftest.py: a pytest_configure hook that aborts the
run before collection (so no test module is even imported) when DATABASE_URL is
a Postgres DSN, plus the original session-scoped autouse fixture retained as
belt-and-braces. These tests exercise the guard the only honest way, by
invoking pytest as a subprocess: importing conftest.py in-process would trigger
the guard on this very suite (which must stay runnable with DATABASE_URL
unset).

Incident context (#445): a suite run inheriting a leaked DSN wrote 17 fixture
rows into PROD Supabase on 2026-10-19. The guard must trip before any test
executes or echoes the environment, so the subprocess assertions pin three
properties: the run refuses with exit code 3, zero tests executed, and the
refusal output never echoes the DSN or its password.
"""

import os
import subprocess
import sys

DSN_GUARD_MESSAGE = "REFUSING TO RUN TESTS: DATABASE_URL is a Postgres DSN"
GUARDED_DSN = "postgresql://guard-user:hunter2-topsecret@example.invalid/db"
GUARDED_PASSWORD = "hunter2-topsecret"


def _run_pytest(workdir_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Invoke pytest in a subprocess with a controlled environment."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k != "PYTEST_CURRENT_TEST"  # else nested pytest misparses its header
    }
    env.update(workdir_env)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--no-header",
            "-p",
            "no:cacheprovider",
            "tests/test_domain.py::test_domain_exports_bucket_kind",
            "-x",
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )


def test_postgres_dsn_aborts_suite_zero_tests_ran():
    result = _run_pytest({"DATABASE_URL": GUARDED_DSN})
    combined = result.stdout + result.stderr
    assert result.returncode == 3, combined
    assert DSN_GUARD_MESSAGE in combined
    # Refusal must precede execution entirely. pytest_exit during configure
    # emits no summary line at all, so pin zero execution negatively: no
    # report counters, no collection notice, no trace of the target test.
    lowered = combined.lower()
    assert "passed" not in lowered, combined
    assert "failed" not in lowered, combined
    assert "skipped" not in lowered, combined
    assert "collected" not in lowered, combined
    assert "test_domain_exports_bucket_kind" not in combined


def test_postgres_dsn_not_echoed_in_output():
    result = _run_pytest({"DATABASE_URL": GUARDED_DSN})
    combined = result.stdout + result.stderr
    assert GUARDED_DSN not in combined
    assert GUARDED_PASSWORD not in combined


def test_sqlite_database_url_is_allowed():
    result = _run_pytest({"DATABASE_URL": ""})
    assert result.returncode == 0, result.stdout + result.stderr


def test_absent_database_url_is_allowed():
    result = _run_pytest({})
    assert result.returncode == 0, result.stdout + result.stderr
