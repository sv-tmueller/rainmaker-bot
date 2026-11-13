"""Tests for the entry-delay replay diagnostic (delay-replay, #526).

The math tests feed synthetic PricePoint series with prices that step at known
hours, so every expected ROI is a hand-computed number. The fetch-path tests
reuse the saved CLOB fixtures through pytest-httpx; nothing here reaches a live
endpoint.
"""

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from rainmaker import cli
from rainmaker.config import build_target
from rainmaker.delay_replay import (
    DELAY_SNAP_TOLERANCE_S,
    DELAYS_H,
    ReplayBet,
    load_replay_bets,
    render_delay_replay,
    run_delay_replay,
    select_bets,
    settlement_cutoff_ts,
    summarize,
)
from rainmaker.domain import Bucket, Market
from rainmaker.polymarket.prices import CLOB_PRICES_URL, PricePoint
from rainmaker.store.db import connect, init_schema

FIXTURES = Path(__file__).parent / "fixtures"
H = 3600


def _row(
    *,
    market_id: str = "m1",
    run_id: str = "r1",
    bucket: str = "70-71°F",
    side: str | None = "YES",
    edge: float | None = 0.20,
    recommended: bool | int = 1,
    venue: str = "polymarket",
    city: str = "NYC",
    settlement_date: str = "2026-05-30",
    started_at: str = "2026-05-29T12:00:00+00:00",
    ask: float = 0.35,
    actual_value: float = 71.0,
) -> dict[str, Any]:
    return {
        "market_id": market_id,
        "run_id": run_id,
        "bucket": bucket,
        "side": side,
        "p_win": 0.85,
        "edge": edge,
        "recommended": recommended,
        "variable": "TMAX",
        "venue": venue,
        "outcome_spec": None,
        "city": city,
        "settlement_date": settlement_date,
        "started_at": started_at,
        "ask": ask,
        "implied_prob": ask,
        "actual_value": actual_value,
    }


def _ts(stamp: str) -> int:
    return int(datetime.fromisoformat(stamp).timestamp())


def _bet(
    row: dict[str, Any],
    points: list[PricePoint] | None,
    *,
    miss: str | None = None,
    cutoff_ts: int | None = None,
) -> ReplayBet:
    run_ts = _ts(row["started_at"])
    if cutoff_ts is None:
        cutoff_ts = run_ts + 10_000 * H  # far away: the cutoff never bites
    return ReplayBet(row=row, run_ts=run_ts, cutoff_ts=cutoff_ts, points=points, miss=miss)


def _stepped(run_ts: int) -> list[PricePoint]:
    """One YES price per window, each stamped just inside its delay's lookback."""
    return [
        PricePoint(t=run_ts - 1800, p=0.30),  # the +0h read
        PricePoint(t=run_ts + 1 * H - 600, p=0.40),  # the +1h read
        PricePoint(t=run_ts + 3 * H - 600, p=0.50),  # the +3h read
        PricePoint(t=run_ts + 6 * H - 600, p=0.60),  # the +6h read
        PricePoint(t=run_ts + 12 * H - 600, p=0.70),  # the +12h read
    ]


def _cell(result: dict[str, Any], lead: str, delay: int) -> dict[str, Any]:
    return result["leads"][lead]["delays"][delay]


# --- bet selection ----------------------------------------------------------


def test_select_bets_is_polymarket_best_per_market_run_only():
    rows = [
        _row(market_id="m1", run_id="r1", bucket="70-71°F", edge=0.10),
        _row(market_id="m1", run_id="r1", bucket="72-73°F", edge=0.30),  # best of the run
        _row(market_id="m2", run_id="r1", venue="kalshi"),
        _row(market_id="m3", run_id="r1", recommended=0),
    ]
    bets, n_ungradable = select_bets(rows, since=None)
    assert [(b["market_id"], b["bucket"]) for b in bets] == [("m1", "72-73°F")]
    assert n_ungradable == 0


def test_select_bets_counts_ungradable_once_and_keeps_them_out():
    rows = [
        _row(market_id="m1", run_id="r1"),
        _row(market_id="m2", run_id="r1", bucket="inches"),  # no parser reads this
    ]
    bets, n_ungradable = select_bets(rows, since=None)
    assert [b["market_id"] for b in bets] == ["m1"]
    assert n_ungradable == 1


def test_select_bets_since_filters_by_run_start_date():
    rows = [
        _row(market_id="m1", run_id="r1", started_at="2026-05-10T12:00:00+00:00"),
        _row(market_id="m2", run_id="r2", started_at="2026-05-20T12:00:00+00:00"),
    ]
    bets, _ = select_bets(rows, since="2026-05-15")
    assert [b["market_id"] for b in bets] == ["m2"]


# --- local-midnight cutoff --------------------------------------------------


def test_settlement_cutoff_is_local_midnight_ending_the_date():
    # 2026-05-30 ends at 2026-05-31 00:00 EDT = 04:00 UTC.
    expected = int(datetime(2026, 5, 31, 4, 0, tzinfo=UTC).timestamp())
    assert settlement_cutoff_ts("2026-05-30", "America/New_York") == expected


# --- summarize: prices ------------------------------------------------------


def test_yes_bet_snaps_each_delay_to_its_own_price():
    row = _row(ask=0.35, actual_value=71.0)  # YES wins
    run_ts = _ts(row["started_at"])
    result = summarize([_bet(row, _stepped(run_ts))], n_ungradable=0)

    expected_price = {0: 0.30, 1: 0.40, 3: 0.50, 6: 0.60, 12: 0.70}
    for delay, price in expected_price.items():
        cell = _cell(result, "1", delay)
        assert cell["priced"] == 1
        assert cell["at_delay"]["staked"] == pytest.approx(price)
        assert cell["at_delay"]["roi"] == pytest.approx((1 - price) / price)
        # Same bets, recorded ask.
        assert cell["at_ask"]["roi"] == pytest.approx((1 - 0.35) / 0.35)


def test_no_bet_is_priced_at_one_minus_yes():
    row = _row(side="NO", ask=0.60, actual_value=80.0)  # 80 not in 70-71: NO wins
    run_ts = _ts(row["started_at"])
    result = summarize([_bet(row, _stepped(run_ts))], n_ungradable=0)

    cell = _cell(result, "1", 1)
    assert cell["at_delay"]["staked"] == pytest.approx(0.60)  # 1 - 0.40
    assert cell["at_delay"]["roi"] == pytest.approx(0.40 / 0.60)
    cell12 = _cell(result, "1", 12)
    assert cell12["at_delay"]["staked"] == pytest.approx(0.30)  # 1 - 0.70


def test_losing_bet_loses_the_delayed_price():
    row = _row(ask=0.35, actual_value=80.0)  # YES loses
    run_ts = _ts(row["started_at"])
    result = summarize([_bet(row, _stepped(run_ts))], n_ungradable=0)
    assert _cell(result, "1", 3)["at_delay"]["pnl"] == pytest.approx(-0.50)
    assert _cell(result, "1", 3)["at_delay"]["roi"] == pytest.approx(-1.0)


def test_delays_are_the_batch_set():
    assert DELAYS_H == (0, 1, 3, 6, 12)


# --- summarize: missing prices ----------------------------------------------


def test_each_missing_reason_is_counted_per_delay():
    run_ts = _ts("2026-05-29T12:00:00+00:00")
    gap_points = [PricePoint(t=run_ts - 7000, p=0.30)]  # fresh at +0h only
    rows = [_row(market_id=f"m{i}", run_id=f"r{i}") for i in range(3)]
    items = [
        _bet(rows[0], None, miss="no_token"),
        _bet(rows[1], None, miss="fetch_error"),
        _bet(rows[2], gap_points),
    ]
    result = summarize(items, n_ungradable=0)

    assert _cell(result, "1", 1)["missing"] == {
        "no_token": 1,
        "fetch_error": 1,
        "past_settlement_day": 0,
        "no_price": 1,
    }
    # The +0h read still finds the 0.30 point, 7000s before the run.
    zero = _cell(result, "1", 0)
    assert zero["priced"] == 1
    assert zero["missing"]["no_price"] == 0


def test_point_older_than_tolerance_is_missing_not_stale_priced():
    row = _row()
    run_ts = _ts(row["started_at"])
    old = [PricePoint(t=run_ts + 1 * H - DELAY_SNAP_TOLERANCE_S - 1, p=0.40)]
    result = summarize([_bet(row, old)], n_ungradable=0)
    cell = _cell(result, "1", 1)
    assert cell["priced"] == 0
    assert cell["missing"]["no_price"] == 1


def test_point_exactly_at_tolerance_is_priced():
    row = _row()
    run_ts = _ts(row["started_at"])
    edge_point = [PricePoint(t=run_ts + 1 * H - DELAY_SNAP_TOLERANCE_S, p=0.40)]
    result = summarize([_bet(row, edge_point)], n_ungradable=0)
    assert _cell(result, "1", 1)["priced"] == 1


def test_delay_at_or_after_settlement_midnight_is_past_settlement_day():
    # Lead 0 run at 20:00 UTC = 16:00 EDT on the settlement date.
    row = _row(settlement_date="2026-05-30", started_at="2026-05-30T20:00:00+00:00")
    run_ts = _ts(row["started_at"])
    cutoff = settlement_cutoff_ts("2026-05-30", "America/New_York")  # 04:00 UTC next day
    assert cutoff == run_ts + 8 * H
    # A fresh point sits before every delay target, so only the cutoff can miss.
    points = [PricePoint(t=run_ts + d * H - 60, p=0.5) for d in DELAYS_H]
    result = summarize([_bet(row, points, cutoff_ts=cutoff)], n_ungradable=0)

    assert _cell(result, "0", 0)["priced"] == 1
    assert _cell(result, "0", 3)["priced"] == 1
    assert _cell(result, "0", 6)["priced"] == 1  # 02:00 UTC, 22:00 local
    assert _cell(result, "0", 12)["priced"] == 0
    assert _cell(result, "0", 12)["missing"]["past_settlement_day"] == 1


def test_target_exactly_at_midnight_counts_as_past_settlement_day():
    row = _row()
    run_ts = _ts(row["started_at"])
    points = [PricePoint(t=run_ts + 1 * H - 60, p=0.5)]
    result = summarize([_bet(row, points, cutoff_ts=run_ts + 1 * H)], n_ungradable=0)
    assert _cell(result, "1", 1)["missing"]["past_settlement_day"] == 1


def test_priced_plus_missing_equals_gradable_in_every_cell():
    run_ts = _ts("2026-05-29T12:00:00+00:00")
    rows = [
        _row(market_id="m1", run_id="r1"),
        _row(market_id="m2", run_id="r2", started_at="2026-05-30T12:00:00+00:00"),
        _row(market_id="m3", run_id="r3", side="NO", actual_value=80.0),
        _row(market_id="m4", run_id="r4"),
    ]
    items = [
        _bet(rows[0], _stepped(run_ts)),
        _bet(rows[1], _stepped(_ts(rows[1]["started_at"]))),
        _bet(rows[2], _stepped(run_ts)),
        _bet(rows[3], None, miss="fetch_error"),
    ]
    result = summarize(items, n_ungradable=2)

    assert result["n_ungradable"] == 2
    assert result["n_gradable"] == 4
    assert result["n_bets"] == 6
    for lead, block in result["leads"].items():
        for delay in DELAYS_H:
            cell = block["delays"][delay]
            assert cell["priced"] + sum(cell["missing"].values()) == block["n"], (lead, delay)


# --- summarize: grouping ----------------------------------------------------


def test_lead_rows_add_up_to_all():
    run_ts0 = _ts("2026-05-30T12:00:00+00:00")  # lead 0
    run_ts1 = _ts("2026-05-29T12:00:00+00:00")  # lead 1
    rows = [
        _row(market_id="m1", run_id="r1", started_at="2026-05-30T12:00:00+00:00"),
        _row(market_id="m2", run_id="r2", started_at="2026-05-29T12:00:00+00:00"),
        _row(market_id="m3", run_id="r3", started_at="2026-05-29T12:00:00+00:00", ask=0.5),
    ]
    items = [
        _bet(rows[0], _stepped(run_ts0)),
        _bet(rows[1], _stepped(run_ts1)),
        _bet(rows[2], _stepped(run_ts1)),
    ]
    result = summarize(items, n_ungradable=0)

    assert set(result["leads"]) == {"0", "1", "all"}
    assert result["leads"]["0"]["n"] == 1
    assert result["leads"]["1"]["n"] == 2
    assert result["leads"]["all"]["n"] == 3
    for delay in DELAYS_H:
        parts = [_cell(result, lead, delay) for lead in ("0", "1")]
        total = _cell(result, "all", delay)
        assert total["priced"] == sum(p["priced"] for p in parts)
        assert total["at_delay"]["pnl"] == pytest.approx(sum(p["at_delay"]["pnl"] for p in parts))
        assert total["at_ask"]["pnl"] == pytest.approx(sum(p["at_ask"]["pnl"] for p in parts))


def test_headline_is_ask_roi_over_all_gradable_bets():
    run_ts = _ts("2026-05-29T12:00:00+00:00")
    rows = [
        _row(market_id="m1", run_id="r1", ask=0.40, actual_value=71.0),  # win: +0.60
        _row(market_id="m2", run_id="r2", ask=0.50, actual_value=80.0),  # loss: -0.50
    ]
    # One bet has no price at any delay; the headline still counts it.
    items = [_bet(rows[0], _stepped(run_ts)), _bet(rows[1], None, miss="no_token")]
    result = summarize(items, n_ungradable=0)
    assert result["headline"]["n_bets"] == 2
    assert result["headline"]["roi"] == pytest.approx((0.60 - 0.50) / 0.90)


def test_empty_input_has_headline_and_no_leads_but_all():
    result = summarize([], n_ungradable=0)
    assert result["n_gradable"] == 0
    assert result["headline"]["n_bets"] == 0
    assert list(result["leads"]) == ["all"]
    assert result["leads"]["all"]["n"] == 0


# --- fetch path (pytest-httpx, saved fixtures) ------------------------------

_TOKEN = "tok-yes-70-71"
_NYC_RUN = "2026-03-01T12:00:00+00:00"  # inside the saved fixture window


def _market_raw(market_id: str, settlement: str, token: str) -> str:
    market = Market(
        id=market_id,
        slug="s",
        title="t",
        target=build_target("NYC", "TMAX", datetime.fromisoformat(settlement).date()),
        buckets=[
            Bucket(
                label="70-71°F",
                kind="range",
                lo=70,
                hi=71,
                threshold=None,
                yes_token_id=token,
                best_ask=0.35,
                best_bid=None,
                yes_price=0.35,
            )
        ],
    )
    return json.dumps(market.model_dump(mode="json"))


def _seed(
    conn,
    market_id: str,
    run_id: str,
    *,
    token: str = _TOKEN,
    side: str = "YES",
    venue: str = "polymarket",
    raw: str | None = None,
    started_at: str = _NYC_RUN,
    settlement: str = "2026-03-02",
    ask: float = 0.35,
    actual: float = 71.0,
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO runs (id, started_at, status) VALUES (?, ?, ?)",
        (run_id, started_at, "ok"),
    )
    if raw is None:
        raw = _market_raw(market_id, settlement, token)
    conn.execute(
        "INSERT INTO markets (id, city, variable, settlement_date, venue, raw) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (market_id, "NYC", "TMAX", settlement, venue, raw),
    )
    conn.execute(
        "INSERT INTO prices (run_id, market_id, outcome, side, price, implied_prob, captured_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (run_id, market_id, "70-71°F", side, ask, ask, "t"),
    )
    conn.execute(
        "INSERT INTO predictions "
        "(run_id, market_id, bucket, side, p_win, edge, recommended, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, market_id, "70-71°F", side, 0.88, 0.20, 1, "t"),
    )
    conn.execute(
        "INSERT INTO outcomes (market_id, actual_value, settled_at) VALUES (?, ?, ?)",
        (market_id, actual, "t"),
    )
    conn.commit()


def _prices_url(token: str) -> re.Pattern[str]:
    return re.compile(re.escape(CLOB_PRICES_URL) + rf".*market={token}(&|$)")


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def test_run_prices_every_delay_from_the_saved_history(httpx_mock):
    conn = connect(":memory:")
    init_schema(conn)
    _seed(conn, "m1", "r1")  # YES, ask 0.35, wins
    _seed(conn, "m2", "r2", token="tok-no", side="NO", ask=0.80, actual=80.0)  # NO wins
    httpx_mock.add_response(url=_prices_url(_TOKEN), json=_fixture("clob_prices_history.json"))
    httpx_mock.add_response(url=_prices_url("tok-no"), json=_fixture("clob_prices_history.json"))

    with httpx.Client() as client:
        result = run_delay_replay(conn, client, since=None)
    conn.close()

    assert result["n_gradable"] == 2
    for delay in DELAYS_H:
        cell = _cell(result, "1", delay)
        assert cell["priced"] == 2
        assert sum(cell["missing"].values()) == 0
        # YES at 0.15 wins 0.85; NO at 1 - 0.15 = 0.85 wins 0.15.
        assert cell["at_delay"]["pnl"] == pytest.approx(0.85 + 0.15)
        assert cell["at_delay"]["staked"] == pytest.approx(0.15 + 0.85)


def test_run_sends_one_request_per_bet_and_uses_the_yes_token(httpx_mock):
    conn = connect(":memory:")
    init_schema(conn)
    _seed(conn, "m1", "r1", token="tok-a")
    httpx_mock.add_response(url=_prices_url("tok-a"), json=_fixture("clob_prices_history.json"))
    with httpx.Client() as client:
        run_delay_replay(conn, client, since=None)
    conn.close()
    assert len(httpx_mock.get_requests()) == 1


def test_empty_history_counts_no_price_after_the_coarse_retry(httpx_mock):
    conn = connect(":memory:")
    init_schema(conn)
    _seed(conn, "m1", "r1")
    # An empty hourly series triggers the 720-fidelity retry: two requests.
    for _ in range(2):
        httpx_mock.add_response(
            url=_prices_url(_TOKEN), json=_fixture("clob_prices_history_empty.json")
        )
    with httpx.Client() as client:
        result = run_delay_replay(conn, client, since=None)
    conn.close()

    for delay in DELAYS_H:
        cell = _cell(result, "1", delay)
        assert cell["priced"] == 0
        assert cell["missing"]["no_price"] == 1


def test_http_error_counts_fetch_error_and_the_bet_stays_in_n(httpx_mock):
    conn = connect(":memory:")
    init_schema(conn)
    _seed(conn, "m1", "r1")
    httpx_mock.add_response(url=_prices_url(_TOKEN), status_code=500)
    with httpx.Client() as client:
        result = run_delay_replay(conn, client, since=None)
    conn.close()

    assert result["leads"]["all"]["n"] == 1
    for delay in DELAYS_H:
        assert _cell(result, "all", delay)["missing"]["fetch_error"] == 1


def test_malformed_body_counts_fetch_error(httpx_mock):
    conn = connect(":memory:")
    init_schema(conn)
    _seed(conn, "m1", "r1")
    httpx_mock.add_response(url=_prices_url(_TOKEN), json={"nope": []})
    with httpx.Client() as client:
        result = run_delay_replay(conn, client, since=None)
    conn.close()
    assert _cell(result, "1", 1)["missing"]["fetch_error"] == 1


def test_unparsable_raw_counts_no_token_without_a_request(httpx_mock):
    conn = connect(":memory:")
    init_schema(conn)
    _seed(conn, "m1", "r1", raw="not json")
    with httpx.Client() as client:
        result = run_delay_replay(conn, client, since=None)
    conn.close()
    assert _cell(result, "1", 12)["missing"]["no_token"] == 1
    assert httpx_mock.get_requests() == []


def test_precip_market_bet_resolves_its_token(httpx_mock):
    from rainmaker.config import PrecipStation
    from rainmaker.domain import PrecipBracket, PrecipMonthlyMarket, PrecipTarget

    station = PrecipStation(
        city="NYC",
        resolution_name="NYC",
        name="Central Park",
        lat=40.78,
        lon=-73.97,
        timezone="America/New_York",
        ghcnd_id="USW00094728",
    )
    market = PrecipMonthlyMarket(
        id="p1",
        slug="s",
        title="t",
        target=PrecipTarget(
            station=station,
            variable="PRCP",
            year=2026,
            month=3,
            settlement_date=datetime(2026, 3, 31).date(),
        ),
        buckets=[
            PrecipBracket(
                label="2-3",
                kind="range",
                lo=2.0,
                hi=3.0,
                threshold=None,
                yes_token_id="tok-precip",
                best_ask=0.3,
                best_bid=None,
                yes_price=0.3,
            )
        ],
    )
    raw = json.dumps(market.model_dump(mode="json"))
    conn = connect(":memory:")
    init_schema(conn)
    _seed(conn, "p1", "r1", raw=raw, settlement="2026-03-31")
    conn.execute("UPDATE markets SET variable = 'PRCP' WHERE id = 'p1'")
    conn.execute("UPDATE predictions SET bucket = '2-3' WHERE market_id = 'p1'")
    conn.execute("UPDATE prices SET outcome = '2-3' WHERE market_id = 'p1'")
    conn.execute("UPDATE outcomes SET actual_value = 2.5 WHERE market_id = 'p1'")
    conn.commit()
    httpx_mock.add_response(
        url=_prices_url("tok-precip"), json=_fixture("clob_prices_history.json")
    )
    with httpx.Client() as client:
        result = run_delay_replay(conn, client, since=None)
    conn.close()
    assert _cell(result, "all", 1)["priced"] == 1


def test_kalshi_rows_are_not_fetched(httpx_mock):
    conn = connect(":memory:")
    init_schema(conn)
    _seed(conn, "m1", "r1", venue="kalshi")
    with httpx.Client() as client:
        result = run_delay_replay(conn, client, since=None)
    conn.close()
    assert result["n_bets"] == 0
    assert httpx_mock.get_requests() == []


def test_load_replay_bets_marks_the_late_cutoff_from_the_station_timezone(httpx_mock):
    conn = connect(":memory:")
    init_schema(conn)
    _seed(conn, "m1", "r1")
    httpx_mock.add_response(url=_prices_url(_TOKEN), json=_fixture("clob_prices_history.json"))
    bets, _ = select_bets(
        [
            {
                **_row(started_at=_NYC_RUN, settlement_date="2026-03-02"),
                "market_id": "m1",
            }
        ],
        since=None,
    )
    with httpx.Client() as client:
        items = load_replay_bets(conn, client, bets)
    conn.close()
    assert items[0].cutoff_ts == settlement_cutoff_ts("2026-03-02", "America/New_York")
    assert items[0].run_ts == _ts(_NYC_RUN)


# --- renderer ---------------------------------------------------------------


def _rendered(result: dict[str, Any], since: str | None = None) -> str:
    return "\n".join(render_delay_replay(result, since=since))


def test_render_states_trade_prices_not_asks_and_the_caveats():
    run_ts = _ts("2026-05-29T12:00:00+00:00")
    result = summarize([_bet(_row(), _stepped(run_ts))], n_ungradable=1)
    text = _rendered(result, since="2026-05-01")

    assert "trade prices, not asks" in text
    assert "recorded ask" in text
    assert "1 - YES" in text
    assert "gross of fees" in text
    assert "no re-check of edge" in text
    assert "Polymarket only" in text
    assert "since: 2026-05-01" in text
    assert "ungradable" in text
    for delay in DELAYS_H:
        assert f"+{delay}h" in text


def test_render_shows_missing_counts_by_reason():
    run_ts = _ts("2026-05-29T12:00:00+00:00")
    rows = [_row(market_id="m1", run_id="r1"), _row(market_id="m2", run_id="r2")]
    items = [_bet(rows[0], _stepped(run_ts)), _bet(rows[1], None, miss="fetch_error")]
    text = _rendered(summarize(items, n_ungradable=0))
    assert "fetch_error" in text
    assert "no_token" in text
    assert "past_settlement_day" in text
    assert "no_price" in text


def test_render_empty_result_does_not_crash():
    text = _rendered(summarize([], n_ungradable=0))
    assert "no settled Polymarket bets" in text


# --- CLI --------------------------------------------------------------------

PG_DSN = "postgresql://u:hunter2-topsecret@example.invalid/db"


class _Connected(Exception):
    pass


def _boom(*_a, **_k):
    raise _Connected


def test_cli_refuses_a_postgres_dsn_without_allow_prod(monkeypatch):
    monkeypatch.setattr(cli, "connect", _boom)
    monkeypatch.setenv("DATABASE_URL", PG_DSN)
    with pytest.raises(SystemExit) as exc:
        cli.main(["delay-replay"])
    assert "--allow-prod" in str(exc.value)
    assert "hunter2-topsecret" not in str(exc.value)


def test_cli_with_allow_prod_proceeds_to_connect(monkeypatch):
    monkeypatch.setattr(cli, "connect", _boom)
    monkeypatch.setenv("DATABASE_URL", PG_DSN)
    with pytest.raises(_Connected):
        cli.main(["delay-replay", "--allow-prod"])


def test_cli_missing_sqlite_file_exits_without_creating_it(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    db = tmp_path / "absent" / "x.db"
    with pytest.raises(SystemExit) as exc:
        cli.main(["delay-replay", "--db", str(db)])
    assert "not found" in str(exc.value)
    assert not db.exists()
    assert not db.parent.exists()


def test_cli_runs_on_sqlite_and_passes_since(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    db = tmp_path / "x.db"
    conn = connect(str(db))
    init_schema(conn)
    conn.close()
    seen: dict[str, Any] = {}

    import rainmaker.delay_replay as module

    real = module.run_delay_replay

    def spy(conn, client, since=None):
        seen["since"] = since
        return real(conn, client, since=since)

    monkeypatch.setattr(module, "run_delay_replay", spy)
    cli.main(["delay-replay", "--db", str(db), "--since", "2026-05-15"])
    out = capsys.readouterr().out
    assert seen["since"] == "2026-05-15"
    assert "trade prices, not asks" in out
    assert "since: 2026-05-15" in out


def test_cli_does_not_write_to_the_store(monkeypatch, tmp_path):
    """The diagnostic must not run init_schema: no tables appear in an empty file."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    db = tmp_path / "empty.db"
    import sqlite3

    sqlite3.connect(db).close()
    with pytest.raises(Exception, match="no such table"):
        cli.main(["delay-replay", "--db", str(db)])
    raw = sqlite3.connect(db)
    tables = raw.execute("SELECT name FROM sqlite_master").fetchall()
    raw.close()
    assert tables == []
