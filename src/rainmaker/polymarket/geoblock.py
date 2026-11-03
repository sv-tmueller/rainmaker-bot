"""Read-only Polymarket geoblock check and a fail-closed guard for trading.

Polymarket enforces geographic restrictions per IP, including on order
endpoints. The trade path must refuse to act when Polymarket reports the host
as blocked. Confirmed against https://docs.polymarket.com/api-reference/geoblock
(2026-11-02):

- `GET https://polymarket.com/api/geoblock`, no auth, no query params. It lives
  on polymarket.com, not the API servers.
- Response: `{"blocked": bool, "ip": str, "country": str, "region": str}`.
  `country` is ISO 3166-1 alpha-2, `region` a bare subdivision code.
- The answer is computed per request (`cache-control: no-store`), and it
  describes the caller's own egress IP only. It says nothing about any other
  host.

Whether every close-only region (can close, cannot open) reports `blocked: true`
is unconfirmed. The guard trusts `blocked` and hard-codes no country list,
because the endpoint is what Polymarket enforces and its published lists change
without notice.

Only `blocked` decides. `ip`, `country` and `region` are diagnostics, so a host
with no region data is not false-blocked. Tested against saved fixtures only,
never a live endpoint.
"""

import httpx
from pydantic import BaseModel, ConfigDict, StrictBool

GEOBLOCK_URL = "https://polymarket.com/api/geoblock"


class GeoblockStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    # StrictBool is load-bearing: a lax bool would coerce "false" or 0 to False
    # and fail open.
    blocked: StrictBool
    ip: str | None = None
    country: str | None = None
    region: str | None = None


class GeoblockedError(RuntimeError):
    """The host is blocked, or the check could not prove it is not."""


def fetch_geoblock(client: httpx.Client) -> GeoblockStatus:
    """The geoblock status of the caller's IP. Raises on any failure.

    Redirects are never followed, even on a client built with
    `follow_redirects=True`, so a 3xx raises and fails closed.
    """
    resp = client.get(GEOBLOCK_URL, follow_redirects=False)
    resp.raise_for_status()
    return GeoblockStatus.model_validate(resp.json())


def require_unblocked(client: httpx.Client) -> GeoblockStatus:
    """Return the status when allowed, else raise GeoblockedError.

    Transport errors, non-2xx responses, unparseable bodies and a missing or
    non-bool `blocked` all count as blocked.
    """
    try:
        status = fetch_geoblock(client)
    except (httpx.HTTPError, ValueError) as exc:
        raise GeoblockedError(
            f"Polymarket geoblock check failed, treating this host as blocked: {exc}"
        ) from exc
    if status.blocked:
        raise GeoblockedError(
            "Polymarket reports this host as blocked "
            f"(country={status.country or 'unknown'}, "
            f"region={status.region or 'unknown'}, "
            f"ip={status.ip or 'unknown'}), refusing to trade"
        )
    return status
