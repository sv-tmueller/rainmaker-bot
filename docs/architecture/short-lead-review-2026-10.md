# Fresh-eyes short-lead accuracy review (2026-10)

Second-opinion pass over the 0-3 day lead path, per #425 (batch #424).
The repo was last reviewed wholesale on 2026-10-07 (issues #396-#407, all
merged); those findings are treated as adjudicated and are cited as closed,
not re-reported.

Reviewed in the order a datum flows: source fetch/stamping ->
freshness gating -> pooling/sigma fit -> calibration apply -> bucket
integration -> edge gate. Each module gets an explicit verdict.

| # | Module | Verdict |
|---|--------|---------|
| 1 | forecasts/nws.py | FINDINGS (1 medium, 1 low) |
| 2 | forecasts/openmeteo.py | CLEAN |
| 3 | forecasts/aggregate.py | FINDING (1 low) |
| 4 | probability/distribution.py | FINDINGS (1 medium suspicion, 1 low nit) |
| 5 | probability/calibration.py | FINDINGS (1 medium, 1 low) |
| 6 | probability/outcomes.py | CLEAN |
| 7 | ranking/edge.py | FINDING (1 medium) |

<!-- MODULES -->

## Module 1: forecasts/nws.py

TODO

## Module 2: forecasts/openmeteo.py

TODO

## Module 3: forecasts/aggregate.py

TODO

## Module 4: probability/distribution.py

TODO

## Module 5: probability/calibration.py

TODO

## Module 6: probability/outcomes.py

TODO

## Module 7: ranking/edge.py

TODO

<!-- DEFERRED -->

## Deferred threads

TODO

<!-- NITS -->

## Nits (deliberately unfiled)

TODO
