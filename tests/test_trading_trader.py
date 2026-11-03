"""The paper trader, end to end on a temp SQLite store and saved CLOB books.

Runs are seeded through `record_run` (hand-built reports, so the stored
predictions and prices rows have the real shape). Books and the geoblock
endpoint come from an `httpx.MockTransport`, so an unexpected request fails the
test and every request is recorded for the "no network" assertions.
"""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from rainmaker.config import (
    MIN_EDGE,
    POLYMARKET_TAKER_FEE_RATE,
    STATIONS,
    Target,
)
from rainmaker.domain import Bucket, Market, PrecipBracket, PrecipMonthlyMarket
from rainmaker.fees import taker_fee_per_share
from rainmaker.forecasts.base import ForecastSet
from rainmaker.polymarket.book import CLOB_BOOK_URL, OrderBook, fetch_book
from rainmaker.polymarket.geoblock import GEOBLOCK_URL
from rainmaker.polymarket.precip_markets import parse_precip_event
from rainmaker.ranking.edge import MarketReport, RankedOutcome
from rainmaker.store.db import connect, init_schema
from rainmaker.store.query import latest_ok_run, recommended_polymarket_outcomes
from rainmaker.store.record import record_paper_order, record_run
from rainmaker.trading.pricing import limit_price
from rainmaker.trading.trader import (
    TradeSummary,
    check_geoblock,
    gate_floor,
    render_trade_summary,
    run_paper_trade,
    trade_mode,
)

FIXTURES = Path(__file__).parent / "fixtures"
RATE = POLYMARKET_TAKER_FEE_RATE
NOW = datetime(2026, 11, 3, 18, 0, tzinfo=UTC)
RUN_STARTED = NOW - timedelta(minutes=7)
GEO_OK = "allowed (country DK, region 84)"

DEEP = "clob_book_trade_deep.json"
THIN = "clob_book_trade_thin.json"
EMPTY = "clob_book_empty.json"


class Clob:
    """A MockTransport handler: books by token id, plus the geoblock endpoint."""

    def __init__(self) -> None:
        self.books: dict[str, str | Exception | httpx.Response | dict[str, Any]] = {}
        self.geoblock: str | Exception = "polymarket_geoblock_allowed.json"
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if str(request.url) == GEOBLOCK_URL:
            if isinstance(self.geoblock, Exception):
                raise self.geoblock
            return httpx.Response(200, json=_fixture(self.geoblock), request=request)
        assert str(request.url).startswith(CLOB_BOOK_URL), f"unexpected request {request.url}"
        token = request.url.params["token_id"]
        assert token in self.books, f"unexpected book fetch for {token}"
        behavior = self.books[token]
        if isinstance(behavior, Exception):
            raise behavior
        if isinstance(behavior, httpx.Response):
            behavior.request = request
            return behavior
        data = _fixture(behavior) if isinstance(behavior, str) else dict(behavior)
        if isinstance(behavior, str):
            data["asset_id"] = token
        return httpx.Response(200, json=data, request=request)

    def fetched_tokens(self) -> list[str]:
        return [r.url.params["token_id"] for r in self.requests if "token_id" in r.url.params]


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def clob():
    return Clob()


@pytest.fixture
def client(clob):
    c = httpx.Client(transport=httpx.MockTransport(clob))
    yield c
    c.close()


@pytest.fixture
def conn():
    c = connect(":memory:")
    init_schema(c)
    yield c
    c.close()


def _bucket(label: str, lo: int, ask: float, no_ask: float, *, token: str) -> Bucket:
    return Bucket(
        label=label,
        kind="range",
        lo=lo,
        hi=lo + 1,
        threshold=None,
        yes_token_id=f"yes-{token}",
        best_ask=ask,
        best_bid=1 - no_ask,
        yes_price=ask,
        no_token_id=f"no-{token}",
        no_ask=no_ask,
    )


def _market(
    market_id: str,
    buckets: list[Bucket],
    *,
    city: str = "NYC",
    variable: str = "TMAX",
    venue: str = "polymarket",
) -> Market:
    return Market(
        id=market_id,
        slug=f"market-{market_id}",
        title=f"Highest temperature in {city} on Nov 5?",
        target=Target(
            station=STATIONS[city],
            variable=variable,  # type: ignore[arg-type]
            local_date=date(2026, 11, 5),
        ),
        buckets=buckets,
        venue=venue,
    )


def _outcome(
    label: str, side: str, p_win: float, ask: float, *, recommended: bool = True
) -> RankedOutcome:
    edge = p_win - ask
    return RankedOutcome(
        bucket_label=label,
        side=side,  # type: ignore[arg-type]
        p_win=p_win,
        best_ask=ask,
        edge=edge,
        net_edge=edge - taker_fee_per_share(ask, RATE),
        recommended=recommended,
    )


def _report(market: Market, outcomes: list[RankedOutcome]) -> MarketReport:
    return MarketReport(
        market_id=market.id,
        title=market.title,
        city=market.target.station.city,
        station=market.target.station.icao,
        variable=market.target.variable,
        settlement_date=market.target.local_date,
        mu=70.0,
        sigma=2.0,
        n_sources=3,
        calibrated="full",
        coverage=[],
        outcomes=outcomes,
        excluded_no_ask=[],
        venue=market.venue,
    )


def seed(
    conn,
    markets: list[tuple[Market, list[RankedOutcome]]],
    *,
    run_id: str = "run-1",
    started_at: datetime = RUN_STARTED,
    status: str = "ok",
) -> None:
    evaluated = [
        (m, ForecastSet(target=m.target, samples=[], coverage=[]), _report(m, outs))
        for m, outs in markets
    ]
    record_run(
        conn,
        run_id=run_id,
        started_at=started_at.isoformat(),
        finished_at=(started_at + timedelta(minutes=2)).isoformat(),
        status=status,
        evaluated=evaluated,
    )


def seed_precip(conn, market: PrecipMonthlyMarket, outcomes: list[RankedOutcome]) -> None:
    report = _report_precip(market, outcomes)
    record_run(
        conn,
        run_id="run-1",
        started_at=RUN_STARTED.isoformat(),
        finished_at=(RUN_STARTED + timedelta(minutes=2)).isoformat(),
        status="ok",
        evaluated=[],
        precip_evaluated=[(market, report)],
    )


def _report_precip(market: PrecipMonthlyMarket, outcomes: list[RankedOutcome]) -> MarketReport:
    return MarketReport(
        market_id=market.id,
        title=market.title,
        city=market.target.station.city,
        station=market.target.station.resolution_name,
        variable="PRCP",
        settlement_date=market.target.settlement_date,
        mu=None,
        sigma=None,
        n_sources=3,
        calibrated="uncalibrated",
        coverage=[],
        outcomes=outcomes,
        excluded_no_ask=[],
    )


def seed_open_position(
    conn, market_id: str, usdc: float, *, created_at: datetime, outcome: str = "00-01°F"
) -> None:
    """An already-filled paper order on an unsettled market of an older run."""
    conn.execute(
        "INSERT OR IGNORE INTO runs (id, started_at, status) VALUES (?, ?, 'ok')",
        ("run-old", (NOW - timedelta(days=3)).isoformat()),
    )
    conn.execute(
        "INSERT OR IGNORE INTO markets (id, city, variable, settlement_date) "
        "VALUES (?, 'NYC', 'TMAX', '2026-11-05')",
        (market_id,),
    )
    conn.commit()
    record_paper_order(
        conn,
        run_id="run-old",
        market_id=market_id,
        outcome=outcome,
        side="YES",
        token_id="tok-old",
        status="filled",
        stake_usdc=usdc,
        fill_price=0.5,
        shares=usdc / 0.5,
        fee_usdc=0.0,
        p_win=0.8,
        edge=0.2,
        quoted_ask=0.5,
        limit_price=0.6,
        book_snapshot="{}",
        created_at=created_at,
    )


def rows(conn, sql: str = "SELECT * FROM paper_orders ORDER BY id") -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql).fetchall()]


def trade(conn, client, *, now: datetime = NOW, geoblock: str = GEO_OK) -> TradeSummary:
    return run_paper_trade(conn, client, now=now, geoblock=geoblock)


def three_order_store(conn, clob) -> dict[str, str]:
    """One filled, one partial and one no_fill order, all YES at ask 0.58."""
    buckets = [
        _bucket("70-71°F", 70, 0.58, 0.45, token="a"),
        _bucket("72-73°F", 72, 0.58, 0.45, token="b"),
        _bucket("74-75°F", 74, 0.58, 0.45, token="c"),
    ]
    market = _market("900001", buckets)
    seed(
        conn,
        [
            (
                market,
                [
                    _outcome("70-71°F", "YES", 0.80, 0.58),
                    _outcome("72-73°F", "YES", 0.79, 0.58),
                    _outcome("74-75°F", "YES", 0.78, 0.58),
                ],
            )
        ],
    )
    clob.books["yes-a"] = DEEP
    clob.books["yes-b"] = THIN
    clob.books["yes-c"] = EMPTY
    return {"filled": "70-71°F", "partial": "72-73°F", "no_fill": "74-75°F"}


# ---------------------------------------------------------------------------
# mode
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "mode"),
    [
        (None, "off"),
        ("", "off"),
        ("off", "off"),
        ("paper", "paper"),
        ("live", "live"),
        ("bogus", "off"),
        ("PAPER", "off"),
        (" paper", "off"),
    ],
)
def test_trade_mode(raw, mode):
    assert trade_mode(raw) == mode


def test_unknown_trade_mode_warns_on_stderr(capsys):
    assert trade_mode("PAPER") == "off"
    assert "PAPER" in capsys.readouterr().err


def test_unset_and_off_trade_mode_stay_silent(capsys):
    trade_mode(None)
    trade_mode("off")
    assert capsys.readouterr().err == ""


# ---------------------------------------------------------------------------
# query helpers
# ---------------------------------------------------------------------------


def test_latest_ok_run_is_the_newest_ok_run_and_ignores_failed_runs(conn):
    assert latest_ok_run(conn) is None
    seed(conn, [], run_id="old", started_at=NOW - timedelta(hours=5))
    seed(conn, [], run_id="newest-ok", started_at=NOW - timedelta(hours=1))
    seed(conn, [], run_id="newer-failed", started_at=NOW, status="failed")
    run = latest_ok_run(conn)
    assert run is not None
    assert run["id"] == "newest-ok"
    assert run["started_at"] == (NOW - timedelta(hours=1)).isoformat()


def test_recommended_polymarket_outcomes_joins_prediction_price_and_market(conn):
    market = _market(
        "900001",
        [
            _bucket("70-71°F", 70, 0.58, 0.45, token="a"),
            _bucket("72-73°F", 72, 0.30, 0.65, token="b"),
        ],
    )
    kalshi = _market("K-1", [_bucket("70-71°F", 70, 0.58, 0.45, token="k")], venue="kalshi")
    seed(
        conn,
        [
            (
                market,
                [
                    _outcome("70-71°F", "YES", 0.80, 0.58),
                    _outcome("70-71°F", "NO", 0.20, 0.45, recommended=False),
                    _outcome("72-73°F", "NO", 0.90, 0.65),
                ],
            ),
            (kalshi, [_outcome("70-71°F", "YES", 0.80, 0.58)]),
        ],
    )
    got = recommended_polymarket_outcomes(conn, "run-1")
    assert [(r["market_id"], r["outcome"], r["side"]) for r in got] == [
        ("900001", "70-71°F", "YES"),
        ("900001", "72-73°F", "NO"),
    ]
    first, second = got
    assert first["p_win"] == pytest.approx(0.80)
    assert first["edge"] == pytest.approx(0.22)
    assert first["quoted_ask"] == pytest.approx(0.58)
    assert first["variable"] == "TMAX"
    assert second["quoted_ask"] == pytest.approx(0.65)  # the NO price row, not the YES row
    assert json.loads(first["raw"])["id"] == "900001"
    assert recommended_polymarket_outcomes(conn, "other-run") == []


def test_recommended_polymarket_outcomes_drops_a_null_side_row(conn):
    market = _market("900001", [_bucket("70-71°F", 70, 0.58, 0.45, token="a")])
    seed(conn, [(market, [_outcome("70-71°F", "YES", 0.80, 0.58)])])
    conn.execute("UPDATE predictions SET side = NULL")
    conn.commit()
    assert recommended_polymarket_outcomes(conn, "run-1") == []


# ---------------------------------------------------------------------------
# gate floor and geoblock
# ---------------------------------------------------------------------------


def test_gate_floor_adds_the_station_delta_only_where_one_is_configured():
    assert gate_floor("TMAX", "KSFO") == pytest.approx(MIN_EDGE + 0.05)
    assert gate_floor("TMAX", "KSFO") == pytest.approx(0.17)
    assert gate_floor("TMIN", "KSFO") == pytest.approx(MIN_EDGE)
    assert gate_floor("TMAX", "KLGA") == pytest.approx(MIN_EDGE)
    assert gate_floor("PRCP", None) == pytest.approx(MIN_EDGE)


def test_check_geoblock_reports_allowed_with_country_and_region_but_never_the_ip(clob, client):
    result = check_geoblock(client)
    assert result == "allowed (country DK, region 84)"
    assert "203.0.113" not in result


def test_check_geoblock_reports_blocked_as_recorded_only(clob, client):
    clob.geoblock = "polymarket_geoblock_blocked.json"
    result = check_geoblock(client)
    assert result == "BLOCKED (country US, region NY), recorded only in paper"
    assert "203.0.113" not in result


@pytest.mark.parametrize(
    "failure",
    [httpx.ConnectError("no route"), httpx.ReadTimeout("slow")],
)
def test_check_geoblock_reports_a_transport_failure(clob, client, failure):
    clob.geoblock = failure
    result = check_geoblock(client)
    assert result.startswith(f"check failed ({type(failure).__name__}")
    assert result.endswith("recorded only in paper")


def test_check_geoblock_reports_an_unparseable_body(clob, client):
    def handler(request):
        return httpx.Response(200, json={"blocked": "no"}, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as odd:
        result = check_geoblock(odd)
    assert result.startswith("check failed (ValidationError")


# ---------------------------------------------------------------------------
# end to end
# ---------------------------------------------------------------------------


def test_end_to_end_records_a_filled_a_partial_and_a_no_fill_order(conn, clob, client):
    names = three_order_store(conn, clob)
    summary = trade(conn, client)

    got = {r["outcome"]: r for r in rows(conn)}
    assert {o: r["status"] for o, r in got.items()} == {v: k for k, v in names.items()}
    fee = taker_fee_per_share(0.58, RATE)
    expected_limit = limit_price(p_win=0.80, floor=MIN_EDGE, fee_rate=RATE)
    assert expected_limit == pytest.approx(0.66893, abs=1e-5)

    filled = got[names["filled"]]
    assert filled["side"] == "YES"
    assert filled["token_id"] == "yes-a"
    assert filled["stake_usdc"] == 5.0
    assert filled["edge"] == pytest.approx(0.80 - 0.58 - fee)  # net, not the stored gross
    assert filled["p_win"] == pytest.approx(0.80)
    assert filled["quoted_ask"] == pytest.approx(0.58)
    assert filled["limit_price"] == pytest.approx(expected_limit)
    assert filled["fill_price"] == pytest.approx(0.58)
    assert filled["shares"] * filled["fill_price"] + filled["fee_usdc"] == pytest.approx(5.0)
    assert filled["created_at"] == NOW.isoformat()
    assert filled["run_id"] == "run-1"
    assert filled["market_id"] == "900001"

    partial = got[names["partial"]]
    assert partial["token_id"] == "yes-b"
    assert partial["shares"] == pytest.approx(6.0)  # 3 at 0.58 and 3 at 0.59, then over the limit
    spent = partial["shares"] * partial["fill_price"] + partial["fee_usdc"]
    assert spent < 5.0
    assert partial["edge"] == pytest.approx(0.79 - 0.58 - fee)

    no_fill = got[names["no_fill"]]
    assert no_fill["token_id"] == "yes-c"
    assert no_fill["shares"] == 0
    assert no_fill["fill_price"] is None
    assert no_fill["quoted_ask"] == pytest.approx(0.58)

    for r in got.values():
        snapshot = OrderBook.model_validate_json(r["book_snapshot"])
        assert snapshot.asset_id == r["token_id"]
    deep = fetch_book("yes-a", client)
    assert OrderBook.model_validate_json(filled["book_snapshot"]) == deep

    assert summary.recorded == {"filled": 1, "partial": 1, "no_fill": 1}
    assert summary.book_errors == 0
    assert sorted(clob.fetched_tokens()[:3]) == ["yes-a", "yes-b", "yes-c"]


def test_a_no_candidate_is_priced_on_the_no_token_and_the_no_ask(conn, clob, client):
    market = _market("900001", [_bucket("70-71°F", 70, 0.30, 0.58, token="a")])
    seed(conn, [(market, [_outcome("70-71°F", "NO", 0.80, 0.58)])])
    clob.books["no-a"] = DEEP  # the YES token is not registered: fetching it fails the test
    trade(conn, client)
    (row,) = rows(conn)
    assert row["side"] == "NO"
    assert row["token_id"] == "no-a"
    assert row["quoted_ask"] == pytest.approx(0.58)
    assert row["p_win"] == pytest.approx(0.80)
    assert row["status"] == "filled"
    assert clob.fetched_tokens() == ["no-a"]


def test_ksfo_tmax_limit_uses_the_station_floor(conn, clob, client):
    market = _market(
        "900001", [_bucket("70-71°F", 70, 0.50, 0.55, token="a")], city="San Francisco"
    )
    seed(conn, [(market, [_outcome("70-71°F", "YES", 0.70, 0.50)])])
    clob.books["yes-a"] = DEEP
    trade(conn, client)
    (row,) = rows(conn)
    assert row["limit_price"] == pytest.approx(limit_price(p_win=0.70, floor=0.17, fee_rate=RATE))
    assert row["limit_price"] != pytest.approx(
        limit_price(p_win=0.70, floor=MIN_EDGE, fee_rate=RATE)
    )


def test_precip_candidate_uses_the_bracket_token_and_the_base_floor(conn, clob, client):
    event = json.loads((FIXTURES / "polymarket_precip_monthly_nyc.json").read_text())
    parsed = parse_precip_event(event)
    bracket = PrecipBracket(
        label='2-3"',
        kind="range",
        lo=2.0,
        hi=3.0,
        threshold=None,
        yes_token_id="yes-precip",
        best_ask=0.50,
        best_bid=0.45,
        yes_price=0.48,
        no_token_id="no-precip",
        no_ask=0.55,
    )
    market = parsed.model_copy(update={"buckets": [bracket]})
    seed_precip(conn, market, [_outcome('2-3"', "YES", 0.70, 0.50)])
    clob.books["yes-precip"] = DEEP
    trade(conn, client)
    (row,) = rows(conn)
    assert row["token_id"] == "yes-precip"
    assert row["market_id"] == market.id
    assert row["limit_price"] == pytest.approx(limit_price(p_win=0.70, floor=0.12, fee_rate=RATE))


def test_kalshi_and_unrecommended_outcomes_are_never_loaded_or_fetched(conn, clob, client):
    market = _market(
        "900001",
        [
            _bucket("70-71°F", 70, 0.58, 0.45, token="a"),
            _bucket("72-73°F", 72, 0.58, 0.45, token="b"),
        ],
    )
    kalshi = _market("K-1", [_bucket("70-71°F", 70, 0.58, 0.45, token="k")], venue="kalshi")
    seed(
        conn,
        [
            (
                market,
                [
                    _outcome("70-71°F", "YES", 0.80, 0.58),
                    _outcome("72-73°F", "YES", 0.80, 0.58, recommended=False),
                ],
            ),
            (kalshi, [_outcome("70-71°F", "YES", 0.80, 0.58)]),
        ],
    )
    clob.books["yes-a"] = DEEP
    summary = trade(conn, client)
    assert [r["token_id"] for r in rows(conn)] == ["yes-a"]
    assert clob.fetched_tokens() == ["yes-a"]
    assert summary.loaded == 1


# ---------------------------------------------------------------------------
# sizing: net-edge ordering and caps
# ---------------------------------------------------------------------------


def test_orders_are_ranked_by_net_edge_not_gross_edge(conn, clob, client):
    # A has the higher gross edge (0.135 vs 0.130) but the lower net edge
    # (0.1225 vs 0.1236) once the fee at its price is taken off.
    market = _market(
        "900001",
        [
            _bucket("70-71°F", 70, 0.50, 0.55, token="a"),
            _bucket("72-73°F", 72, 0.85, 0.20, token="b"),
        ],
    )
    seed(
        conn,
        [
            (
                market,
                [
                    _outcome("70-71°F", "YES", 0.635, 0.50),
                    _outcome("72-73°F", "YES", 0.98, 0.85),
                ],
            )
        ],
    )
    seed_open_position(conn, "900002", 145.0, created_at=NOW - timedelta(days=2))
    clob.books["yes-b"] = DEEP
    summary = trade(conn, client)
    assert [
        r["outcome"] for r in rows(conn, "SELECT * FROM paper_orders WHERE run_id='run-1'")
    ] == ["72-73°F"]
    assert summary.blocked["open_cap"] == 1
    assert clob.fetched_tokens() == ["yes-b"]


def test_the_daily_cap_blocks_the_third_order(conn, clob, client):
    buckets = [
        _bucket(f"{70 + 2 * i}-{71 + 2 * i}°F", 70 + 2 * i, 0.58, 0.45, token=str(i))
        for i in range(3)
    ]
    market = _market("900001", buckets)
    seed(
        conn,
        [
            (
                market,
                [_outcome(b.label, "YES", 0.80 - 0.01 * i, 0.58) for i, b in enumerate(buckets)],
            )
        ],
    )
    seed_open_position(conn, "900002", 40.0, created_at=NOW - timedelta(hours=1))
    for i in range(3):
        clob.books[f"yes-{i}"] = DEEP
    summary = trade(conn, client)
    new = rows(conn, "SELECT * FROM paper_orders WHERE run_id='run-1'")
    assert [r["outcome"] for r in new] == [buckets[0].label, buckets[1].label]
    assert summary.planned == 2
    assert summary.blocked["daily_cap"] == 1
    assert clob.fetched_tokens() == ["yes-0", "yes-1"]


def test_a_second_trade_skips_open_positions_but_retries_a_no_fill(conn, clob, client):
    names = three_order_store(conn, clob)
    trade(conn, client)
    assert len(rows(conn)) == 3

    # The next cycle: a newer run over the same outcomes.
    market = _market(
        "900001",
        [
            _bucket("70-71°F", 70, 0.58, 0.45, token="a"),
            _bucket("72-73°F", 72, 0.58, 0.45, token="b"),
            _bucket("74-75°F", 74, 0.58, 0.45, token="c"),
        ],
    )
    later = NOW + timedelta(minutes=2)
    seed(
        conn,
        [
            (
                market,
                [
                    _outcome("70-71°F", "YES", 0.80, 0.58),
                    _outcome("72-73°F", "YES", 0.79, 0.58),
                    _outcome("74-75°F", "YES", 0.78, 0.58),
                ],
            )
        ],
        run_id="run-2",
        started_at=later - timedelta(minutes=3),
    )
    clob.requests.clear()
    summary = trade(conn, client, now=later)

    assert summary.blocked["open_position"] == 2
    assert clob.fetched_tokens() == ["yes-c"]
    again = rows(conn, "SELECT * FROM paper_orders WHERE run_id='run-2'")
    assert [(r["outcome"], r["status"]) for r in again] == [(names["no_fill"], "no_fill")]
    assert len(rows(conn)) == 4


# ---------------------------------------------------------------------------
# failures
# ---------------------------------------------------------------------------


def _bad_responses() -> dict[str, Any]:
    good = _fixture(DEEP)
    wrong_asset = dict(good, asset_id="some-other-token")
    return {
        "yes-a": httpx.Response(404, json={"error": "No orderbook exists"}),
        "yes-b": httpx.ReadTimeout("slow book"),
        "yes-c": wrong_asset,
        "yes-d": DEEP,
    }


def test_a_failed_book_fetch_records_no_row_and_the_other_orders_still_record(conn, clob, client):
    buckets = [
        _bucket(f"{70 + 2 * i}-{71 + 2 * i}°F", 70 + 2 * i, 0.58, 0.45, token="abcd"[i])
        for i in range(4)
    ]
    market = _market("900001", buckets)
    seed(
        conn,
        [
            (
                market,
                [_outcome(b.label, "YES", 0.80 - 0.01 * i, 0.58) for i, b in enumerate(buckets)],
            )
        ],
    )
    clob.books.update(_bad_responses())
    summary = trade(conn, client)
    assert [r["token_id"] for r in rows(conn)] == ["yes-d"]
    assert summary.book_errors == 3
    assert summary.recorded == {"filled": 1, "partial": 0, "no_fill": 0}
    assert summary.planned == 4
    text = render_trade_summary(summary)
    assert "3 book errors" in text


@pytest.mark.parametrize(
    "geoblock",
    [
        "BLOCKED (country US, region NY), recorded only in paper",
        "check failed (ConnectError: no route), recorded only in paper",
    ],
)
def test_a_geoblock_result_is_reported_but_orders_still_record(conn, clob, client, geoblock):
    three_order_store(conn, clob)
    summary = trade(conn, client, geoblock=geoblock)
    assert len(rows(conn)) == 3
    assert summary.geoblock == geoblock
    assert f"geoblock: {geoblock}" in render_trade_summary(summary)


# ---------------------------------------------------------------------------
# freshness
# ---------------------------------------------------------------------------


def _assert_no_trade(conn, clob, summary, reason: str) -> None:
    assert summary.no_trade_reason is not None
    assert reason in summary.no_trade_reason
    assert rows(conn) == []
    assert clob.requests == []
    assert f"no trade: {summary.no_trade_reason}" in render_trade_summary(summary)


def test_an_empty_store_does_not_trade(conn, clob, client):
    _assert_no_trade(conn, clob, trade(conn, client), "no ok run")


def test_a_failed_only_run_does_not_trade(conn, clob, client):
    three_order_store(conn, clob)
    conn.execute("UPDATE runs SET status = 'failed'")
    conn.commit()
    _assert_no_trade(conn, clob, trade(conn, client), "no ok run")


def test_a_run_older_than_the_limit_does_not_trade(conn, clob, client):
    three_order_store(conn, clob)
    stale = NOW - timedelta(minutes=60, seconds=1)
    conn.execute("UPDATE runs SET started_at = ?", (stale.isoformat(),))
    conn.commit()
    _assert_no_trade(conn, clob, trade(conn, client), "60 min")


def test_a_run_that_starts_in_the_future_does_not_trade(conn, clob, client):
    three_order_store(conn, clob)
    conn.execute("UPDATE runs SET started_at = ?", ((NOW + timedelta(seconds=1)).isoformat(),))
    conn.commit()
    _assert_no_trade(conn, clob, trade(conn, client), "future")


@pytest.mark.parametrize("stamp", ["not a time", "2026-11-03T17:53:00"])
def test_an_unparseable_or_naive_start_time_does_not_trade(conn, clob, client, stamp):
    three_order_store(conn, clob)
    conn.execute("UPDATE runs SET started_at = ?", (stamp,))
    conn.commit()
    _assert_no_trade(conn, clob, trade(conn, client), "start time")


def test_a_run_exactly_at_the_age_limit_still_trades(conn, clob, client):
    three_order_store(conn, clob)
    conn.execute("UPDATE runs SET started_at = ?", ((NOW - timedelta(minutes=60)).isoformat(),))
    conn.commit()
    summary = trade(conn, client)
    assert summary.no_trade_reason is None
    assert len(rows(conn)) == 3


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------


def test_summary_counts_match_the_recorded_rows(conn, clob, client):
    three_order_store(conn, clob)
    summary = trade(conn, client)
    by_status = {
        r["status"]: r["n"]
        for r in rows(conn, "SELECT status, count(*) AS n FROM paper_orders GROUP BY status")
    }
    assert {k: v for k, v in summary.recorded.items() if v} == by_status
    spent = rows(
        conn,
        "SELECT SUM(shares * fill_price + fee_usdc) AS s FROM paper_orders "
        "WHERE status IN ('filled', 'partial')",
    )[0]["s"]
    assert summary.staked_usdc == pytest.approx(spent)
    assert summary.planned_stake_usdc == pytest.approx(15.0)

    text = render_trade_summary(summary)
    lines = text.splitlines()
    assert "trade: mode paper" in lines
    assert f"geoblock: {GEO_OK}" in lines
    assert any(line.startswith("run: run-1 started ") and "7 min ago" in line for line in lines)
    assert "candidates: 3 loaded, 0 skipped (bad_row 0, no_token 0, no_limit 0)" in lines
    assert any(
        line.startswith("sizing: 3 orders, 0 blocked (halted 0, open_position 0") for line in lines
    )
    assert "orders: 3 recorded (filled 1, partial 1, no_fill 1), 0 book errors" in lines
    assert any(line.startswith(f"staked: {spent:.2f} USDC") for line in lines)
    assert sum(line.startswith("order: ") for line in lines) == 3


def test_candidates_with_no_token_a_bad_row_or_no_limit_are_skipped_and_counted(conn, clob, client):
    buckets = [
        _bucket(f"{70 + 2 * i}-{71 + 2 * i}°F", 70 + 2 * i, 0.58, 0.45, token=str(i))
        for i in range(5)
    ]
    market = _market("900001", buckets)
    seed(
        conn,
        [
            (
                market,
                [
                    _outcome(buckets[0].label, "YES", 0.80, 0.58),  # missing label in raw
                    _outcome(buckets[1].label, "YES", 0.79, 0.58),  # empty token in raw
                    _outcome(buckets[2].label, "YES", 0.78, 0.58),  # NULL p_win
                    _outcome(buckets[3].label, "YES", 0.10, 0.58),  # recommended, no limit
                    _outcome(buckets[4].label, "YES", 0.77, 0.58),  # fine
                ],
            )
        ],
    )
    raw = json.loads(conn.execute("SELECT raw FROM markets").fetchone()["raw"])
    raw["buckets"][0]["label"] = "renamed"
    raw["buckets"][1]["yes_token_id"] = ""
    conn.execute("UPDATE markets SET raw = ?", (json.dumps(raw),))
    conn.execute("UPDATE predictions SET p_win = NULL WHERE bucket = ?", (buckets[2].label,))
    conn.commit()
    clob.books["yes-4"] = DEEP
    summary = trade(conn, client)
    assert summary.loaded == 1
    assert summary.skipped == {"bad_row": 1, "no_token": 2, "no_limit": 1}
    assert clob.fetched_tokens() == ["yes-4"]
    assert len(rows(conn)) == 1
    assert (
        "candidates: 1 loaded, 4 skipped (bad_row 1, no_token 2, no_limit 1)"
        in render_trade_summary(summary).splitlines()
    )


def test_a_priced_run_without_candidates_trades_nothing_and_says_so(conn, clob, client):
    market = _market("900001", [_bucket("70-71°F", 70, 0.58, 0.45, token="a")])
    seed(conn, [(market, [_outcome("70-71°F", "YES", 0.80, 0.58, recommended=False)])])
    summary = trade(conn, client)
    assert summary.loaded == 0
    assert summary.planned == 0
    assert rows(conn) == []
    assert clob.requests == []
    assert "orders: 0 recorded" in render_trade_summary(summary)
