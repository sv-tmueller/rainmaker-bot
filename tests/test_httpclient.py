import httpx
import pytest

from rainmaker.config import NWS_USER_AGENT
from rainmaker.httpclient import RetryTransport, build_client


class _StubTransport(httpx.BaseTransport):
    """Replays a scripted list of behaviors: raise an Exception or return a Response."""

    def __init__(self, behaviors: list[object]) -> None:
        self._behaviors = list(behaviors)
        self.calls = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        behavior = self._behaviors.pop(0)
        if isinstance(behavior, Exception):
            raise behavior
        assert isinstance(behavior, httpx.Response)
        return behavior


def _request() -> httpx.Request:
    return httpx.Request("GET", "https://example.test")


def test_retries_transient_error_then_returns_response():
    resp = httpx.Response(200)
    stub = _StubTransport([httpx.RemoteProtocolError("boom"), resp])
    sleeps: list[float] = []
    transport = RetryTransport(transport=stub, attempts=3, backoff=0.5, sleep=sleeps.append)

    out = transport.handle_request(_request())

    assert out is resp
    assert stub.calls == 2
    assert sleeps == [0.5]


def test_success_is_not_retried():
    resp = httpx.Response(200)
    stub = _StubTransport([resp])
    sleeps: list[float] = []
    transport = RetryTransport(transport=stub, attempts=3, backoff=0.5, sleep=sleeps.append)

    assert transport.handle_request(_request()) is resp
    assert stub.calls == 1
    assert sleeps == []


def test_gives_up_after_attempts_and_reraises_last():
    errors = [
        httpx.ReadError("first"),
        httpx.ReadError("second"),
        httpx.RemoteProtocolError("final"),
    ]
    stub = _StubTransport(list(errors))
    sleeps: list[float] = []
    transport = RetryTransport(transport=stub, attempts=3, backoff=0.5, sleep=sleeps.append)

    with pytest.raises(httpx.RemoteProtocolError, match="final"):
        transport.handle_request(_request())

    assert stub.calls == 3
    assert sleeps == [0.5, 1.0]  # exponential backoff before each retry, none after the last


def test_non_transport_error_is_not_retried():
    stub = _StubTransport([ValueError("not a network error")])
    transport = RetryTransport(transport=stub, attempts=3, backoff=0.0, sleep=lambda _s: None)

    with pytest.raises(ValueError):
        transport.handle_request(_request())

    assert stub.calls == 1


def test_build_client_uses_retry_transport_with_headers_and_timeout():
    client = build_client(42.0)
    try:
        assert isinstance(client._transport, RetryTransport)
        assert client.headers["User-Agent"] == NWS_USER_AGENT
        assert client.timeout.read == 42.0
    finally:
        client.close()
