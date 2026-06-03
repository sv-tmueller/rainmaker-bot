# Phase 5 (slice 1): more US cities

Date: 2026-06-03. Issue: #8 (Phase 5). Status: approved design, pre-implementation.

Phase 5 in the MVP 1.0 spec is "breadth: more cities and market types". That
bundles independent pieces of very different size and risk, so it is split. This
spec covers only the first slice: widen the temperature-bucket pipeline from one
US city to all live US cities. Precipitation, lowest-temperature (TMIN), and any
standalone threshold-binary markets are out of scope and get their own specs.

## Why this is small

The discovery path is already registry-driven. `discover_markets` keeps only
events whose title city is a key in `STATIONS` (`_is_us_temp_event`), so the ~50
live international temperature markets and the non-weather noise (earthquakes,
pandemics) in the `weather` tag are already filtered out. `parse_market`
re-reads each market's description on every run and rejects it if the station
ICAO is not named there, so a wrong-station forecast cannot slip through.

The result: adding a city is adding a correct `Station` row. The forecast
engine, probability engine, ranking, and calibration all generalize with no
code change.

## Decision

Curated static registry, one hand-written `Station` row per city, verified
against the live resolution text. Rejected alternatives: extracting the station
from each market description at runtime (brittle free-text parsing, and it still
cannot supply the coordinates and GHCND id that forecasting and backfill need),
and adding cities without a GHCND id (the `Station` model requires it, and a
city that cannot be backfilled is half-enabled).

## Cities to add

Ten cities (NYC is already in the registry, giving eleven total). The key must
match the Polymarket title string exactly, because discovery looks up
`STATIONS[parse_city(title)]`.

| City key      | Resolution station          | ICAO  | Note                  |
|---------------|-----------------------------|-------|-----------------------|
| Miami         | Miami Intl                  | KMIA  |                       |
| Chicago       | O'Hare Intl                 | KORD  |                       |
| Dallas        | Love Field                  | KDAL  | not DFW               |
| Houston       | William P. Hobby            | KHOU  | not Bush/IAH          |
| Los Angeles   | Los Angeles Intl            | KLAX  | multi-word title key  |
| San Francisco | San Francisco Intl          | KSFO  | multi-word title key  |
| Seattle       | Seattle-Tacoma Intl         | KSEA  |                       |
| Austin        | Austin-Bergstrom Intl       | KAUS  |                       |
| Atlanta       | Hartsfield-Jackson Intl     | KATL  |                       |
| Denver        | Buckley Space Force Base    | KBKF  | Aurora CO, not KDEN   |

The three bold-noted stations are traps: the market resolves on a station that
is not the city's obvious primary airport. Dallas and Houston come from the
Phase 0 findings; Denver was confirmed by reading its live market description on
2026-06-03 (resolution page `.../us/co/aurora/KBKF`).

## Station fields and how each value is sourced

Each row needs `city, icao, name, lat, lon, timezone, wunderground_url,
ghcnd_id` (the existing `Station` model).

- `icao`, `name`, `wunderground_url`: from the resolution text (Phase 0 table
  plus the Denver probe).
- `lat`, `lon`: the resolution station's airport coordinates, not the city
  centre. The forecast must target the station that settles the market.
- `timezone`: the station's IANA zone.
- `ghcnd_id`: the NOAA NCEI GHCND id for that station. Each id is verified
  during implementation by a one-shot NCEI daily-summaries query; if it returns
  no TMAX data the id is wrong. No id is committed unverified.

## Calibration

Every new city starts uncalibrated. `run` already falls back to the widened raw
spread and labels the forecast `(uncalibrated)`, so the report is correct from
day one. Fitting a calibration cell per city is a follow-on
`rainmaker backfill --city <X>` run, not part of this slice.

## Components that do not change

`polymarket/client.py` (discovery + filtering), `polymarket/markets.py`
(parsing + the ICAO guard), the forecast sources, `probability/`, `ranking/`,
`report/`, and `store/`. Only `config.py` (the registry) gains rows. Tests are
added.

## Verification (TDD)

Write the failing test first in each case.

1. Registry completeness: every `Station` has all required fields populated and
   a timezone that `zoneinfo.ZoneInfo` accepts.
2. Discovery and parsing: a fixture event list mixing several US cities, a
   multi-word city (Los Angeles), a trap city (Dallas resolving on KDAL), and an
   international city (London). Assert discovery keeps exactly the US registry
   cities and maps each to the right station and ICAO.
3. Wrong-station guard: a market whose description omits its ICAO is rejected
   with a clear error.
4. GHCND ids: verified during implementation by a manual one-shot NCEI query
   per station, confirming each returns TMAX data for the expected station. This
   is a manual gate, not a committed test, because tests never hit live
   endpoints. Committed coverage is the registry test in (1) plus the existing
   fixture-based backfill tests from Phase 4.
5. The existing golden end-to-end test (NYC) stays green unchanged.

## Risks

- A `ghcnd_id` that is subtly wrong (a nearby station id) would fit calibration
  on the wrong actuals. Mitigation: verify each id returns data for the expected
  station, and rely on the per-run ICAO-in-description guard for the forecast
  side, which is the value that actually settles the market.
- Polymarket could change a resolution station. The per-run guard fails loud
  rather than resolving the wrong day, so this surfaces as a skipped market, not
  a silent bad forecast.
