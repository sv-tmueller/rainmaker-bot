from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict

Variable = Literal["TMAX", "TMIN", "PRCP"]


class Station(BaseModel):
    model_config = ConfigDict(frozen=True)

    city: str
    icao: str
    name: str
    lat: float
    lon: float
    timezone: str
    wunderground_url: str
    ghcnd_id: str | None = None  # NOAA NCEI GHCND station id; None for international advisory rows
    unit: Literal["F", "C"] = "F"  # settlement unit; US stations use F


class PrecipStation(BaseModel):
    """A monthly-precipitation settlement station.

    Parallel to Station but keyed on the climate-tool label the market rules
    name (resolution_name plays the guard role icao plays for temperature),
    because precip markets settle on the NOAA/NWS monthly figure, not a
    Wunderground daily reading.
    """

    model_config = ConfigDict(frozen=True)

    city: str
    resolution_name: str  # exact climate-tool label named in the market rules
    name: str
    lat: float
    lon: float
    timezone: str
    ghcnd_id: str  # NOAA NCEI GHCND station id, for GSOM monthly actuals


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

# Monthly precipitation settles on a different station than the temperature
# markets: NYC on Central Park (not LaGuardia), Seattle on the "Seattle City
# Area" threaded record at Sand Point WFO (not SeaTac). Both GHCND ids were
# confirmed against NCEI GSOM PRCP: Central Park May 2026 = 3.06 in, and Sand
# Point's GSOM monthly totals match the ACIS "Seattle City Area" figures.
PRECIP_STATIONS: dict[str, PrecipStation] = {
    "NYC": PrecipStation(
        city="NYC",
        resolution_name="Central Park NY",
        name="Central Park",
        lat=40.7790,
        lon=-73.9692,
        timezone="America/New_York",
        ghcnd_id="USW00094728",
    ),
    "Seattle": PrecipStation(
        city="Seattle",
        resolution_name="Seattle City Area",
        name="Seattle Sand Point WFO",
        lat=47.6872,
        lon=-122.2553,
        timezone="America/Los_Angeles",
        ghcnd_id="USW00094290",
    ),
}

# Kalshi (read-only second venue). Daily high-temp markets settle on the NWS
# Climatological Report (Daily) for a named station, which differs from the
# Polymarket/Wunderground station for NYC (Central Park, not LaGuardia) and
# Chicago (Midway, not O'Hare). The Station.name field holds the exact phrase the
# Kalshi rule text uses (the parser guards on it); wunderground_url holds the NWS
# CLI product URL (the recorder stores it as resolution_source).
KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"

KALSHI_HIGH_SERIES: dict[str, str] = {
    "NYC": "KXHIGHNY",
    "Chicago": "KXHIGHCHI",
    "Miami": "KXHIGHMIA",
    "Los Angeles": "KXHIGHLAX",
    "Austin": "KXHIGHAUS",
}

# Daily low-temp (TMIN) series. Settle on the same per-city NWS Climatological
# Report (Daily) as the high-temp series (one report carries both max and min),
# so they reuse KALSHI_STATIONS. The low-temp rule text names only the city, not
# the station, so the parser's station guard relaxes for TMIN (see markets.py).
KALSHI_LOW_SERIES: dict[str, str] = {
    "NYC": "KXLOWTNYC",
    "Chicago": "KXLOWTCHI",
    "Miami": "KXLOWTMIA",
    "Los Angeles": "KXLOWTLAX",
    "Austin": "KXLOWTAUS",
}

KALSHI_STATIONS: dict[str, Station] = {
    "NYC": Station(
        city="NYC",
        icao="KNYC",
        name="Central Park, New York",
        lat=40.7790,
        lon=-73.9692,
        timezone="America/New_York",
        wunderground_url="https://forecast.weather.gov/product.php?site=OKX&product=CLI&issuedby=NYC",
        ghcnd_id="USW00094728",  # confirmed in PRECIP_STATIONS (Central Park)
    ),
    "Chicago": Station(
        city="Chicago",
        icao="KMDW",
        name="Chicago Midway",
        lat=41.7860,
        lon=-87.7524,
        timezone="America/Chicago",
        wunderground_url="https://forecast.weather.gov/product.php?site=LOT&product=CLI&issuedby=MDW",
        ghcnd_id="USW00014819",  # Chicago Midway, confirmed against NCEI (backfill #105)
    ),
    "Miami": Station(
        city="Miami",
        icao="KMIA",
        name="Miami International Airport",
        lat=25.7881,
        lon=-80.3169,
        timezone="America/New_York",
        wunderground_url="https://forecast.weather.gov/product.php?site=MFL&product=CLI&issuedby=MIA",
        ghcnd_id="USW00012839",
    ),
    "Los Angeles": Station(
        city="Los Angeles",
        icao="KLAX",
        name="Los Angeles Airport",
        lat=33.9382,
        lon=-118.3866,
        timezone="America/Los_Angeles",
        wunderground_url="https://forecast.weather.gov/product.php?site=LOX&product=CLI&issuedby=LAX",
        ghcnd_id="USW00023174",
    ),
    "Austin": Station(
        city="Austin",
        icao="KAUS",
        name="Austin Bergstrom",
        lat=30.1831,
        lon=-97.6799,
        timezone="America/Chicago",
        wunderground_url="https://forecast.weather.gov/product.php?site=EWX&product=CLI&issuedby=AUS",
        ghcnd_id="USW00013904",
    ),
}

# Kalshi monthly rain series and their settlement stations. Like the temperature
# markets they settle on the per-city NWS Climatological Report, named in the rule
# text (NYC = Central Park, Chicago = CLIMDW / Midway, Denver = CLIDEN / Denver
# Intl). resolution_name is the substring the parser guards on. Every GHCND below
# is confirmed against NCEI station metadata.
KALSHI_RAIN_SERIES: dict[str, str] = {
    "NYC": "KXRAINNYCM",
    "Chicago": "KXRAINCHIM",
    "Denver": "KXRAINDENM",
}

KALSHI_PRECIP_STATIONS: dict[str, PrecipStation] = {
    "NYC": PrecipStation(
        city="NYC",
        resolution_name="Central Park",
        name="Central Park",
        lat=40.7790,
        lon=-73.9692,
        timezone="America/New_York",
        ghcnd_id="USW00094728",  # confirmed in PRECIP_STATIONS (Central Park)
    ),
    "Chicago": PrecipStation(
        city="Chicago",
        resolution_name="CLIMDW",
        name="Chicago Midway",
        lat=41.7860,
        lon=-87.7524,
        timezone="America/Chicago",
        ghcnd_id="USW00014819",  # Chicago Midway, confirmed against NCEI (backfill #105)
    ),
    "Denver": PrecipStation(
        city="Denver",
        resolution_name="CLIDEN",
        name="Denver International Airport",
        lat=39.8467,
        lon=-104.6562,
        timezone="America/Denver",
        ghcnd_id="USW00003017",  # DENVER INTERNATIONAL AIRPORT (NCEI), the CLIDEN station
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
MIN_SIGMA_C = MIN_SIGMA_F * 5 / 9  # same physical spread expressed in Celsius (~0.833)
# 0.80, relaxed from 0.90: the spread-adjusted P/L backtest showed the higher
# floor suppressed profitable bets (see docs/architecture/recommendation-gate.md, #58).
CONFIDENCE_FLOOR = 0.80
MIN_SOURCES = 2
MIN_EDGE = 0.05
PRECIP_VAR_FLOOR = 0.01  # in^2: variance floor for the monthly-total gamma (~0.1in std)
PRECIP_CLIMATOLOGY_YEARS = 20  # prior years of NCEI history for the climatology tail
REPORTS_DIR = "reports"
DB_PATH = "rainmaker.db"

# Calibration (Phase 4)
MIN_CAL_SAMPLES = 30  # a cell needs this many pairs before its calibration is trusted
UNCALIBRATED_WIDEN = 1.25  # widen the raw spread when a cell is uncalibrated
BACKFILL_DAYS = 45  # default history window for calibration; literature optimum ~30-45 days

# Meteorological season start months: DJF=Dec, MAM=Mar, JJA=Jun, SON=Sep.
_SEASON_START_MONTHS = {
    12: 12,
    1: 12,
    2: 12,  # DJF (winter)
    3: 3,
    4: 3,
    5: 3,  # MAM (spring)
    6: 6,
    7: 6,
    8: 6,  # JJA (summer)
    9: 9,
    10: 9,
    11: 9,  # SON (autumn)
}


def season_start_month(d: date) -> tuple[int, int]:
    """Return (year, month) for the first day of the meteorological season containing d."""
    start_month = _SEASON_START_MONTHS[d.month]
    # DJF: if we are in Jan or Feb, the season started in December of the prior year.
    if start_month == 12 and d.month != 12:
        return d.year - 1, 12
    return d.year, start_month


def build_target(city: str, variable: Variable, local_date: date) -> Target:
    return Target(station=STATIONS[city], variable=variable, local_date=local_date)
