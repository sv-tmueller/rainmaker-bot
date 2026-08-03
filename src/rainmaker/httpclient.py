"""HTTP client with retries for transient transport errors and 5xx statuses.

httpx's built-in HTTPTransport(retries=) only retries connection establishment,
not read-side failures like a server that disconnects without answering
(RemoteProtocolError), nor a request that completes but comes back with a
transient server error. A free weather or market endpoint dropping one request
or bouncing a 503 should not abort a scheduled run, so wrap the transport and
retry both the broader TransportError class and a fixed set of retryable
statuses, sharing one backoff budget. 429 is deliberately excluded: ASOS
already owns rate-limit backoff via its own Retry-After loop, and retrying it
here too would double every wait.
"""

import time
from collections.abc import Callable

import httpx

from rainmaker.config import NWS_USER_AGENT

RETRY_ATTEMPTS = 4
RETRY_BACKOFF_S = 0.5
RETRYABLE_STATUS_CODES = frozenset({500, 502, 503, 504})


class RetryTransport(httpx.BaseTransport):
    """Retry transient transport errors and 5xx statuses with exponential backoff."""

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
                response = self._transport.handle_request(request)
            except httpx.TransportError:
                if attempt + 1 == self._attempts:
                    raise
                self._sleep(self._backoff * 2**attempt)
                continue
            if response.status_code not in RETRYABLE_STATUS_CODES:
                return response
            if attempt + 1 == self._attempts:
                # Exhausted: return the 5xx as-is, unclosed. Every call site
                # already calls raise_for_status(), so it raises for free.
                return response
            # Discarding this response before the next attempt: close it so
            # the unread body doesn't leave the connection checked out of
            # httpcore's pool until GC gets around to it.
            response.close()
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
