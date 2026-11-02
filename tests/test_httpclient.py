import httpx
import pytest

from rainmaker.config import NWS_USER_AGENT
from rainmaker.httpclient import (
    OPEN_METEO_DOMAINS,
    RATE_LIMIT_ATTEMPTS,
    RATE_LIMIT_BUDGET_S,
    RATE_LIMIT_DEFAULT_WAIT_S,
    RATE_LIMIT_MAX_WAIT_S,
    RetryTransport,
    build_backfill_client,
    build_client,
    raise_for_status_with_body,
)


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
    streams = [_TrackingStream([b"error body"]) for _ in range(3)]
    stub = _StubTransport([httpx.Response(503, stream=stream) for stream in streams])
    sleeps: list[float] = []
    transport = RetryTransport(transport=stub, attempts=3, backoff=0.5, sleep=sleeps.append)
    request = _request()

    out = transport.handle_request(request)

    assert out.status_code == 503
    assert stub.calls == 3
    assert sleeps == [0.5, 1.0]  # exponential backoff before each retry, none after the last
    assert out.stream.closed is False
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


OPEN_METEO_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"


def _om_request(url: str = OPEN_METEO_URL) -> httpx.Request:
    return httpx.Request("GET", url)


def _rl_transport(
    behaviors: list[object], sleeps: list[float], **kwargs: object
) -> tuple[RetryTransport, _StubTransport]:
    stub = _StubTransport(behaviors)
    transport = RetryTransport(
        transport=stub,
        sleep=sleeps.append,
        rate_limit_domains=OPEN_METEO_DOMAINS,
        **kwargs,  # type: ignore[arg-type]
    )
    return transport, stub


def test_rate_limited_open_meteo_is_retried_with_default_wait():
    stream_a = _TrackingStream([b"slow down"])
    resp = httpx.Response(200)
    sleeps: list[float] = []
    transport, stub = _rl_transport([httpx.Response(429, stream=stream_a), resp], sleeps)

    out = transport.handle_request(_om_request())

    assert out is resp
    assert stub.calls == 2
    assert sleeps == [RATE_LIMIT_DEFAULT_WAIT_S]
    assert stream_a.closed is True


def test_rate_limit_wait_doubles_between_retries():
    sleeps: list[float] = []
    transport, _stub = _rl_transport(
        [httpx.Response(429), httpx.Response(429), httpx.Response(200)], sleeps
    )

    transport.handle_request(_om_request())

    assert sleeps == [RATE_LIMIT_DEFAULT_WAIT_S, 2 * RATE_LIMIT_DEFAULT_WAIT_S]


def test_retry_after_seconds_is_honored_exactly():
    sleeps: list[float] = []
    transport, _stub = _rl_transport(
        [httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200)], sleeps
    )

    transport.handle_request(_om_request())

    assert sleeps == [7.0]


def test_retry_after_over_max_wait_fails_fast_without_sleeping():
    stream = _TrackingStream([b"hourly limit"])
    sleeps: list[float] = []
    transport, stub = _rl_transport(
        [httpx.Response(429, headers={"Retry-After": "3600"}, stream=stream)], sleeps
    )

    out = transport.handle_request(_om_request())

    assert out.status_code == 429
    assert stub.calls == 1
    assert sleeps == []
    assert stream.closed is False


def test_persistent_rate_limit_returns_last_429_unclosed_after_attempts():
    streams = [_TrackingStream([b"x"]) for _ in range(RATE_LIMIT_ATTEMPTS)]
    sleeps: list[float] = []
    transport, stub = _rl_transport([httpx.Response(429, stream=s) for s in streams], sleeps)

    out = transport.handle_request(_om_request())

    assert out.status_code == 429
    assert stub.calls == RATE_LIMIT_ATTEMPTS
    assert len(sleeps) == RATE_LIMIT_ATTEMPTS - 1
    assert out.stream is streams[-1]
    assert streams[-1].closed is False
    assert all(s.closed for s in streams[:-1])


def test_rate_limit_budget_is_shared_and_never_refills():
    # Retry-After 50 each: the first request spends 100 of the 120s budget,
    # the second cannot afford another 50 and returns its 429 at once.
    sleeps: list[float] = []
    behaviors: list[object] = [httpx.Response(429, headers={"Retry-After": "50"})] * 5
    transport, stub = _rl_transport(behaviors, sleeps)

    transport.handle_request(_om_request())
    assert sum(sleeps) == 100.0
    calls_after_first = stub.calls

    out = transport.handle_request(_om_request())

    assert out.status_code == 429
    assert stub.calls == calls_after_first + 1
    assert sum(sleeps) <= RATE_LIMIT_BUDGET_S
    assert sum(sleeps) == 100.0


def test_wait_over_remaining_budget_fails_fast():
    assert RATE_LIMIT_MAX_WAIT_S < RATE_LIMIT_BUDGET_S
    sleeps: list[float] = []
    transport, stub = _rl_transport(
        [httpx.Response(429, headers={"Retry-After": "60"})] * 4, sleeps
    )

    transport.handle_request(_om_request())
    transport.handle_request(_om_request())

    # 60 + 60 = 120 spent on the first request's two retries; the second
    # request's first 429 finds the budget empty and does not sleep.
    assert sum(sleeps) == RATE_LIMIT_BUDGET_S
    assert stub.calls == 4


@pytest.mark.parametrize(
    "url",
    [
        "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py",
        "https://api.synopticdata.com/v2/stations/timeseries",
        "https://data-api.polymarket.com/trades",
        "https://notopen-meteo.com/v1/forecast",
    ],
)
def test_rate_limit_retry_skips_other_hosts(url: str):
    resp = httpx.Response(429)
    sleeps: list[float] = []
    transport, stub = _rl_transport([resp], sleeps)

    out = transport.handle_request(_om_request(url))

    assert out is resp
    assert stub.calls == 1
    assert sleeps == []


@pytest.mark.parametrize(
    "url",
    [
        "https://api.open-meteo.com/v1/forecast",
        "https://ensemble-api.open-meteo.com/v1/ensemble",
        "https://historical-forecast-api.open-meteo.com/v1/forecast",
        "https://previous-runs-api.open-meteo.com/v1/forecast",
    ],
)
def test_rate_limit_retry_covers_every_open_meteo_host(url: str):
    sleeps: list[float] = []
    transport, stub = _rl_transport([httpx.Response(429), httpx.Response(200)], sleeps)

    transport.handle_request(_om_request(url))

    assert stub.calls == 2


def test_429_then_503_then_200_succeeds_on_a_tight_attempt_budget():
    resp = httpx.Response(200)
    sleeps: list[float] = []
    transport, stub = _rl_transport(
        [httpx.Response(429), httpx.Response(503), resp], sleeps, attempts=2, backoff=0.25
    )

    out = transport.handle_request(_om_request())

    assert out is resp
    assert stub.calls == 3
    assert sleeps == [RATE_LIMIT_DEFAULT_WAIT_S, 0.25]


def test_build_client_has_rate_limit_retry_off_unless_asked():
    default = build_client(5.0)
    scoped = build_client(5.0, rate_limit_domains=OPEN_METEO_DOMAINS)
    try:
        assert default._transport._rate_limit_domains == frozenset()
        assert scoped._transport._rate_limit_domains == OPEN_METEO_DOMAINS
    finally:
        default.close()
        scoped.close()


def test_build_backfill_client_retries_open_meteo_rate_limits():
    client = build_backfill_client()
    try:
        assert client._transport._rate_limit_domains == OPEN_METEO_DOMAINS
        assert client._transport._attempts == 2
    finally:
        client.close()


def test_zero_attempts_still_terminates_on_transport_error():
    stub = _StubTransport([httpx.ReadError("only")])
    transport = RetryTransport(transport=stub, attempts=0, backoff=0.0, sleep=lambda _s: None)

    with pytest.raises(httpx.ReadError):
        transport.handle_request(_request())

    assert stub.calls == 1


def test_zero_attempts_still_terminates_on_retryable_status():
    resp = httpx.Response(503, request=_request())
    stub = _StubTransport([resp])
    transport = RetryTransport(transport=stub, attempts=0, backoff=0.0, sleep=lambda _s: None)

    assert transport.handle_request(_request()) is resp
    assert stub.calls == 1


@pytest.mark.parametrize("status", [301, 302, 304, 404, 429, 503])
def test_raise_for_status_with_body_raises_on_anything_not_2xx(status: int):
    resp = httpx.Response(status, text="limit hit", request=_request())

    with pytest.raises(httpx.HTTPStatusError, match=f"HTTP {status}"):
        raise_for_status_with_body(resp)


def test_raise_for_status_with_body_passes_2xx_and_includes_body_on_error():
    raise_for_status_with_body(httpx.Response(200, request=_request()))

    resp = httpx.Response(429, text="Hourly  limit\nexceeded", request=_request())
    with pytest.raises(httpx.HTTPStatusError, match="Hourly limit exceeded"):
        raise_for_status_with_body(resp)
