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
