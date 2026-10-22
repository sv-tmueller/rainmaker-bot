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
