from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict

Variable = Literal["TMAX", "TMIN"]


class Station(BaseModel):
    model_config = ConfigDict(frozen=True)

    city: str
    icao: str
    name: str
    lat: float
    lon: float
    timezone: str
    wunderground_url: str
    ghcnd_id: str  # NOAA NCEI GHCND station id, for backfill actuals


class Target(BaseModel):
    model_config = ConfigDict(frozen=True)

    station: Station
    variable: Variable
    local_date: date


STATIONS: dict[str, Station] = {
    "NYC": Station(
        city="NYC",
        icao="KLGA",
        name="LaGuardia Airport",
        lat=40.7792,
        lon=-73.8803,
        timezone="America/New_York",
        wunderground_url="https://www.wunderground.com/history/daily/us/ny/new-york-city/KLGA",
        ghcnd_id="USW00014732",
    ),
    "Miami": Station(
        city="Miami",
        icao="KMIA",
        name="Miami Intl Airport",
        lat=25.7881,
        lon=-80.3169,
        timezone="America/New_York",
        wunderground_url="https://www.wunderground.com/history/daily/us/fl/miami/KMIA",
        ghcnd_id="USW00012839",
    ),
    "Chicago": Station(
        city="Chicago",
        icao="KORD",
        name="Chicago O'Hare Intl Airport",
        lat=41.9602,
        lon=-87.9316,
        timezone="America/Chicago",
        wunderground_url="https://www.wunderground.com/history/daily/us/il/chicago/KORD",
        ghcnd_id="USW00094846",
    ),
    "Dallas": Station(
        city="Dallas",
        icao="KDAL",
        name="Dallas Love Field",
        lat=32.8384,
        lon=-96.8358,
        timezone="America/Chicago",
        wunderground_url="https://www.wunderground.com/history/daily/us/tx/dallas/KDAL",
        ghcnd_id="USW00013960",
    ),
    "Houston": Station(
        city="Houston",
        icao="KHOU",
        name="Houston William P. Hobby Airport",
        lat=29.6459,
        lon=-95.2821,
        timezone="America/Chicago",
        wunderground_url="https://www.wunderground.com/history/daily/us/tx/houston/KHOU",
        ghcnd_id="USW00012918",
    ),
    "Los Angeles": Station(
        city="Los Angeles",
        icao="KLAX",
        name="Los Angeles Intl Airport",
        lat=33.9382,
        lon=-118.3866,
        timezone="America/Los_Angeles",
        wunderground_url="https://www.wunderground.com/history/daily/us/ca/los-angeles/KLAX",
        ghcnd_id="USW00023174",
    ),
    "San Francisco": Station(
        city="San Francisco",
        icao="KSFO",
        name="San Francisco Intl Airport",
        lat=37.6196,
        lon=-122.3656,
        timezone="America/Los_Angeles",
        wunderground_url="https://www.wunderground.com/history/daily/us/ca/san-francisco/KSFO",
        ghcnd_id="USW00023234",
    ),
    "Seattle": Station(
        city="Seattle",
        icao="KSEA",
        name="Seattle-Tacoma Intl Airport",
        lat=47.4447,
        lon=-122.3144,
        timezone="America/Los_Angeles",
        wunderground_url="https://www.wunderground.com/history/daily/us/wa/seatac/KSEA",
        ghcnd_id="USW00024233",
    ),
    "Austin": Station(
        city="Austin",
        icao="KAUS",
        name="Austin-Bergstrom Intl Airport",
        lat=30.1831,
        lon=-97.6799,
        timezone="America/Chicago",
        wunderground_url="https://www.wunderground.com/history/daily/us/tx/austin/KAUS",
        ghcnd_id="USW00013904",
    ),
    "Atlanta": Station(
        city="Atlanta",
        icao="KATL",
        name="Hartsfield-Jackson Atlanta Intl Airport",
        lat=33.6297,
        lon=-84.4422,
        timezone="America/New_York",
        wunderground_url="https://www.wunderground.com/history/daily/us/ga/atlanta/KATL",
        ghcnd_id="USW00013874",
    ),
    "Denver": Station(
        city="Denver",
        icao="KBKF",
        name="Buckley Space Force Base",
        lat=39.7167,
        lon=-104.7500,
        timezone="America/Denver",
        wunderground_url="https://www.wunderground.com/history/daily/us/co/aurora/KBKF",
        ghcnd_id="USW00023036",
    ),
}

# Source config
NWS_USER_AGENT = "rainmaker-bot (thomas.mueller@solvvision.de)"
OPENMETEO_MODELS = [
    "gfs_seamless",
    "ecmwf_ifs025",
    "icon_seamless",
    "gem_seamless",
    "meteofrance_seamless",
]
OPENMETEO_ENSEMBLE_MODELS = ["gfs_seamless", "ecmwf_ifs025", "icon_seamless"]
OPENMETEO_FORECAST_DAYS = 7
FRESHNESS_LIMIT_HOURS = 24

# Probability engine + ranking thresholds (uncalibrated; tune in Phase 4)
MIN_SIGMA_F = 1.5
CONFIDENCE_FLOOR = 0.90
MIN_SOURCES = 2
REPORTS_DIR = "reports"
DB_PATH = "rainmaker.db"

# Calibration (Phase 4)
MIN_CAL_SAMPLES = 30  # a cell needs this many pairs before its calibration is trusted
UNCALIBRATED_WIDEN = 1.25  # widen the raw spread when a cell is uncalibrated


def build_target(city: str, variable: Variable, local_date: date) -> Target:
    return Target(station=STATIONS[city], variable=variable, local_date=local_date)
