"""Tests for the Polymarket geoblock guard (polymarket.com/api/geoblock).

Fixture-tested only. Never hits the live endpoint. The fixtures are hand-made
and use documentation IPs (203.0.113.0/24).
"""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from rainmaker.polymarket.geoblock import (
    GEOBLOCK_URL,
    GeoblockedError,
    GeoblockStatus,
    fetch_geoblock,
    require_unblocked,
)

FIXTURES = Path(__file__).parent / "fixtures"

ALLOWED = "polymarket_geoblock_allowed.json"
BLOCKED = "polymarket_geoblock_blocked.json"


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def test_fetch_parses_allowed_fixture(httpx_mock):
    httpx_mock.add_response(url=GEOBLOCK_URL, json=_fixture(ALLOWED))
    with httpx.Client() as client:
        status = fetch_geoblock(client)
    assert status.blocked is False
    assert status.country == "DK"
    assert status.region == "84"
    assert status.ip == "203.0.113.7"


def test_fetch_parses_blocked_fixture(httpx_mock):
    httpx_mock.add_response(url=GEOBLOCK_URL, json=_fixture(BLOCKED))
    with httpx.Client() as client:
        status = fetch_geoblock(client)
    assert status == GeoblockStatus(blocked=True, ip="203.0.113.42", country="US", region="NY")


def test_fetch_makes_one_get_with_no_query_params(httpx_mock):
    httpx_mock.add_response(url=GEOBLOCK_URL, json=_fixture(ALLOWED))
    with httpx.Client() as client:
        fetch_geoblock(client)
    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert str(requests[0].url) == GEOBLOCK_URL
    assert requests[0].url.params == httpx.QueryParams()


def test_require_unblocked_returns_status_when_allowed(httpx_mock):
    httpx_mock.add_response(url=GEOBLOCK_URL, json=_fixture(ALLOWED))
    with httpx.Client() as client:
        status = require_unblocked(client)
    assert status.blocked is False
    assert status.country == "DK"


def test_require_unblocked_raises_when_blocked(httpx_mock):
    httpx_mock.add_response(url=GEOBLOCK_URL, json=_fixture(BLOCKED))
    with httpx.Client() as client, pytest.raises(GeoblockedError) as exc:
        require_unblocked(client)
    msg = str(exc.value)
    assert "US" in msg
    assert "NY" in msg
    assert "refusing to trade" in msg


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("boom"),
        httpx.ReadTimeout("slow"),
        httpx.RemoteProtocolError("bad framing"),
    ],
)
def test_transport_failure_fails_closed(httpx_mock, exc):
    httpx_mock.add_exception(exc, url=GEOBLOCK_URL)
    with httpx.Client() as client, pytest.raises(GeoblockedError) as caught:
        require_unblocked(client)
    assert "treating this host as blocked" in str(caught.value)


@pytest.mark.parametrize(
    ("status_code", "headers", "content"),
    [
        (302, {"Location": "https://polymarket.com/"}, b""),
        (403, {"content-type": "text/html"}, b"<html>Attention Required! Cloudflare</html>"),
        (404, {}, b"not found"),
        (429, {}, b"slow down"),
        (500, {}, b"oops"),
        (503, {}, b"unavailable"),
    ],
)
def test_non_2xx_fails_closed(httpx_mock, status_code, headers, content):
    httpx_mock.add_response(
        url=GEOBLOCK_URL, status_code=status_code, headers=headers, content=content
    )
    with httpx.Client() as client, pytest.raises(GeoblockedError):
        require_unblocked(client)


@pytest.mark.parametrize(
    "content",
    [b"<html>hello</html>", b"", b"[]", b"null", b'"ok"'],
)
def test_unparseable_200_body_fails_closed(httpx_mock, content):
    httpx_mock.add_response(url=GEOBLOCK_URL, status_code=200, content=content)
    with httpx.Client() as client, pytest.raises(GeoblockedError):
        require_unblocked(client)


def test_missing_blocked_fails_closed(httpx_mock):
    body = {"ip": "203.0.113.7", "country": "DK", "region": "84"}
    httpx_mock.add_response(url=GEOBLOCK_URL, json=body)
    with httpx.Client() as client, pytest.raises(GeoblockedError):
        require_unblocked(client)


# Lax bool coercion would turn "false", "no", 0 and "0" into False, which
# reads as "not blocked" and fails open. Anything but a JSON bool is blocked.
@pytest.mark.parametrize("value", ["false", "no", 0, "0", None, 1, "true"])
def test_non_bool_blocked_fails_closed(httpx_mock, value):
    body = {**_fixture(ALLOWED), "blocked": value}
    httpx_mock.add_response(url=GEOBLOCK_URL, json=body)
    with httpx.Client() as client, pytest.raises(GeoblockedError):
        require_unblocked(client)


def test_unknown_extra_field_is_ignored(httpx_mock):
    body = {**_fixture(ALLOWED), "newField": "x"}
    httpx_mock.add_response(url=GEOBLOCK_URL, json=body)
    with httpx.Client() as client:
        status = require_unblocked(client)
    assert status.blocked is False


def test_only_blocked_decides(httpx_mock):
    httpx_mock.add_response(url=GEOBLOCK_URL, json={"blocked": False})
    with httpx.Client() as client:
        status = require_unblocked(client)
    assert status == GeoblockStatus(blocked=False)
    assert status.ip is None
    assert status.country is None
    assert status.region is None
