"""Regression tests for the pytest DATABASE_URL refusal guard (#448).

The guard lives in tests/conftest.py: a session-scoped autouse fixture that
aborts the run before collecting/executing any test when DATABASE_URL is a
Postgres DSN. These tests exercise it the only honest way, by invoking pytest
as a subprocess: importing conftest.py in-process would trigger the guard on
this very suite (which must stay runnable with DATABASE_URL unset).

Incident context (#445): a suite run inheriting a leaked DSN wrote 17 fixture
rows into PROD Supabase on 2026-10-19. The guard must trip before any test
writes, so the subprocess assertions check the refusal surfaces at collection
time, ahead of any test execution.
"""

import os
import subprocess
import sys

DSN_GUARD_MESSAGE = "REFUSING TO RUN TESTS: DATABASE_URL is a Postgres DSN"


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


def test_postgres_dsn_aborts_suite_with_clear_message():
    result = _run_pytest({"DATABASE_URL": "postgresql://user:pw@example.invalid/db"})
    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert DSN_GUARD_MESSAGE in combined


def test_sqlite_database_url_is_allowed():
    result = _run_pytest({"DATABASE_URL": ""})
    assert result.returncode == 0, result.stdout + result.stderr


def test_absent_database_url_is_allowed():
    result = _run_pytest({})
    assert result.returncode == 0, result.stdout + result.stderr
