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


def _database_url_is_postgres(dsn: str | None) -> bool:
    """An empty-string DSN is treated as unset: some shells and CI wrappers
    export DATABASE_URL="" when no DSN is configured, and the CLI treats
    falsy the same way (cli._datastore falls back to the SQLite default).
    """
    return bool(dsn) and dsn.startswith(("postgres://", "postgresql://"))


def pytest_configure(config: pytest.Config) -> None:
    """Abort the run before collection if DATABASE_URL is a Postgres DSN.

    Fires before any test module is imported, closing the window a
    session-scoped autouse fixture leaves open (module-level code in a test
    module would already have run by the time the fixture executes).
    """
    if _database_url_is_postgres(os.environ.get("DATABASE_URL")):
        pytest.exit(_DSN_GUARD_MESSAGE, returncode=3)


@pytest.fixture(scope="session", autouse=True)
def refuse_postgres_database_url() -> None:
    """Belt-and-braces backstop for the pytest_configure hook above.

    Covers exotic invocations that skip conftest-level configure hooks
    (e.g. a future plugin reorder); harmless duplication when the hook
    already fired.
    """
    if _database_url_is_postgres(os.environ.get("DATABASE_URL")):
        pytest.exit(_DSN_GUARD_MESSAGE, returncode=3)
