"""HTTP client with retries for transient transport errors and 5xx statuses.

httpx's built-in HTTPTransport(retries=) only retries connection establishment,
not read-side failures like a server that disconnects without answering
(RemoteProtocolError), nor a request that completes but comes back with a
transient server error. A free weather or market endpoint dropping one request
or bouncing a 503 should not abort a scheduled run, so wrap the transport and
retry both the broader TransportError class and a fixed set of retryable
statuses, sharing one backoff budget. 429 is not retried by default: ASOS, wrh
and data-api run their own 429 loops, and retrying it here too would double
every wait. A host opts in through `rate_limit_domains` (OPEN_METEO_DOMAINS),
and then 429 is retried under the RATE_LIMIT_* caps.
"""

import time
from collections.abc import Callable

import httpx

from rainmaker.config import NWS_USER_AGENT

RETRY_ATTEMPTS = 4
RETRY_BACKOFF_S = 0.5
RETRYABLE_STATUS_CODES = frozenset({500, 502, 503, 504})

# Bounds for the batch backfill client (#449/#446): the backfill run iterates
# many (station, variable) cells inside a workflow timeout, so a hard-down
# source must fail fast per cell instead of chaining minute-scale retries.
BACKFILL_RETRY_ATTEMPTS = 2
BACKFILL_RETRY_BACKOFF_S = 0.25
BACKFILL_TIMEOUT_S = 10.0

# 429 handling for opted-in hosts (#481). Attempts are counted per request,
# separately from the 5xx/transport attempts. The budget is per transport
# instance (one CLI run) and never refills, so a hard limit costs at most
# RATE_LIMIT_BUDGET_S in total, not per call.
OPEN_METEO_DOMAINS = frozenset({"open-meteo.com"})
RATE_LIMIT_ATTEMPTS = 4
RATE_LIMIT_DEFAULT_WAIT_S = 10.0
RATE_LIMIT_MAX_WAIT_S = 60.0
RATE_LIMIT_BUDGET_S = 120.0


def _host_matches(host: str, domains: frozenset[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Retry-After as seconds, or None when absent or an HTTP-date."""
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


class RetryTransport(httpx.BaseTransport):
    """Retry transient transport errors and 5xx statuses with exponential backoff.

    With rate_limit_domains, also retry 429 from those hosts (see module docstring).
    """

    def __init__(
        self,
        transport: httpx.BaseTransport | None = None,
        attempts: int = RETRY_ATTEMPTS,
        backoff: float = RETRY_BACKOFF_S,
        sleep: Callable[[float], object] = time.sleep,
        rate_limit_domains: frozenset[str] = frozenset(),
    ) -> None:
        self._transport = transport or httpx.HTTPTransport()
        self._attempts = attempts
        self._backoff = backoff
        self._sleep = sleep
        self._rate_limit_domains = rate_limit_domains
        self._rate_limit_budget_left = RATE_LIMIT_BUDGET_S

    def _rate_limit_wait(self, response: httpx.Response, retries_done: int) -> float | None:
        """Seconds to wait before retrying a 429, or None to give up now."""
        if retries_done + 1 >= RATE_LIMIT_ATTEMPTS:
            return None
        wait = _retry_after_seconds(response)
        if wait is None:
            wait = RATE_LIMIT_DEFAULT_WAIT_S * 2**retries_done
        # Waiting less than Retry-After would only draw another 429, so a wait
        # we cannot afford means give up rather than shorten it.
        if wait > RATE_LIMIT_MAX_WAIT_S or wait > self._rate_limit_budget_left:
            return None
        return wait

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        rate_limited = _host_matches(request.url.host, self._rate_limit_domains)
        rate_limit_retries = 0
        attempt = 0
        while True:
            try:
                response = self._transport.handle_request(request)
            except httpx.TransportError:
                if attempt + 1 >= self._attempts:
                    raise
                self._sleep(self._backoff * 2**attempt)
                attempt += 1
                continue
            if rate_limited and response.status_code == 429:
                wait = self._rate_limit_wait(response, rate_limit_retries)
                if wait is None:
                    # Return the 429 unclosed so the caller can read the body.
                    return response
                response.close()
                self._rate_limit_budget_left -= wait
                rate_limit_retries += 1
                self._sleep(wait)
                continue
            if response.status_code not in RETRYABLE_STATUS_CODES:
                return response
            if attempt + 1 >= self._attempts:
                # Exhausted: return the 5xx as-is, unclosed. Every call site
                # already calls raise_for_status(), so it raises for free.
                return response
            # Discarding this response before the next attempt: close it so
            # the unread body doesn't leave the connection checked out of
            # httpcore's pool until GC gets around to it.
            response.close()
            self._sleep(self._backoff * 2**attempt)
            attempt += 1

    def close(self) -> None:
        self._transport.close()


def raise_for_status_with_body(response: httpx.Response, limit: int = 300) -> None:
    """raise_for_status() that puts the response body in the message.

    Open-Meteo's 429 body names which limit was hit (minutely, hourly, daily),
    which the stock message drops. Still an httpx.HTTPStatusError, so callers
    that catch httpx.HTTPError keep working.
    """
    if response.is_success:
        return
    body = " ".join(response.text.split())[:limit]
    raise httpx.HTTPStatusError(
        f"HTTP {response.status_code} for {response.request.url}: {body}",
        request=response.request,
        response=response,
    )


def build_client(
    timeout: float, *, rate_limit_domains: frozenset[str] = frozenset()
) -> httpx.Client:
    """An httpx client that retries transient transport errors, with our User-Agent.

    rate_limit_domains opts those host suffixes into 429 retry (off by default).
    """
    return httpx.Client(
        headers={"User-Agent": NWS_USER_AGENT},
        timeout=timeout,
        transport=RetryTransport(rate_limit_domains=rate_limit_domains),
    )


def build_backfill_client(
    timeout: float = BACKFILL_TIMEOUT_S,
    attempts: int = BACKFILL_RETRY_ATTEMPTS,
    backoff: float = BACKFILL_RETRY_BACKOFF_S,
) -> httpx.Client:
    """Like build_client, but tightly bounded for the batch backfill run.

    The backfill loop iterates many (station, variable) cells against several
    free sources, and the whole run sits inside a workflow timeout. During the
    #446 incident a hard-down NCEI turned each Kalshi-only station into a
    minute-scale retry chain and cancelled the run mid-refit. Two attempts and
    a short timeout keep one dead source to seconds per cell; the loop already
    treats a raised HTTPError as a per-cell skip, so the run completes partial
    and the summary line reports what failed. Open-Meteo 429s are retried
    within a capped budget (#481); other hosts keep their own handling.
    """
    return httpx.Client(
        headers={"User-Agent": NWS_USER_AGENT},
        timeout=timeout,
        transport=RetryTransport(
            attempts=attempts, backoff=backoff, rate_limit_domains=OPEN_METEO_DOMAINS
        ),
    )
