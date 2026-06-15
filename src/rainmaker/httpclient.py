"""HTTP client with retries for transient transport errors.

httpx's built-in HTTPTransport(retries=) only retries connection establishment,
not read-side failures like a server that disconnects without answering
(RemoteProtocolError). A free weather or market endpoint dropping one request
should not abort a scheduled run, so wrap the transport and retry the broader
TransportError class with backoff.
"""

import time
from collections.abc import Callable

import httpx

from rainmaker.config import NWS_USER_AGENT

RETRY_ATTEMPTS = 4
RETRY_BACKOFF_S = 0.5


class RetryTransport(httpx.BaseTransport):
    """Retry transient transport errors with exponential backoff, then re-raise."""

    def __init__(
        self,
        transport: httpx.BaseTransport | None = None,
        attempts: int = RETRY_ATTEMPTS,
        backoff: float = RETRY_BACKOFF_S,
        sleep: Callable[[float], object] = time.sleep,
    ) -> None:
        self._transport = transport or httpx.HTTPTransport()
        self._attempts = attempts
        self._backoff = backoff
        self._sleep = sleep

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        for attempt in range(self._attempts):
            try:
                return self._transport.handle_request(request)
            except httpx.TransportError:
                if attempt + 1 == self._attempts:
                    raise
                self._sleep(self._backoff * 2**attempt)
        raise AssertionError("unreachable")  # pragma: no cover

    def close(self) -> None:
        self._transport.close()


def build_client(timeout: float) -> httpx.Client:
    """An httpx client that retries transient transport errors, with our User-Agent."""
    return httpx.Client(
        headers={"User-Agent": NWS_USER_AGENT},
        timeout=timeout,
        transport=RetryTransport(),
    )
