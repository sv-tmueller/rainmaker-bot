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
