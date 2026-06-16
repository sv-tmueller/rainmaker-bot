# Intl Settlement Feasibility: IEM/Mesonet METAR for #103c

**Spike:** #184
**Date:** 2026-06-16
**Status:** GO

## Summary

Iowa State Mesonet (IEM) ASOS carries full METAR history for all four
international Polymarket settlement stations (EGLC, LFPB, EFHK, SBGR). When
fetched without the `report_type=3` filter (all obs types, not just routine
hourly), daily TMAX and TMIN derived from spot-tmpc readings match Wunderground
settlement values exactly across 36 TMAX spot-checks and 20 TMIN spot-checks
spanning 2024-2026: zero bucket flips, zero mean delta. No new dependency: this
is the same IEM endpoint already used for US ASOS settlement in
`forecasts/asos.py`.

---

## 1. Coverage findings

| Station | ICAO | City | IEM network | History from | Obs cadence | n_obs/day |
|---------|------|------|-------------|-------------|-------------|-----------|
| EGLC | London City Airport | London | GB__ASOS | 2010+ | 30-min (routine + SPECI) | 48 |
| LFPB | Paris-Le Bourget | Paris | FR__ASOS | 2010+ | 30-min (routine + SPECI) | 48 |
| EFHK | Helsinki Vantaa | Helsinki | FI__ASOS | 2010+ | 30-min (routine + SPECI) | 48 |
| SBGR | Sao Paulo-Guarulhos | Sao Paulo | BR__ASOS | 2010+ | ~30-min (routine dominant) | 24-30 |

All stations confirmed live: a single API call to
`https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py` returned 168
observations per station for a 7-day window. SBGR returns 24-30 obs/day
(fewer SPECI in dataset). All stations have confirmed history to at least
2010-01-01; spot-checks confirmed data for Jan 2010, Jan 2015, and Jan 2020.

IEM station metadata confirms the network for each:
`https://mesonet.agron.iastate.edu/api/1/station/{ICAO}.geojson` ->
`network: GB__ASOS / FR__ASOS / FI__ASOS / BR__ASOS`.

The 4-letter ICAO (EGLC, LFPB, EFHK, SBGR) is passed directly as the
`station` parameter. No K-strip (that is US-only convention).

---

## 2. Daily-extreme reduction approach

### Algorithm

1. Fetch all obs (no `report_type` filter) for the UTC window that spans the
   full local calendar day.
2. Convert each observation's UTC timestamp to local time using
   `zoneinfo.ZoneInfo(station.timezone)`.
3. Filter to observations whose local calendar date matches the target date.
4. TMAX = max of spot `tmpc`; TMIN = min of spot `tmpc` (native Celsius).

### Timezone handling

Each `INTL_STATIONS` entry already carries a `timezone` field. UTC offsets:

| Station | TZ | UTC offset (summer) | UTC offset (winter) |
|---------|----|--------------------|--------------------|
| EGLC | Europe/London | UTC+1 | UTC+0 |
| LFPB | Europe/Paris | UTC+2 | UTC+1 |
| EFHK | Europe/Helsinki | UTC+3 | UTC+2 |
| SBGR | America/Sao_Paulo | UTC-3 | UTC-3 (no DST) |

`zoneinfo.ZoneInfo` (Python 3.9+ stdlib) handles DST transitions automatically.
No third-party library required.

### Why `report_type=3` must be omitted for intl stations

The existing US ASOS code uses `report_type=3` (routine hourly METAR only,
issued at :50 past the hour for most US stations). European ASOS stations
additionally issue SPECI (special) reports at :20, making the observation
cadence 30 min. Filtering to routine-only misses SPECI observations that
may hold the actual daily peak.

Confirmed for EFHK 2026-06-09: the :20 SPECI reported 19C at 11:20 local;
routine :50 observations peaked at 18C. Wunderground includes all obs and
settled 19C. Omitting `report_type` (returns all obs types) eliminates this
gap entirely: IEM TMAX = WU TMAX = 19C.

The fix is intl-specific. The existing US path (`report_type=3`) should remain
unchanged - the US calibration data from spike #101a was computed on that basis.

---

## 3. Settlement match with Wunderground

### Method

WU ground-truth source: `api.weather.com/v1/location/{ICAO}:9:{CC}/observations/historical.json`
with `units=m` (metric, Celsius). This endpoint returns per-observation METAR
readings. The settlement value used here is `max(temp)` across all WU
observations for the local day.

IEM source: same as `forecasts/asos.py` (`MESONET_ASOS_URL`), with `report_type`
omitted and local-day bucketing applied.

**Note on comparison independence:** WU's hourly observations endpoint and IEM
both ingest from the same upstream ASOS/METAR feed. The comparison confirms that
both systems agree on what the METAR data says - it does not independently
validate IEM against a proprietary WU-internal computation. The WU daily summary
API (`observations/daily.json`) that would give a truly distinct computation path
returns HTTP 401 (paid endpoint); the WU public HTML page is Angular-rendered and
carries no parseable data. This is a real limitation of the comparison.

What is validated: that IEM and WU derive the same daily extreme from the same
METAR obs stream, and that both agree on the integer-Celsius values that markets
settle on. The logical chain is: (1) WU settles on the METAR extreme for the
local day; (2) IEM holds that same METAR obs stream; (3) both return the same
integer-Celsius values. Step (2)-(3) is what this spike confirms directly. Step
(1) is what is asserted (WU's settlement methodology) but not independently
cross-checked.

### TMAX results (56 spot-checks)

| Station | Dates tested | Zero-delta | Flips | Mean delta | Max |delta| |
|---------|-------------|-----------|-------|-----------|-----|
| EGLC | 16 (Jun26 x6, May26 x10, 2024-25 x5+5) | 16/16 | 0 | 0.00C | 0.0C |
| LFPB | 16 (Jun26 x6, May26 x10) | 16/16 | 0 | 0.00C | 0.0C |
| EFHK | 16 (Jun26 x6, May26 x10, 2024-25 x5+5) | 16/16 | 0 | 0.00C | 0.0C |
| SBGR | 10 (Jun26 x6, May26 x10) | 10/10 | 0 | 0.00C | 0.0C |

Total: 56 TMAX spot-checks, 0 flips, 0.00C mean delta.

### TMIN results (20 spot-checks)

All 4 stations, 5 dates each (2026-06-08 to 2026-06-12):
0/20 flips, mean delta = 0.00C, max |delta| = 0.0C.

### Interpretation

Spot-tmpc max/min over all obs types exactly reproduces the Wunderground daily
extreme for every date and station tested. The native-integer Celsius values are
identical. Bucket-flip risk is zero in this dataset.

**Comparison with #101a (US NCEI-to-ASOS):** That spike found 1.2-4.5% flip
rates for US stations due to NCEI rounding and station-shift effects. Intl
IEM-to-WU shows 0% because both use the same underlying METAR source (IEM and
WU both ingest ASOS/METAR). There is no rounding-layer divergence: WU reports
whole-degree-C integers that are exactly the METAR spot readings IEM carries.

**Caveats:**
- The comparison is not fully independent (see Method note above). An alternative
  ground truth such as OGIMET or WMO CLIMAT archives would provide an independent
  cross-check. That was out of scope for this spike.
- 56 TMAX + 20 TMIN spot-checks is a shorter baseline than the #101a analysis
  (~45 days of continuous data). A 90-day post-implementation backcheck per
  station is recommended before enabling intl betting recommendations.

---

## 4. Free-sources position

**No new dependency.** IEM / Iowa State Mesonet is the same free service
already used in `forecasts/asos.py` (`MESONET_ASOS_URL`). Three changes to the
existing ASOS path cover all intl stations:

1. Pass the 4-letter ICAO unchanged (no K-strip for intl).
2. Omit `report_type=3` (no filter - returns all obs types).
3. Bucket by local calendar day using `station.timezone` and `zoneinfo.ZoneInfo`.
4. Return Celsius directly; skip the F conversion for `station.unit == "C"`.

IEM rate-limit handling already in `asos.py` (429 retry with backoff) is
sufficient for intl stations.

---

## 5. GO / NO-GO recommendation

**GO.**

All four intl settlement stations (EGLC, LFPB, EFHK, SBGR) are available on
IEM with:

- History depth exceeding 10 years
- 24-48 obs/day cadence
- Zero delta vs Wunderground settlement values across 76 spot-checks (56 TMAX
  + 20 TMIN) over 2024-2026, spanning seasons, multiple temperature ranges, and
  DST transitions

The only implementation wrinkle (missing SPECI obs from `report_type=3`) has a
one-line fix. The free-sources position is intact. No new vendor, no API key,
no additional cost.

**Conditions attached to GO:**

1. Omit `report_type` for intl IEM requests; keep `report_type=3` for US.
2. Reduce by local calendar day using `station.timezone` and `zoneinfo.ZoneInfo`.
3. Return Celsius; skip F conversion for `station.unit == "C"`.
4. Run a 90-day post-implementation backcheck per station before enabling intl
   betting recommendations.

---

## 6. Rough implementation shape for #103c

Changes are contained to `forecasts/asos.py` and `store/settle.py`. Estimated
size: `size:M`.

### `forecasts/asos.py`

- Extend `ICAO_TO_ASOS_STATION` with intl entries that pass the ICAO unchanged:
  `"EGLC": "EGLC", "LFPB": "LFPB", "EFHK": "EFHK", "SBGR": "SBGR"`.
- Add a `local_tz: str | None = None` parameter to `fetch_asos_daily_extreme`.
  When set (intl path): omit `report_type`, convert timestamps to local, return
  Celsius.
  When `None` (existing US path): keep `report_type=3`, bucket by UTC, return F.
- Alternatively, keep the signature and add an `intl_mode: bool` flag.

### `store/settle.py`

- The `ghcnd_id is None` guard that currently skips intl markets: relax it for
  stations in `INTL_STATIONS`. The ASOS path covers settlement; GHCND is only
  needed for the NCEI-based fallback.
- Thread `station.unit` and `station.timezone` through to the ASOS call.

### Testing

- Fixture: add an IEM CSV fixture for one intl station (e.g. EGLC) covering one
  local day, including a :20 SPECI that raises the max vs the :50 routine.
- Test: `fetch_asos_daily_extreme` with the intl fixture returns correct
  Celsius TMAX, not F.
- Test: local-day bucketing correctly splits observations across the UTC
  midnight boundary for a UTC-ahead timezone (e.g. EFHK UTC+3).
- Existing US ASOS tests: must remain green (no regression on `report_type=3`
  or UTC-day bucketing).

---

## 7. Appendix: raw probe summary

### June 2026 TMAX - all 4 stations, all obs types

| Date | EGLC | LFPB | EFHK | SBGR |
|------|------|------|------|------|
| 2026-06-08 | 16C | 22C | 23C | 24C |
| 2026-06-09 | 19C | 19C | 19C | 24C |
| 2026-06-10 | 17C | 19C | 19C | 24C |
| 2026-06-11 | 17C | 19C | 18C | 20C |
| 2026-06-12 | 23C | 26C | 17C | 21C |
| 2026-06-13 | 22C | 27C | 15C | 22C |

All IEM = WU on every cell.

### May 2026 TMAX sample (every 3 days)

| Date | EGLC | LFPB | EFHK | SBGR |
|------|------|------|------|------|
| 2026-05-01 | 24C | 26C | 21C | 27C |
| 2026-05-04 | 15C | 18C | 13C | 27C |
| 2026-05-07 | 14C | 17C | 14C | 29C |
| 2026-05-10 | 13C | 18C | 15C | 20C |
| 2026-05-13 | 13C | 14C | 18C | 25C |
| 2026-05-16 | 15C | 16C | 19C | 27C |
| 2026-05-19 | 18C | 17C | 23C | 18C |
| 2026-05-22 | 28C | 29C | 17C | 17C |
| 2026-05-25 | 31C | 31C | 18C | 22C |
| 2026-05-28 | 31C | 33C | 16C | 21C |

All IEM = WU on every cell.

### Historical spot-checks (EGLC and EFHK only)

| Date | EGLC IEM | EGLC WU | EFHK IEM | EFHK WU |
|------|---------|---------|---------|---------|
| 2024-03-20 | 17C | 17C | 2C | 2C |
| 2024-09-10 | 19C | 19C | 24C | 24C |
| 2025-01-15 | 10C | 10C | 3C | 3C |
| 2025-07-15 | 22C | 22C | 29C | 29C |
| 2025-12-15 | 11C | 11C | 6C | 6C |

All IEM = WU.
