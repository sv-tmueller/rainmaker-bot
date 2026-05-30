# Polymarket weather markets - Phase 0 discovery findings

Phase 0 discovery spike for issue #3. Read-only enumeration of live Polymarket
US-city weather markets and their resolution rules. Snapshot taken 2026-05-30.

## Recommendation: GO (proceed to Phase 1), with one required spec amendment

Live US-city daily temperature markets exist in good numbers, the data we need
(metadata, resolution rules, prices, order book) is fully readable with no auth,
and the venue is a fit for the advisory bot. The decision gate passes.

One thing must change before Phase 1: the markets do not resolve against NWS or
NOAA. They resolve against **Weather Underground**, reading a **specific named
airport station** per city, in **whole degrees Fahrenheit**. The spec assumes
NWS/NOAA. See "Resolution source" below for what this means and the decision it
forces. We do not need to pivot off Polymarket, and Kalshi is not required.

## What is live (2026-05-30 snapshot)

Daily markets of the form "Highest temperature in <city> on <date>?" and
"Lowest temperature in <city> on <date>?". In the active "weather" tag on this
day: 17 US-city temperature events across 10 US cities, plus international cities
(London, Paris, Tokyo-area, Hong Kong, Shenzhen, Seoul, etc.) and longer-horizon
climate and natural-disaster markets that are out of scope.

US cities seen: NYC, Miami, Chicago, Dallas, Houston, Los Angeles, San Francisco,
Seattle, Austin, Atlanta. Mostly "highest", a few "lowest". New markets are
posted daily, so the same city recurs each day. This is enough breadth for
MVP 1.0 and supports the single-city start the spec calls for.

## Resolution rules (the part that settles the market)

Each city's market resolves against the daily extreme recorded at one fixed
airport station, sourced from the Weather Underground history page for that
station, rounded to whole degrees Fahrenheit.

| City          | Station                          | ICAO  | Weather Underground resolution page |
|---------------|----------------------------------|-------|-------------------------------------|
| NYC           | LaGuardia Airport                | KLGA  | wunderground.com/history/daily/us/ny/new-york-city/KLGA |
| Miami         | Miami Intl Airport               | KMIA  | .../us/fl/miami/KMIA |
| Chicago       | Chicago O'Hare Intl Airport      | KORD  | .../us/il/chicago/KORD |
| Dallas        | Dallas Love Field                | KDAL  | .../us/tx/dallas/KDAL |
| Houston       | Houston (William P. Hobby)       | KHOU  | .../us/tx/houston/KHOU |
| Los Angeles   | Los Angeles Intl Airport         | KLAX  | .../us/ca/los-angeles/KLAX |
| San Francisco | San Francisco Intl Airport       | KSFO  | .../us/ca/san-francisco/KSFO |
| Seattle       | Seattle-Tacoma Intl Airport      | KSEA  | .../us/wa/seatac/KSEA |
| Austin        | Austin-Bergstrom Intl Airport    | KAUS  | .../us/tx/austin/KAUS |
| Atlanta       | Hartsfield-Jackson Intl Airport  | KATL  | .../us/ga/atlanta/KATL |

Resolution rule text, verbatim essence (NYC example):

> This market will resolve to the temperature range that contains the highest
> temperature recorded at the LaGuardia Airport Station in degrees Fahrenheit on
> 29 May '26. The resolution source for this market will be information from
> Wunderground [KLGA history page]. The resolution source measures temperatures
> to whole degrees Fahrenheit, [so] this is the level of precision used when
> resolving. This market can not resolve until the first data point for the
> following date has been published. Revisions to temperatures recorded within
> this market's timeframe will be considered until the first datapoint for the
> following date has been published, after which any alterations will not be
> considered.

Two station gotchas that break a forecast if you assume the obvious airport:
- Dallas resolves on **Love Field (KDAL)**, not DFW.
- Houston resolves on **Hobby (KHOU)**, not Bush/IAH.

The station and rule live in each market's own description, so pin the exact
station per market from that text and re-read it on every run. Do not assume the
station from the city name.

## Outcome structure

Each event is a set of mutually exclusive temperature buckets, and each bucket is
its own binary Yes/No market with its own CLOB token pair. NYC "Highest temp on
May 30" had 11 buckets:

| bucket          | live Yes price |
|-----------------|----------------|
| 59 deg F or below | 0.0005 |
| 60-61           | 0.0005 |
| 62-63           | 0.001 |
| 64-65           | 0.001 |
| 66-67           | 0.001 |
| 68-69           | 0.004 |
| 70-71           | 0.984 |
| 72-73           | 0.0045 |
| 74-75           | 0.005 |
| 76-77           | 0.0045 |
| 78 deg F or higher | 0.0005 |

Interior buckets are 2 deg F wide and the tails are open-ended. The Yes prices
across buckets sum to roughly 1. The probability engine (Phase 2) must integrate
the forecast distribution over these rounded-degree bucket edges, including the
open tails.

## Price and order-book access (read-only, confirmed)

- Market metadata and current prices: Gamma API.
  `GET https://gamma-api.polymarket.com/events?closed=false&active=true&tag_slug=weather&limit=100&offset=N`
  Each market object carries `clobTokenIds`, `outcomes`, `outcomePrices`,
  `resolutionSource`, `endDate`, and the rule text in `description`.
- Live midpoint: `GET https://clob.polymarket.com/midpoint?token_id=<id>` returned
  `{"mid":"0.0005"}` (HTTP 200).
- Order book: `GET https://clob.polymarket.com/book?token_id=<id>` returned bids
  and asks arrays (HTTP 200, no auth). The sampled off-mode bucket had a thin book
  (0 bids, 61 asks, best ask 0.001 size 394), which matters for Phase 2.

No credentials, no wallet, no signing needed for any of this. Consistent with the
1.0 read-only constraint.

## Risks and decisions this raises

1. **Resolution source is Weather Underground, not NWS/NOAA (decision needed).**
   The spec and CLAUDE.md name NWS/NOAA and Open-Meteo. The markets settle on
   Weather Underground's reading of a named station. The stations are real
   ASOS/METAR airport sites, so NWS and Open-Meteo can still serve as forecast
   inputs for the same locations. But the quantity that settles the market is
   "what Weather Underground reports for station KXXX, in whole deg F". For the
   advisory MVP 1.0 we only forecast and compare to price, so we do not strictly
   need to read Weather Underground yet. For MVP 2.0 (settling against actuals)
   we do. Decision to make: settle 2.0 against Weather Underground (which has no
   free official API, so the history page would be scraped), or first prove that
   the NWS/NOAA daily extreme for these stations matches Weather Underground
   closely enough to settle against NOAA. This touches the "free sources only,
   no new source without an explicit decision" rule, so it is a maintainer call.

2. **Forecast the exact station, in whole deg F.** Tie NWS and Open-Meteo
   forecasts to each station's coordinates, produce a daily max (or min)
   distribution, round to whole deg F, then map onto the bucket edges. Wrong
   station (DFW vs Love Field) makes a perfect forecast worthless.

3. **Day-of markets are near-resolved; edge lives earlier.** The NYC day-of
   market already priced the mode bucket at 0.984. Real edge is more likely 1 to
   3 days ahead when the forecast distribution is still wide. Off-mode buckets
   have thin books. Phase 2 ranking, the confidence floor, and any slippage
   realism should account for this.

4. **Settlement timing.** Event `endDate` showed 12:00Z; resolution is gated on
   "first data point for the following date". Confirm trade-close vs resolution
   timing during Phase 1 so freshness limits and run timing are correct.

## Suggested Phase 1 starting point

NYC, highest temperature, station KLGA. It is present every day and is among the
more liquid US-city markets. Build the single-city forecast-to-bucket path end to
end, then widen to the other nine cities (using the station table above).

## How to reproduce

```sh
# 1. enumerate live weather events (US-city temp markets are a subset)
curl -s "https://gamma-api.polymarket.com/events?closed=false&active=true&tag_slug=weather&limit=100" \
  | jq -r '.[] | select(.title|test("temperature in (NYC|Chicago|Dallas|Miami|Houston|Los Angeles|San Francisco|Seattle|Austin|Atlanta)";"i")) | .title'

# 2. inspect one event's rule, buckets, prices, and token ids
curl -s "https://gamma-api.polymarket.com/events?closed=false&active=true&tag_slug=weather&limit=100" \
  | jq '.[] | select(.title|test("Highest temperature in NYC";"i")) | {title, description, buckets:[.markets[]|{.groupItemTitle, outcomePrices, clobTokenIds}]}'

# 3. confirm price + order book readable for a bucket's Yes token
TOK=<clobTokenIds[0] from step 2>
curl -s "https://clob.polymarket.com/midpoint?token_id=$TOK"
curl -s "https://clob.polymarket.com/book?token_id=$TOK" | jq '{bids:(.bids|length),asks:(.asks|length)}'
```
