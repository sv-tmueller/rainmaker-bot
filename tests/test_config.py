import zoneinfo
from datetime import date

from rainmaker.config import STATIONS, build_target


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
