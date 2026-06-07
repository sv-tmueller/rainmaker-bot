import zoneinfo
from datetime import date

from rainmaker.config import (
    KALSHI_HIGH_SERIES,
    KALSHI_LOW_SERIES,
    KALSHI_STATIONS,
    PRECIP_STATIONS,
    STATIONS,
    build_target,
)


def test_nyc_station_resolves_to_klga():
    s = STATIONS["NYC"]
    assert s.icao == "KLGA"
    assert s.timezone == "America/New_York"
    assert abs(s.lat - 40.7792) < 1e-6
    assert abs(s.lon - (-73.8803)) < 1e-6


def test_build_target():
    t = build_target("NYC", "TMAX", date(2026, 5, 31))
    assert t.station.icao == "KLGA"
    assert t.variable == "TMAX"
    assert t.local_date == date(2026, 5, 31)


EXPECTED_CITIES = {
    "NYC",
    "Miami",
    "Chicago",
    "Dallas",
    "Houston",
    "Los Angeles",
    "San Francisco",
    "Seattle",
    "Austin",
    "Atlanta",
    "Denver",
}


def test_all_us_cities_present():
    assert set(STATIONS) == EXPECTED_CITIES


def test_every_station_is_valid():
    for key, s in STATIONS.items():
        assert s.city == key
        assert len(s.icao) == 4 and s.icao.startswith("K")
        assert s.name
        assert -90 <= s.lat <= 90
        assert -180 <= s.lon <= 180
        assert s.wunderground_url.startswith("https://")
        assert s.ghcnd_id.startswith("USW")
        zoneinfo.ZoneInfo(s.timezone)  # raises if the timezone is invalid


def test_trap_stations_resolve_to_the_right_airport():
    # the market settles on these stations, not the city's obvious main airport
    assert STATIONS["Dallas"].icao == "KDAL"  # Love Field, not DFW
    assert STATIONS["Houston"].icao == "KHOU"  # Hobby, not IAH
    assert STATIONS["Denver"].icao == "KBKF"  # Buckley SFB, not KDEN


def test_precip_nyc_resolves_to_central_park():
    s = PRECIP_STATIONS["NYC"]
    assert s.ghcnd_id == "USW00094728"  # Central Park, confirmed GSOM anchor 3.06 in May 2026
    assert s.resolution_name == "Central Park NY"  # the climate-tool label named in the rules
    assert s.timezone == "America/New_York"


def test_precip_seattle_resolves_to_city_area_not_seatac():
    s = PRECIP_STATIONS["Seattle"]
    # "Seattle City Area" (wfo=sew) threads to Sand Point WFO, NOT the SeaTac
    # temperature station; confirmed by GSOM == ACIS monthly precip.
    assert s.ghcnd_id == "USW00094290"
    assert s.ghcnd_id != STATIONS["Seattle"].ghcnd_id  # not SeaTac USW00024233
    assert s.resolution_name == "Seattle City Area"


def test_precip_stations_present():
    assert set(PRECIP_STATIONS) == {"NYC", "Seattle"}


def test_every_precip_station_is_valid():
    for key, s in PRECIP_STATIONS.items():
        assert s.city == key
        assert s.resolution_name
        assert s.name
        assert -90 <= s.lat <= 90
        assert -180 <= s.lon <= 180
        assert s.ghcnd_id.startswith("US")
        zoneinfo.ZoneInfo(s.timezone)  # raises if the timezone is invalid


def test_kalshi_registry_aligned():
    # every city with a series ticker has a settlement station and vice versa, and
    # high and low temp cover the same cities (they share the per-city CLI station)
    assert set(KALSHI_HIGH_SERIES) == set(KALSHI_STATIONS)
    assert set(KALSHI_LOW_SERIES) == set(KALSHI_STATIONS)
    # the two cities that differ from the Polymarket temperature stations
    assert KALSHI_STATIONS["NYC"].icao == "KNYC"  # Central Park, not LaGuardia
    assert KALSHI_STATIONS["Chicago"].icao == "KMDW"  # Midway, not O'Hare
    # every station carries the rule-text guard phrase and a resolution-source URL
    for city, st in KALSHI_STATIONS.items():
        assert st.name, city
        assert st.wunderground_url.startswith("https://"), city
        assert st.ghcnd_id.startswith("USW"), city
        zoneinfo.ZoneInfo(st.timezone)
