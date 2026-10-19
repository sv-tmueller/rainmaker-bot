# TMIN night-fragment blast radius: measured, zero observed (#450)

Appendix for #450 (implements #434, found by the #425 review). Question: the
former NWS TMIN period matcher returned the FIRST night period beginning on
the match date, so a post-midnight "Overnight" fragment could shadow the
wanted "Tonight" period (corruption mode) or make lead-0 return zero samples
silently (silent-loss mode). How often did that shape actually occur in prod?
Verdict: **zero observed shadowing events** in stored history; the flagged
disagreements in the exposure window are persistent regional model
differences, not matcher anomalies. The matcher fix ships anyway as
defense-in-depth (the shape is legal NWS output), but the live-path exposure
was nil. Historical context: #426 (merged 2026-10-19) already retargeted TMIN
sampling away from the periods product to the hourly calendar-day low, so the
matcher this appendix sizes is the pre-#426 code path.

## Method

Prod Supabase history, read-only SELECTs (scratch psycopg script, DSN from
env, never committed or echoed). Population: every `forecasts` row with
`variable = 'TMIN'` and `source IN ('nws', 'open-meteo')` (25,817 rows),
joined to `runs` for `started_at` and `markets` for `city`. Grouped by
`(run_id, market_id, lead_time)`; NWS value = median of the row's stored
values, Open-Meteo consensus = median over all OM models and ensemble members
in the group. Exposure window: runs started 00:00-06:00 UTC (the cron window
where post-midnight fragments are possible). Flag threshold: |NWS - OM|
greater than 2.0F, a conservative upper edge of the known #404 night-period
wedge (measured mean 1.0-1.7F, peaks 7F).

Prune caveat (same as #427): prune.py keeps only the latest run per settled
market per UTC day, so intraday history is sparse and the window holds few
groups. Counts below are over what survived pruning.

## Numbers

Window (runs started 00:00-06:00 UTC):

- 74 (run, market, lead) groups; 57 with an NWS value; 44 comparable
  (both sources present); 13 NWS-only.
- 11 of 44 flagged (25.0%), delta median -0.07F, mean -0.48F, |delta| p50
  1.45F, p90 2.90F, max 9.55F. By lead: 1 -> 6, 2 -> 5.

Control (all other hours): 3,512 groups, 1,149 comparable, 255 flagged
(22.2%), |delta| p50 1.10F, p90 2.85F, max 9.50F.

Era split at the #426 merge: essentially all history is pre-retarget
(3,585 groups pre, 1 post), so the window and control cohorts both describe
the old matcher era.

Flagged in-window groups by UTC hour (denominator): 03:00 -> 11 (of 44).
All other flagged groups sit outside the window (21:00-23:00 UTC dominate:
192 of 266 pre-retarget flags).

## Attribution: persistent disagreement, not shadowing

A shadowing event has a signature: the NWS value jumps relative to
neighboring NWS runs for the same market and lead (the sampled period
changed), while the underlying forecast did not. For each flagged in-window
group, the stored NWS value was compared against NWS values for the same
(market, lead) from neighboring runs within +/-36h:

- 11 of 11 flagged groups: value equals the neighboring-run median (jump
  0.00F in 8 of 11; +0.50F, -1.50F in the rest, all within the 2.0F wedge
  band and consistent with ordinary forecast updates).
- Conclusion: the flagged disagreements are persistent NWS-vs-OM regional
  model differences concentrated in Austin and Dallas (cold-season
  continental sites), present across the whole day, not period-selection
  artifacts. Zero transient flips attributable to fragment shadowing.

Silent-loss mode leaves no trace in the forecasts table (the row simply
does not exist), so its frequency is not measurable retroactively; the
window's 13 NWS-only groups lack an OM counterpart and cannot be attributed
either way.

## Decision

No remediation of historical rows is warranted (nothing corrupted was found).
The matcher fix in this issue is justified on correctness grounds for any
future consumer of the periods product, not on observed damage.
