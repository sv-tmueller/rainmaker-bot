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


class _TrackingStream(httpx.SyncByteStream):
    """An unread byte stream that records whether close() was called on it."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.closed = False

    def __iter__(self):
        yield from self._chunks

    def close(self) -> None:
        self.closed = True


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


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_retries_retryable_status_then_returns_success(status: int):
    resp = httpx.Response(200)
    stub = _StubTransport([httpx.Response(status), resp])
    sleeps: list[float] = []
    transport = RetryTransport(transport=stub, attempts=3, backoff=0.5, sleep=sleeps.append)

    out = transport.handle_request(_request())

    assert out is resp
    assert stub.calls == 2
    assert sleeps == [0.5]


def test_gives_up_after_attempts_and_returns_last_5xx_unraised():
    stub = _StubTransport([httpx.Response(503), httpx.Response(503), httpx.Response(503)])
    sleeps: list[float] = []
    transport = RetryTransport(transport=stub, attempts=3, backoff=0.5, sleep=sleeps.append)
    request = _request()

    out = transport.handle_request(request)

    assert out.status_code == 503
    assert stub.calls == 3
    assert sleeps == [0.5, 1.0]  # exponential backoff before each retry, none after the last
    # Client.send normally attaches the request after the transport returns;
    # do the same here so raise_for_status() has what it needs.
    out.request = request
    with pytest.raises(httpx.HTTPStatusError):
        out.raise_for_status()


def test_non_retryable_status_is_returned_on_first_attempt():
    resp = httpx.Response(404)
    stub = _StubTransport([resp])
    sleeps: list[float] = []
    transport = RetryTransport(transport=stub, attempts=3, backoff=0.5, sleep=sleeps.append)

    out = transport.handle_request(_request())

    assert out is resp
    assert stub.calls == 1
    assert sleeps == []


def test_discarded_retryable_response_is_closed_but_returned_one_is_not():
    stream_a = _TrackingStream([b"error body"])
    stream_b = _TrackingStream([b"ok body"])
    stub = _StubTransport(
        [
            httpx.Response(503, stream=stream_a),
            httpx.Response(200, stream=stream_b),
        ]
    )
    transport = RetryTransport(transport=stub, attempts=3, backoff=0.0, sleep=lambda _s: None)

    out = transport.handle_request(_request())

    assert out.status_code == 200
    assert stream_a.closed is True
    assert stream_b.closed is False


def test_429_is_not_retried_here_asos_owns_it():
    resp = httpx.Response(429)
    stub = _StubTransport([resp])
    sleeps: list[float] = []
    transport = RetryTransport(transport=stub, attempts=3, backoff=0.5, sleep=sleeps.append)

    out = transport.handle_request(_request())

    assert out is resp
    assert stub.calls == 1
    assert sleeps == []
