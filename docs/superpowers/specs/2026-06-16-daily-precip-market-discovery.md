# Daily-Binary Precipitation Market Discovery

**Date:** 2026-06-16
**Issue:** #192 (spike) | **Gates:** #87 (build)
**Verdict:** GO - scoped to Kalshi NYC as beachhead; Polymarket opportunistic/dormant

---

## Scope

Phase-0 read-only scan to confirm whether live, tradeable daily-binary precipitation
markets ("Will it rain in `<city>` on `<date>`?") exist on Polymarket (Gamma API) and/or
Kalshi right now. Distinct from the monthly-total bracket markets we already support.

---

## Polymarket (Gamma API)

### Active events scanned

177 active `weather`-tagged events fetched (6 pages, `closed=false, active=true`). Every
precipitation event found was the **monthly-total bracket** form already supported:

| Event title | ID | Type |
|---|---|---|
| Precipitation in NYC in June? | 531291 | Monthly bracket |
| Precipitation in Seattle in June? | 531299 | Monthly bracket |
| Precipitation in London in June? | 531307 | Monthly bracket |
| Precipitation in Seoul in June? | 531410 | Monthly bracket |
| Precipitation in Hong Kong in June? | 531426 | Monthly bracket |

**Zero live daily-binary rain markets on Polymarket as of 2026-06-16.**

### Closed events (historical)

Scanned 2000 closed `weather`-tagged events ordered by end date descending.
Three daily-binary rain events found, all from a two-day experiment on 8-10 June 2026:

| Event | ID | Cities | End date | Created | Total volume |
|---|---|---|---|---|---|
| Where will it rain on June 10? | 576682 | Atlanta, San Francisco, Denver, Boston, Dallas | 2026-06-10 | 2026-06-09 19:54Z | $1,400 |
| Will it rain in Central Park on June 9? | 573453 | NYC (Central Park) | 2026-06-09 | 2026-06-08 21:28Z | $288 |
| Where will it rain on June 9? | 573484 | Denver, San Francisco, Atlanta, Dallas, Boston | 2026-06-09 | 2026-06-08 22:04Z | $2,048 |

Public links (closed):
- `https://polymarket.com/event/where-will-it-rain-on-june-10`
- `https://polymarket.com/event/will-it-rain-in-central-park-on-june-9`
- `https://polymarket.com/event/where-will-it-rain-on-june-9`

### Polymarket resolution rule (from event descriptions)

Source: NOAA via `https://www.weather.gov/wrh/climate`

- **Station:** city-specific NOAA weather station (Central Park for NYC, NWS climatological reports for other cities)
- **Threshold:** total precipitation strictly greater than 0 inches (i.e., >= 0.01" in practice)
- **Trace exclusion:** "Trace rain, specified by a 'T' in the named column, will **not** qualify toward a Yes resolution" - trace resolves **No**
- **Precision:** 2 decimal places
- **Settlement timing:** resolved once NOAA's daily data for the specified date is finalized

### Polymarket assessment

The June 9-10 events appear to be a one-off pilot: created the day before settlement, closed the next day, no new markets have appeared since. With zero live markets today and no evidence of an ongoing series, Polymarket is **dormant/opportunistic** for daily-binary rain.

---

## Kalshi

### Daily rain series enumeration

Queried all 281 Kalshi "Climate and Weather" category series and filtered to
`frequency == "daily"` with "rain" or "precip" in the title. Six series found:

| Ticker | Title | Settlement source | Open markets today |
|---|---|---|---|
| KXRAINNYC | NYC rain | NWS Climatological Report (OKX) | **2 live** |
| RAINNYC | NYC rain | National Weather Service | 0 |
| KXRAINDNYC | Daily Rain - NYC | National Weather Service | 0 |
| KXRAINSEA | Seattle rain | National Weather Service | 0 |
| RAINSEA | Seattle rain | National Weather Service | 0 |
| KXRAIND | Rain Daily | National Weather Service | 0 |

**Only KXRAINNYC has live open markets.** The other five series exist but have no
open markets (and in most cases no historical settled markets - likely
inactive/deprecated scaffolding).

### Live KXRAINNYC markets (as of 2026-06-16)

| Ticker | Question | Date | Yes ask | Yes bid | No ask | No bid | Volume (total) | Open interest | Closes |
|---|---|---|---|---|---|---|---|---|---|
| KXRAINNYC-26JUN16-T0 | Will it rain in NYC on Tuesday (June 16)? | 2026-06-16 | $0.01 | $0.00 | $1.00 | $0.99 | $4,673 | $3,525 | 2026-06-17 03:59Z |
| KXRAINNYC-26JUN17-T0 | Will it rain in NYC on Wednesday (June 17)? | 2026-06-17 | $0.89 | $0.84 | $0.16 | $0.11 | $850 | $824 | 2026-06-18 03:59Z |

Public link (series): `https://kalshi.com/markets/kxrainnyc`

### Kalshi resolution rule (from `rules_primary` and `rules_secondary` fields)

Series: KXRAINNYC | Contract terms: `https://kalshi-public-docs.s3.amazonaws.com/contract_terms/RAINNYC.pdf`

- **Station:** Central Park, New York (NWS Climatological Report, site=OKX)
- **Threshold:** precipitation "strictly greater than 0" inches
- **Trace rule:** "If the Expiration Value is T (when the target is 0) for Trace... then the market resolves to **Yes**" - trace resolves **Yes** (opposite of Polymarket)
- **Settlement timing:** first 10:00 AM ET following the release of the NWS Climatological Report for the target date, with a 7-day backstop fallback to the NWS time series
- **Fallback source:** `https://www.weather.gov/wrh/timeseries?site=knyc`

Recent settled markets confirm the series is active:
- KXRAINNYC-26JUN15-T0 (June 15): settled Yes
- KXRAINNYC-26JUN14-T0 (June 14): settled Yes
- KXRAINNYC-26JUN13-T0 (June 13): settled No

### Kalshi liquidity assessment

KXRAINNYC is thin by the standards of the temperature markets, but real:

- June 16 market: $4,673 total volume, $3,525 open interest - markets settle same day, so volume accumulates fast intraday
- June 17 market: $850 volume (24h), $824 open interest with a 5-cent yes spread ($0.84/$0.89)
- Markets open roughly 14:00 UTC the day before, close at 03:59Z the following day (11:59 PM ET day-of)

At advisory scale (human reviewing and placing bets manually), this is workable.

---

## Key divergence between venues

| Aspect | Polymarket | Kalshi (KXRAINNYC) |
|---|---|---|
| Current status | No live markets | 2 live markets |
| City coverage | Multi-city (ad hoc pilot) | NYC only |
| Station | NOAA WRH (city-specific) | Central Park / NWS CLI (OKX) |
| Threshold | > 0 inches (>= 0.01" in practice) | > 0 inches |
| Trace ("T") rule | Trace = **No** | Trace = **Yes** |
| Settlement timing | After NOAA finalizes | First 10 AM ET after CLI |
| Market cadence | Experimental, one-off (June 8-10) | Daily series, auto-created |

The trace-rule divergence is critical: on a trace day the two venues settle opposite
results. Any #87 build must encode the resolution rule per venue separately. A
single `daily_precip_settles(precip_inches)` function cannot cover both.

---

## GO / NO-GO recommendation

**GO - scoped to Kalshi NYC as beachhead, Polymarket opportunistic/dormant.**

Evidence:

1. KXRAINNYC is a live, recurring daily binary market on exactly the quantity
   of interest (will precip > 0 at Central Park). It settles daily against the
   NWS CLI report, the same source the existing settle path already reads.
2. The series has settled markets from at least June 13 onward, is auto-creating
   daily, and has $800-4,700 per-day volume.
3. The threshold (> 0") is a binary condition, not a bracket. Forecasting
   P(precip > 0) from NWS QPF and Open-Meteo precip probability is well-defined.
4. Polymarket had a 2-day experiment with identical resolution logic (different
   trace rule) but is currently dormant. Build against Kalshi; add Polymarket
   discovery opportunistically when/if a series reappears.

Constraints the build must honor:

- Single city at launch (NYC). No other daily rain series is live on either venue.
- Trace divergence: Kalshi trace = Yes; any future Polymarket series trace = No.
  Encode per-venue, not shared.
- Thin liquidity relative to temperature markets - enforce the same minimum-edge
  gate (#87 should inherit the existing `MIN_EDGE` floor).
- Settlement source: NWS Climatological Report (OKX product page) is the primary;
  the NWS hourly time series (`knyc`) is Kalshi's fallback and should be the
  fallback for settlement too.

---

## What was not found

- No daily-binary rain markets for Chicago, Dallas, Atlanta, Denver, Seattle,
  Miami, Houston, Boston, LA, San Francisco, or Austin on either venue today.
- The KXRAINSEA (Seattle) series exists on Kalshi but has zero open and zero
  settled markets - likely created but never activated.
- No Kalshi daily series for cities other than NYC appears to be operational.
