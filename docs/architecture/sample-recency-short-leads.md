# Sample recency at short leads: measured, no change (#427)

Decision doc for #427, part of batch #424. Question: `FRESHNESS_LIMIT_HOURS`
is a flat 24h for all leads (config.py), so at 0-3 day horizons a 23h-old
sample and a 1h-old sample weigh the same in the pool. Does sample age at
fetch predict forecast error at short leads? If yes, tighten the gate per
lead; if no, leave the gate alone and ship the negative result. Verdict:
**no change**, flavor (b): age barely varies in pruned history, so there is
nothing to weight. No code touched on the live path.

## Method

Prod Supabase history, read-only export (scratch psycopg script, DSN from
env, never committed). Population: every `forecasts` row joined to a settled
temperature market (`markets` x `outcomes`, `actual_value IS NOT NULL`),
variables TMAX/TMIN, US cities only (international rows settle in Celsius and
would contaminate Fahrenheit errors; see the caveat below). Export sanity:
the SELECT count(*) cross-check matched the exported row count exactly
(65,866 rows total; 50,090 after the US-city filter).

Age proxy: `staleness_days = stored lead_time - fetch_lead`, where
`fetch_lead = settlement_date - station-local date of fetched_at` (converted
with zoneinfo using the config station timezone). Stored lead_time is computed
at parse from the issuance local date (nws.py), so the difference is how many
days old the issuance was at fetch. Caveats stated in advance in the sub-plan:

- Day resolution by necessity. Hour-scale age died with prune (prune.py keeps
  only the latest run per settled market, UTC day), so a within-day
  freshness distinction cannot be recovered from this history.
- Open-Meteo samples are stamped `issued_at = fetch wall-clock` since #401
  (openmeteo.py), so their staleness is structurally 0. The measurable
  population is effectively NWS. This is a property of the data, not a bug
  in the measurement.
- `outcomes.won` is never populated (grading lives on `predictions.won`);
  settledness is `actual_value` presence. The first export attempt filtered
  on `won IS NOT NULL` and correctly returned 0 rows until this was noticed.

Error metric: `error = mean(values_json) - actual_value` per forecast row
(ensemble rows hold member values; the group mean is taken), scored as MAE
of the absolute error, fresh (staleness 0) vs stale (>= 1 day), plus
Spearman rho between staleness_days and |error|. Units: international
cities (London, Paris, Helsinki, Sao Paulo) settle in Celsius while
`values_json` stores Fahrenheit, producing absurd ~48F apparent biases;
excluding them leaves sane US-city MAEs of 1.5-2.8F, matching the
sub-plan's expectation for short-lead MAE.

Exact SQL (read-only):

```sql
SELECT f.id, f.market_id, m.city, m.variable, f.source, f.model,
       f.values_json, f.lead_time, f.fetched_at,
       m.settlement_date, o.actual_value
FROM forecasts f
JOIN markets m ON m.id = f.market_id
JOIN outcomes o ON o.market_id = f.market_id
WHERE o.actual_value IS NOT NULL
  AND f.variable IN ('TMAX', 'TMIN')
  AND m.settlement_date IS NOT NULL;
```

## Pre-registered decision rule

Decided before looking, recorded here verbatim from the sub-plan: implement
recency awareness iff, in the NWS lead 0-2 band, n_stale >= 100, AND
(|rho| >= 0.10 with p < 0.01, OR MAE_stale >= 1.10 x MAE_fresh), AND the
direction is consistent across at least 2 of the 3 leads with adequate n.
Otherwise: no-change verdict. Two flavors, both legitimate closures:
(a) age varies and does not predict error (report the numbers); (b) age
barely varies in pruned history (n_stale < 100, report the stale fraction).

## Numbers

Staleness distribution (day resolution, US cities):

| Source | n | stale fraction (staleness 0) | histogram |
| --- | ---: | ---: | --- |
| nws | 5,442 | 99.3% fresh | 0: 5406, 1: 34, 5: 2 |
| open-meteo | 44,648 | 100% fresh | 0: 44648 |

Per-cell statistics (fresh = staleness 0, stale = >= 1 day):

| Source | fetch lead | n | n stale | stale % | Spearman rho | p | MAE fresh | MAE stale | stale/fresh |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| nws | 0 | 1,551 | 10 | 0.6% | -0.010 | 0.71 | 1.59 | 1.43 | 0.90 |
| nws | 1 | 2,759 | 14 | 0.5% | -0.021 | 0.26 | 1.82 | 1.65 | 0.90 |
| nws | 2 | 1,130 | 10 | 0.9% | -0.049 | 0.10 | 2.04 | 1.30 | 0.64 |
| open-meteo | 0 | 20,960 | 0 | 0.0% | - | - | 2.36 | - | - |
| open-meteo | 1 | 19,112 | 0 | 0.0% | - | - | 2.66 | - | - |
| open-meteo | 2 | 4,576 | 0 | 0.0% | - | - | 2.82 | - | - |

NWS by variable (leads 0-2): TMAX rho -0.016/-0.046/-0.084 (p 0.53/0.055/0.045),
TMIN rho +0.017/-0.009 (adequate-n leads only). Nothing reaches |rho| >= 0.10
with p < 0.01 anywhere.

Confound guard (per-city MAE, largest NWS cells): stale rows concentrate in
Seattle (lead 0: 9 stale, MAE 1.36 vs fresh 2.04; lead 1: 12 stale, 1.11 vs
1.86) and stray singles elsewhere (LA lead 1: 2 stale, 4.90 vs 1.95). Stale
rows are too few and too concentrated to estimate a per-city stale penalty;
the pooled stale MAE ratios below 1.0 are small-sample noise, not evidence
that stale samples are better.

## Verdict

No change, flavor (b): age barely varies in pruned history. The NWS lead 0-2
band has n_stale = 10/14/10 against a precondition of >= 100, and Open-Meteo
is structurally always-fresh. The decision rule's first conjunct fails by an
order of magnitude, so the correlation and MAE deltas are moot (both point
the wrong way anyway: every significant-leaning rho is negative and every
stale MAE ratio is below 1.0, opposite of the predicted degradation).

FRESHNESS_LIMIT_HOURS stays flat 24h for all leads. The 23h-vs-1h concern is
real in principle but unanswerable on this history: prune destroyed
within-day resolution, and the flat gate has never admitted a measurably
stale sample at short leads (worst observed staleness is 1 day on 0.5-0.9%
of NWS rows).

Recommendation logged on batch #424: persist `issued_at` in `forecasts`
prospectively (a store change, out of scope here, future batch). Today the
column does not exist, forcing the lead-arithmetic age proxy; with
`issued_at` stored, a future re-measurement could use hour-scale age on
unpruned intraday rows.

## Limitations

- Day-resolution age only (prune); the within-day question that motivated
  #427 cannot be answered from history at all.
- Open-Meteo contributes no variance in age by construction (#401 stamping).
- Stale cells are tiny (n 10-14) and city-concentrated (Seattle), so the
  fresh/stale MAE comparison is descriptive only.
- Measurement is under the old sigma definition; package 2's refit appendix
  re-states Brier under the new sigma. Neither invalidates the other
  (accepted on #424).
