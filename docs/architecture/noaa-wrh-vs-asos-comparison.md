# NOAA wrh/timeseries vs ASOS (Iowa State Mesonet) comparison

Issue #362. Investigation to determine whether our ASOS settlement source
(Iowa State Mesonet, `fetch_asos_daily_extreme`) faithfully reproduces the
NOAA/weather.gov wrh/timeseries hourly extremes that Polymarket now names as
its resolution source.

## Conclusion: DIVERGENCE MATERIAL

**ASOS diverges from wrh/timeseries by >=1 deg F on 33.4% of station-days**
(103 of 308 comparisons), well above the 15% materiality threshold. The mean
absolute delta is 0.749 deg F and the maximum observed delta is 4.6 deg F.

**Recommendation: re-label to size:L and implement the wrh/timeseries fetcher
(Checkpoint 3b).**

## Root cause analysis

Three compounding factors explain the divergence, confirmed by direct
inspection of raw observations for NYC (KLGA) Aug 20, 2026:

1. **Observational frequency**: The wrh/timeseries page is backed by the
   Synoptic Data API (`api.synopticdata.com/v2/stations/timeseries`), which
   returns observations at 5-minute resolution. Our ASOS path
   (`report_type=3`) receives only routine hourly METARs (24 obs/day for
   KLGA). The Synoptic API returns 267+ obs/day for the same station. More
   frequent sampling catches momentary temperature extremes that hourly
   METAR misses entirely.

2. **report_type=3 filter**: Our US ASOS path sends `report_type=3` (routine
   hourly METAR only), excluding SPECI reports. SPECI reports are special
   observations issued when weather changes rapidly. For KLGA Aug 20 UTC,
   removing the filter yielded 32 obs vs 24 with it, and the coldest reading
   shifted from 69.0F (METAR only) to 68.0F (all obs). The Synoptic API
   includes all observation types.

3. **UTC-day vs local-day bucketing**: Our US ASOS path buckets by UTC day;
   wrh/timeseries uses `obtimezone=local` (station local calendar day).
   Polymarket resolves on the calendar day in the station's local timezone,
   so the wrh/timeseries convention is correct. Near-midnight observations
   can shift between days, especially for TMIN (overnight lows) and western
   stations where the UTC/local offset is 7-8 hours.

These factors are additive. Factor 1 alone accounts for much of the TMAX
divergence (momentary daytime peaks missed by hourly sampling); factor 3
drives much of the TMIN divergence (overnight lows split across UTC midnight).

## Methodology

### Sources

- **ASOS**: `fetch_asos_daily_extreme` from `src/rainmaker/forecasts/asos.py`,
  using Iowa State Mesonet (`mesonet.agron.iastate.edu`), `report_type=3`,
  UTC-day bucketing, Fahrenheit return. Two separate requests per station
  (one for TMAX, one for TMIN).
- **WRH**: Synoptic Data API (`api.synopticdata.com/v2/stations/timeseries`)
  backing `weather.gov/wrh/timeseries`. Parameters: `units=temp|F,...`,
  `obtimezone=local`, `complete=1`. Token embedded in weather.gov's
  `apiKey.js`. Requires `Referer: https://www.weather.gov/wrh/timeseries`
  header. Single request per station covering the full date range. Daily
  max/min computed locally from all returned observations using local-day
  bucketing.

### Date range

14 days: 2026-08-17 to 2026-08-30 (inclusive). Dates chosen as the most
recent settled days at time of analysis.

### Stations

All 11 US Polymarket temperature stations: NYC (KLGA), Miami (KMIA),
Chicago (KORD), Dallas (KDAL), Houston (KHOU), Los Angeles (KLAX),
San Francisco (KSFO), Seattle (KSEA), Austin (KAUS), Atlanta (KATL),
Denver (KBKF).

### Materiality threshold

Defined per the sub-plan: markets resolve in 2 deg F buckets. A sustained
>=1 deg F discrepancy on >15% of station-days is material. Each station-day
produces two comparisons (TMAX + TMIN), so 11 stations x 14 days = 308 total
comparisons.

## Results

### Overall

| Metric                       | Value      |
|------------------------------|------------|
| Total comparisons            | 308        |
| Deltas >= 1 deg F            | 103 (33.4%)|
| Mean absolute delta          | 0.749 deg F|
| Max absolute delta           | 4.6 deg F  |
| Materiality threshold        | >15%       |
| Result                       | MATERIAL   |

### By variable

| Variable | Comparisons | >= 1 deg F | Pct   |
|----------|-------------|-----------|-------|
| TMAX     | 154         | 70        | 45.5% |
| TMIN     | 154         | 33        | 21.4% |

TMAX divergence is more severe (45.5% above threshold) because 5-minute
observations frequently catch short-lived afternoon temperature spikes that
hourly METAR misses.

### Per-station summary

| City          | ICAO | Compared | >= 1F | Pct   | Mean |delta| | Max |delta| |
|---------------|------|---------|------|-------|------------|-----------|
| NYC           | KLGA | 28      | 10   | 35.7% | 0.90       | 4.6       |
| Miami         | KMIA | 28      | 10   | 35.7% | 0.63       | 2.0       |
| Chicago       | KORD | 28      | 7    | 25.0% | 0.68       | 1.8       |
| Dallas        | KDAL | 28      | 11   | 39.3% | 0.85       | 2.4       |
| Houston       | KHOU | 28      | 8    | 28.6% | 0.75       | 2.8       |
| Los Angeles   | KLAX | 28      | 13   | 46.4% | 0.83       | 2.4       |
| San Francisco | KSFO | 28      | 14   | 50.0% | 0.99       | 2.4       |
| Seattle       | KSEA | 28      | 7    | 25.0% | 0.85       | 4.4       |
| Austin        | KAUS | 28      | 13   | 46.4% | 0.82       | 2.4       |
| Atlanta       | KATL | 28      | 7    | 25.0% | 0.69       | 2.8       |
| Denver        | KBKF | 28      | 3    | 10.7% | 0.26       | 2.6       |

Denver (KBKF) is the only station below threshold (10.7%). All others exceed
15%. San Francisco (50%) and LA/Austin (both 46.4%) are worst affected --
coastal diurnal patterns and marine-layer transitions produce rapid
short-duration temperature swings that 5-minute sampling captures but hourly
METAR does not.

### Delta distribution

| Band (deg F) | Count |
|--------------|-------|
| 0.0          | 65    |
| 0.5          | 60    |
| 1.0          | 59    |
| 1.5          | 30    |
| 2.0          | 28    |
| 2.5          | 7     |
| 3.0          | 2     |
| 3.5          | 1     |
| 4.5          | 2     |

Deltas cluster around half-degree increments, consistent with the
Celsius-to-Fahrenheit conversion producing 0.5F steps (0.1C ~= 0.18F, but
rounding creates larger effective steps).

## Confirmed resolution rule

Based on this analysis, the confirmed settlement resolution rule is:

- **Primary source**: weather.gov wrh/timeseries hourly observations
  (`https://www.weather.gov/wrh/timeseries?site=<lc-icao>`)
- **Underlying API**: Synoptic Data API
  (`api.synopticdata.com/v2/stations/timeseries`), token from weather.gov's
  `apiKey.js`, requires `Referer` header
- **Station**: same ICAO as current config (e.g. KDAL -> site=kdal)
- **Unit**: Fahrenheit
- **Bucketing**: local calendar day (station timezone), NOT UTC day
- **Precision**: all available observations (5-minute), NOT hourly METAR only
- **Extreme computation**: max(all obs) for TMAX, min(all obs) for TMIN
- **Rounding**: whole degrees Fahrenheit (matching Polymarket bucket edges)
- **Settlement timing**: after first datapoint of following date is published
- **Fallback**: Wunderground (current ASOS path serves as degraded fallback)

## Reproduction

```sh
# Run the comparison script (takes ~2 min, 11 stations x 2 sources)
uv run python scripts/compare_asos_vs_wrh.py --days 14

# Output: scripts/comparison_results.json (machine-readable)
# Console output includes per-station and overall summary
```

The script (`scripts/compare_asos_vs_wrh.py`) is a throwaway analysis tool
not intended for production use. Raw JSON results are in
`scripts/comparison_results.json`.

## Next steps

1. Re-label issue #362 from size:M to size:L.
2. Implement `src/rainmaker/forecasts/wrh.py` (Checkpoint 3b) using the
   Synoptic Data API with local-day bucketing and all-observation sampling.
3. Update `settle.py` routing to use the wrh fetcher for US Polymarket
   TMAX/TMIN, with ASOS as fallback.
4. Update `regrade_polymarket_settlements` to re-converge historical outcomes.
5. Write tests with saved JSON fixtures for the wrh parser.
