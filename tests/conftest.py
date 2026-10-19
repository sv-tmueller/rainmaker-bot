"""Shared pytest configuration for the rainmaker test suite.

Currently holds one thing: the DATABASE_URL refusal guard (#448, implements
#445). On 2026-10-19 a suite run inherited a leaked production DSN and wrote
17 fixture rows into PROD Supabase, silently overwriting live calibration
cells. Tests are SQLite-only by design (making them Postgres-compatible is a
non-goal), so a Postgres DATABASE_URL during pytest is always a mistake, and
the cheapest safe place to stop it is before any test can run.
"""

import os

import pytest

_DSN_GUARD_MESSAGE = (
    "\n"
    "REFUSING TO RUN TESTS: DATABASE_URL is a Postgres DSN "
    "(postgres:// or postgresql://).\n"
    "Tests write fixture rows; running them against a real database corrupts "
    "production data\n"
    "(see #445: 17 phantom rows written to PROD Supabase on 2026-10-19).\n"
    "Unset DATABASE_URL (or point it at a throwaway SQLite path) and re-run.\n"
)


@pytest.fixture(scope="session", autouse=True)
def refuse_postgres_database_url() -> None:
    """Abort the session before any test runs if DATABASE_URL is Postgres.

    Session-scoped and autouse, so it wraps every test regardless of what a
    given invocation selects. An empty-string DSN is treated as unset: some
    shells and CI wrappers export DATABASE_URL="" when no DSN is configured,
    and the CLI treats falsy the same way (cli._datastore falls back to the
    SQLite default).
    """
    dsn = os.environ.get("DATABASE_URL")
    if dsn and dsn.startswith(("postgres://", "postgresql://")):
        pytest.exit(_DSN_GUARD_MESSAGE, returncode=3)
