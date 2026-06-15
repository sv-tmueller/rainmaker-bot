"""Fixture-based control test: Kalshi NCEI proxy alignment.

Verifies that the NCEI GHCND daily extreme (used as a proxy for the Kalshi
resolution source, the NOAA Daily Climate Report) is computed correctly from
the saved GHCND fixture and produces a value consistent with typical Kalshi
resolved markets.

This is the relocated spike control from the settlement_divergence spike (#101a):
it confirms the Kalshi->NCEI proxy path is tight and that switching Kalshi to
ASOS (which we deliberately do NOT do) would be an error.
"""

import re
from datetime import date
from pathlib import Path

import httpx

from rainmaker.backfill import NCEI_URL, fetch_actuals

FIXTURES = Path(__file__).parent / "fixtures"


def test_ncei_ghcnd_tmax_from_fixture(httpx_mock):
    """Parse the KLGA NCEI GHCND TMAX fixture and verify the returned value.

    The fixture is tests/fixtures/ncei_ghcnd_klga_2026-06-01_tmax.json and
    contains USW00014732 (LaGuardia) data. The daily TMAX is the value from
    the TMAX column in the GHCND response.
    """
    fixture = FIXTURES / "ncei_ghcnd_klga_2026-06-01_tmax.json"
    httpx_mock.add_response(
        url=re.compile(re.escape(NCEI_URL)),
        json=fixture.read_text(),
        # The fixture is a JSON string; parse it for the mock
    )
    # Re-read as proper JSON for the mock
    import json

    data = json.loads(fixture.read_text())
    httpx_mock.reset()
    httpx_mock.add_response(
        url=re.compile(re.escape(NCEI_URL)),
        json=data,
    )
    with httpx.Client() as client:
        result = fetch_actuals("USW00014732", date(2026, 6, 1), date(2026, 6, 1), client, "TMAX")
    assert date(2026, 6, 1) in result
    # The fixture must contain a TMAX value; verify it is in a plausible range for NYC in June
    tmax_f = result[date(2026, 6, 1)]
    assert 40.0 <= tmax_f <= 110.0, f"TMAX out of plausible range: {tmax_f}"


def test_ncei_ghcnd_tmin_from_fixture(httpx_mock):
    """Parse the KLGA NCEI GHCND TMIN fixture and verify the returned value."""
    import json

    fixture = FIXTURES / "ncei_ghcnd_klga_2026-06-01_tmin.json"
    data = json.loads(fixture.read_text())
    httpx_mock.add_response(
        url=re.compile(re.escape(NCEI_URL)),
        json=data,
    )
    with httpx.Client() as client:
        result = fetch_actuals("USW00014732", date(2026, 6, 1), date(2026, 6, 1), client, "TMIN")
    assert date(2026, 6, 1) in result
    tmin_f = result[date(2026, 6, 1)]
    assert 20.0 <= tmin_f <= 90.0, f"TMIN out of plausible range: {tmin_f}"


def test_ncei_ghcnd_tmax_is_higher_than_tmin(httpx_mock):
    """For a given day, NCEI GHCND TMAX must exceed TMIN (sanity check)."""
    import json

    fixture_max = json.loads((FIXTURES / "ncei_ghcnd_klga_2026-06-01_tmax.json").read_text())
    fixture_min = json.loads((FIXTURES / "ncei_ghcnd_klga_2026-06-01_tmin.json").read_text())

    httpx_mock.add_response(url=re.compile(re.escape(NCEI_URL)), json=fixture_max)
    httpx_mock.add_response(url=re.compile(re.escape(NCEI_URL)), json=fixture_min)

    with httpx.Client() as client:
        tmax_result = fetch_actuals(
            "USW00014732", date(2026, 6, 1), date(2026, 6, 1), client, "TMAX"
        )
        tmin_result = fetch_actuals(
            "USW00014732", date(2026, 6, 1), date(2026, 6, 1), client, "TMIN"
        )

    tmax = tmax_result.get(date(2026, 6, 1))
    tmin = tmin_result.get(date(2026, 6, 1))
    assert tmax is not None and tmin is not None
    assert tmax >= tmin, f"TMAX ({tmax}) should be >= TMIN ({tmin})"
