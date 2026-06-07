# 96 - Retry transient HTTP errors

## Context

The 2026-06-07 09:21 UTC scheduled run failed. The `run` step succeeded; the
`settle` step crashed on its first NCEI call with
`httpx.RemoteProtocolError: Server disconnected without sending a response`.
NOAA/NCEI dropped the connection mid-request, a transient network blip, not a
data-freshness event and not bad data. Settle has no retry, so one dropped
connection aborts the whole step, fails the job, skips prune + snapshot, and
sends a failure email. The same blip can hit any external call in the `run`
step (NWS, Open-Meteo, Polymarket), which is the more important path.

`httpx.HTTPTransport(retries=)` is not enough: it only retries connection
establishment (`ConnectError`/`ConnectTimeout`), not the read-side
`RemoteProtocolError`. We need our own retry over the transient
`httpx.TransportError` class.

## Approach

Add a retrying transport and route every client through it.

New module `src/rainmaker/httpclient.py`:

- `RetryTransport(httpx.BaseTransport)` wraps an `httpx.HTTPTransport` and, in
  `handle_request`, retries on `httpx.TransportError` with exponential backoff,
  re-raising the last exception after the final attempt. Delegates `close()` to
  the wrapped transport. Sleep is injectable for tests.
- `build_client(timeout)` returns
  `httpx.Client(headers={"User-Agent": NWS_USER_AGENT}, timeout=timeout, transport=RetryTransport())`.
- Constants: `RETRY_ATTEMPTS = 4`, `RETRY_BACKOFF_S = 0.5` (waits 0.5s, 1s, 2s).

`cli.py`: import `build_client`; replace all five
`httpx.Client(headers=..., timeout=...)` constructions (one at timeout 30.0, four
at 60.0) with `build_client(timeout)`. Keep the `httpx` import (used for type
hints and `httpx.HTTPError`).

## Files

- `src/rainmaker/httpclient.py` (new)
- `src/rainmaker/cli.py` (5 client sites -> `build_client`)
- `tests/test_httpclient.py` (new)
- `tests/test_cli.py` (move the client seam: the ~8
  `monkeypatch.setattr(cli.httpx, "Client", lambda **kw: _DummyClient())` lines
  become `monkeypatch.setattr(cli, "build_client", lambda *a, **k: _DummyClient())`)

The settle/openmeteo/golden tests build their own `httpx.Client()` directly and
are unchanged.

## Tests (TDD, write first)

Unit, against a stub wrapped transport with an injected no-op sleep:

1. Retries on `TransportError`, then returns the response from a later attempt.
2. Gives up after `RETRY_ATTEMPTS` and re-raises the last exception.
3. A successful first call is not retried (wrapped transport called once).
4. A non-`TransportError` propagates immediately without retry.
5. `build_client(timeout)` returns a client whose transport is a `RetryTransport`
   and carries the User-Agent header and timeout.

## Verification

- `uv run pytest` (full suite incl. golden e2e) green.
- `uv run ruff check . && uv run ruff format --check .`
- `uv run mypy src`
- Sanity: confirm `build_client` is the only client constructor cli uses
  (`grep -n "httpx.Client(" src/rainmaker/cli.py` returns nothing).
