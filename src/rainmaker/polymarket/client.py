import sys
from typing import Any, cast

import httpx

from rainmaker.config import (
    INTL_STATIONS,
    POLYMARKET_TAKER_FEE_RATE,
    PRECIP_STATIONS,
    STATIONS,
)
from rainmaker.domain import Market, PrecipMonthlyMarket
from rainmaker.polymarket.markets import parse_city, parse_market
from rainmaker.polymarket.precip_markets import (
    parse_precip_city,
    parse_precip_event,
)

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"


def _page_events(
    client: httpx.Client, params: dict[str, str], *, page_size: int, max_pages: int
) -> list[dict[str, Any]]:
    """Page through Gamma events with the given filter. Raises on any HTTP error."""
    events: list[dict[str, Any]] = []
    for page in range(max_pages):
        resp = client.get(
            GAMMA_EVENTS_URL,
            params={**params, "limit": str(page_size), "offset": str(page * page_size)},
        )
        resp.raise_for_status()
        batch = cast(list[dict[str, Any]], resp.json())
        events.extend(batch)
        if len(batch) < page_size:
            break
    return events


def fetch_weather_events(
    client: httpx.Client, *, page_size: int = 100, max_pages: int = 6
) -> list[dict[str, Any]]:
    """Page through Gamma's active weather events. Raises on any HTTP error."""
    return _page_events(
        client,
        {"closed": "false", "active": "true", "tag_slug": "weather"},
        page_size=page_size,
        max_pages=max_pages,
    )


def fetch_closed_weather_events(
    client: httpx.Client, *, page_size: int = 100, max_pages: int = 12
) -> list[dict[str, Any]]:
    """Page through Gamma's closed weather events, most recent first, for backtesting.

    Ordered by end date descending so a bounded page budget reaches the recent
    window the reality check cares about. Raises on any HTTP error.
    """
    return _page_events(
        client,
        {"closed": "true", "tag_slug": "weather", "order": "endDate", "ascending": "false"},
        page_size=page_size,
        max_pages=max_pages,
    )


_ALL_TEMP_STATIONS = {**STATIONS, **INTL_STATIONS}


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _bucket_fee_problem(bucket: dict[str, Any]) -> str | None:
    """Why one bucket's fee block breaks the gate's fee assumption, else None.

    A disabled fee (feesEnabled False) passes: the true fee is then 0, at or
    below the fee the gate subtracts, so net edge is understated, never overstated.
    """
    enabled = bucket.get("feesEnabled")
    if enabled is False:
        return None
    if enabled is None:
        return "fee block is missing (no feesEnabled)"
    if enabled is not True:
        return f"feesEnabled is {enabled!r}, not a boolean"
    schedule = bucket.get("feeSchedule")
    if not isinstance(schedule, dict):
        return "feeSchedule is missing"
    rate = schedule.get("rate")
    if not _is_number(rate) or rate != POLYMARKET_TAKER_FEE_RATE:
        return f"feeSchedule rate is {rate!r}, assumed {POLYMARKET_TAKER_FEE_RATE}"
    exponent = schedule.get("exponent")
    if not _is_number(exponent) or exponent != 1:
        return f"feeSchedule exponent is {exponent!r}, assumed 1"
    if schedule.get("takerOnly") is not True:
        return f"feeSchedule takerOnly is {schedule.get('takerOnly')!r}, assumed True"
    return None


def fee_schedule_exclusion(event: dict[str, Any]) -> str | None:
    """Reason to bar a Polymarket event from recommendations, else None (#504).

    The fee-aware gate (#491) assumes fee = POLYMARKET_TAKER_FEE_RATE * p * (1 - p)
    on taker fills. Gamma carries the schedule on each bucket's market object. Any
    bucket whose schedule differs, or has none, excludes the whole market: a
    missing block means the Gamma schema drifted, and letting it pass would switch
    the guard off. Never raises, so a malformed event cannot abort discovery.
    """
    markets = event.get("markets") if isinstance(event, dict) else None
    if not isinstance(markets, list) or not markets:
        return "fee schedule unverifiable: event has no market objects"
    failures: list[tuple[str, str]] = []
    for item in markets:
        if not isinstance(item, dict):
            failures.append(("?", "market object is not a dict"))
            continue
        problem = _bucket_fee_problem(item)
        if problem is not None:
            name = item.get("groupItemTitle") or item.get("question") or item.get("id") or "?"
            failures.append((str(name), problem))
    if not failures:
        return None
    name, problem = failures[0]
    return (
        f"Polymarket fee schedule does not match the gate's assumed "
        f"{POLYMARKET_TAKER_FEE_RATE} x p x (1 - p) taker fee: bucket {name!r} "
        f"{problem} ({len(failures)} of {len(markets)} buckets fail)"
    )


def _is_known_temp_event(event: dict[str, Any]) -> bool:
    try:
        city = parse_city(event.get("title", ""))
    except ValueError:
        return False
    return city in _ALL_TEMP_STATIONS


def discover_markets(events: list[dict[str, Any]]) -> list[Market]:
    """Parse temperature markets (US + intl Celsius) from pre-fetched weather events.

    A market that fails to parse (for example its description does not name the
    resolution station) is skipped with a warning so one bad market does not
    abort the whole run. Polymarket being down still aborts upstream.
    """
    markets: list[Market] = []
    for ev in events:
        if not _is_known_temp_event(ev):
            continue
        try:
            market = parse_market(ev)
        except (ValueError, KeyError) as exc:
            print(f"skipping market {ev.get('id')}: {exc}", file=sys.stderr)
            continue
        fee_exclusion = fee_schedule_exclusion(ev)
        if fee_exclusion is not None:
            print(f"fee guard: market {market.id} excluded: {fee_exclusion}", file=sys.stderr)
            market = market.model_copy(update={"fee_exclusion": fee_exclusion})
        markets.append(market)
    return markets


def _is_us_precip_event(event: dict[str, Any]) -> bool:
    try:
        city = parse_precip_city(event.get("title", ""))
    except ValueError:
        return False
    return city in PRECIP_STATIONS


def discover_precip_markets(events: list[dict[str, Any]]) -> list[PrecipMonthlyMarket]:
    """Parse the US monthly precipitation markets from pre-fetched weather events.

    The parallel of discover_markets for the precip path: a market that fails to
    parse (for example its description does not name the resolution station) is
    skipped with a warning so one bad market does not abort the whole run.
    Polymarket being down still aborts upstream.
    """
    markets: list[PrecipMonthlyMarket] = []
    for ev in events:
        if not _is_us_precip_event(ev):
            continue
        try:
            market = parse_precip_event(ev)
        except (ValueError, KeyError) as exc:
            print(f"skipping precip market {ev.get('id')}: {exc}", file=sys.stderr)
            continue
        fee_exclusion = fee_schedule_exclusion(ev)
        if fee_exclusion is not None:
            print(
                f"fee guard: precip market {market.id} excluded: {fee_exclusion}", file=sys.stderr
            )
            market = market.model_copy(update={"fee_exclusion": fee_exclusion})
        markets.append(market)
    return markets
