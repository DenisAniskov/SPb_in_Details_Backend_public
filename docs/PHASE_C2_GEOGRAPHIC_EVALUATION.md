# Phase C2 geographic evaluation

10 October 2026. Exact base: `91f11840be20f86af0131ecbe7c8cbdb49883bce`.

Reproduce: `cd backend; python -m scripts.evaluate_phase_c2`.
[Complete rows](phase_c2_geographic_evaluation.json), [frozen starts](phase_c2_test_starts.json),
[C1 catalog](phase_c1_catalog_baseline.json), and frozen C1 service in
`scripts/phase_c1_smart_walk_baseline.py` preserve the comparison.

JSON also binds the rows to canonical catalog digests and algorithm source hashes
with UTF-8/LF normalization for portable Windows/Unix reproduction.

32 starts were frozen before selecting new POIs: five Nevsky, three each in
eight other priority districts and three central regression starts. The central
group includes the old center, Vasilyevsky and Petrogradsky starts. Each has
three unchanged budgets and four filters (all, nature, history/architecture,
mixed nature/history/art): **384 scenarios per cell; 1,536 total**.
All coordinates are synthetic public-area approximations, not surveyed entrances
or physical visits. Every request enables the C1 wizard's unusual preference.

Every main and alternative sequence was recomputed and checked for strict
categories, at most six unique stops, exact rounded-up seconds budget, and
identical output on reversed catalog input. Algorithm scores are not empirical
tourist satisfaction. First transfer, stop count/category variety and spare time
are descriptive quality proxies, not proof of street access.

**All figures below are Haversine/75 m/min plus editorial visits. They are not
pedestrian distances or native walking times. Real MapKit: NOT_TESTED.**

## Four-way feasibility

| Catalog + algorithm | Preset budget | Success / scenarios | Empty | One-stop successes | Median first preliminary transfer, min (successes only) |
|---|---:|---:|---:|---:|---:|
| 77 + C1 | 60 | 57 / 128 | 71 | 54 | 20.917 |
| 77 + C1 | 120 | 123 / 128 | 5 | 81 | 25.917 |
| 77 + C1 | 240 | 128 / 128 | 0 | 7 | 39.392 |
| 126 + C1 | 60 | 87 / 128 | 41 | 69 | 18.183 |
| 126 + C1 | 120 | 125 / 128 | 3 | 57 | 19.467 |
| 126 + C1 | 240 | 128 / 128 | 0 | 4 | 21.133 |
| 77 + v2 | 60 | 57 / 128 | 71 | 52 | 18.183 |
| 77 + v2 | 120 | 123 / 128 | 5 | 60 | 25.117 |
| 77 + v2 | 240 | 128 / 128 | 0 | 1 | 35.392 |
| 126 + v2 | 60 | 87 / 128 | 41 | 61 | 13.733 |
| 126 + v2 | 120 | 125 / 128 | 3 | 38 | 19.25 |
| 126 + v2 | 240 | 128 / 128 | 0 | 0 | 19.358 |

| Catalog + algorithm | All successes / 384 | Empty | Mean stops among successes | First stop within 1 km | Preliminary slack below 5 min |
|---|---:|---:|---:|---:|---:|
| 77 + C1 | 308 | 76 | 1.955 | 62 | 40 |
| 126 + C1 | 340 | 44 | 2.456 | 123 | 45 |
| 77 + v2 | 308 | 76 | 2.61 | 74 | 66 |
| 126 + v2 | 340 | 44 | 3.174 | 135 | 71 |

An unchanged catalog cannot gain new individually feasible first destinations
from ranking alone: C1 already finds a feasible singleton when one exists.
Catalog additions account for new preliminary successes. V2 changes ordering,
combination quality and offers alternate POI sets; its extra stops may consume
spare time. Native fallback must establish the actual result. Counts of low
slack are disclosed above, including any increase; no detour multiplier or
fabricated native success rate is used.

## District comparison

Successes / all scenarios in that district/preset, including failed filters.
Empty count is denominator minus numerator; Nevsky denominator is 20, other
groups 12. This measures these starts, not every neighborhood of the district.

| Scenario district/group | Budget | 77 + C1 | 126 + C1 | 77 + v2 | 126 + v2 |
|---|---:|---:|---:|---:|---:|
| Выборгский район | 60 | 6 / 12 | 6 / 12 | 6 / 12 | 6 / 12 |
| Выборгский район | 120 | 12 / 12 | 12 / 12 | 12 / 12 | 12 / 12 |
| Выборгский район | 240 | 12 / 12 | 12 / 12 | 12 / 12 | 12 / 12 |
| Калининский район | 60 | 8 / 12 | 8 / 12 | 8 / 12 | 8 / 12 |
| Калининский район | 120 | 12 / 12 | 12 / 12 | 12 / 12 | 12 / 12 |
| Калининский район | 240 | 12 / 12 | 12 / 12 | 12 / 12 | 12 / 12 |
| Кировский район | 60 | 3 / 12 | 3 / 12 | 3 / 12 | 3 / 12 |
| Кировский район | 120 | 12 / 12 | 12 / 12 | 12 / 12 | 12 / 12 |
| Кировский район | 240 | 12 / 12 | 12 / 12 | 12 / 12 | 12 / 12 |
| Красногвардейский район | 60 | 7 / 12 | 8 / 12 | 7 / 12 | 8 / 12 |
| Красногвардейский район | 120 | 12 / 12 | 12 / 12 | 12 / 12 | 12 / 12 |
| Красногвардейский район | 240 | 12 / 12 | 12 / 12 | 12 / 12 | 12 / 12 |
| Красносельский район | 60 | 3 / 12 | 3 / 12 | 3 / 12 | 3 / 12 |
| Красносельский район | 120 | 9 / 12 | 9 / 12 | 9 / 12 | 9 / 12 |
| Красносельский район | 240 | 12 / 12 | 12 / 12 | 12 / 12 | 12 / 12 |
| Московский район | 60 | 9 / 12 | 12 / 12 | 9 / 12 | 12 / 12 |
| Московский район | 120 | 12 / 12 | 12 / 12 | 12 / 12 | 12 / 12 |
| Московский район | 240 | 12 / 12 | 12 / 12 | 12 / 12 | 12 / 12 |
| Невский район | 60 | 3 / 20 | 20 / 20 | 3 / 20 | 20 / 20 |
| Невский район | 120 | 19 / 20 | 20 / 20 | 19 / 20 | 20 / 20 |
| Невский район | 240 | 20 / 20 | 20 / 20 | 20 / 20 | 20 / 20 |
| Приморский район | 60 | 3 / 12 | 3 / 12 | 3 / 12 | 3 / 12 |
| Приморский район | 120 | 12 / 12 | 12 / 12 | 12 / 12 | 12 / 12 |
| Приморский район | 240 | 12 / 12 | 12 / 12 | 12 / 12 | 12 / 12 |
| Фрунзенский район | 60 | 3 / 12 | 12 / 12 | 3 / 12 | 12 / 12 |
| Фрунзенский район | 120 | 11 / 12 | 12 / 12 | 11 / 12 | 12 / 12 |
| Фрунзенский район | 240 | 12 / 12 | 12 / 12 | 12 / 12 | 12 / 12 |
| Центральная часть города | 60 | 12 / 12 | 12 / 12 | 12 / 12 | 12 / 12 |
| Центральная часть города | 120 | 12 / 12 | 12 / 12 | 12 / 12 | 12 / 12 |
| Центральная часть города | 240 | 12 / 12 | 12 / 12 | 12 / 12 | 12 / 12 |

## Interest filters

| Filter | 77 + C1 | 126 + C1 | 77 + v2 | 126 + v2 |
|---|---:|---:|---:|---:|
| all | 81 / 96 | 87 / 96 | 81 / 96 | 87 / 96 |
| nature | 72 / 96 | 83 / 96 | 72 / 96 | 83 / 96 |
| history | 74 / 96 | 83 / 96 | 74 / 96 | 83 / 96 |
| mixed | 81 / 96 | 87 / 96 | 81 / 96 | 87 / 96 |

## Local catalog opportunity

Number of starts with at least one POI within each geometric band, out of 32.
These counts do not impose hard geographic limits or prove a route/visit fits.

| Haversine radius, m | Old 77 | Expanded 126 |
|---:|---:|---:|
| 500 | 5 | 12 |
| 1000 | 11 | 19 |
| 2000 | 23 | 28 |
| 3000 | 28 | 31 |
| 5000 | 31 | 32 |

## Nevsky case study (all categories)

Count/total preliminary minutes, and first preliminary transfer minutes.
Empty results retain their explicit reason in JSON. The user's physical start
is unknown. The Phase B reference and four additional starts are all included.

| Start | Budget | 77 + C1 (stops / total / first) | 126 + v2 (stops / total / first) |
|---|---:|---|---|
| nevsky_phase_b | 60 | 1 / 53 / 7.85 | 3 / 60 / 2.17 |
| nevsky_phase_b | 120 | 1 / 53 / 7.85 | 5 / 113 / 2.17 |
| nevsky_phase_b | 240 | 3 / 234 / 7.85 | 6 / 225 / 2.17 |
| nevsky_bolshevikov | 60 | empty | 1 / 38 / 12.65 |
| nevsky_bolshevikov | 120 | 2 / 116 / 36.23 | 4 / 120 / 16.02 |
| nevsky_bolshevikov | 240 | 3 / 240 / 102.35 | 6 / 239 / 16.02 |
| nevsky_dybenko | 60 | empty | 1 / 37 / 6.55 |
| nevsky_dybenko | 120 | 1 / 109 / 48.42 | 4 / 119 / 14.82 |
| nevsky_dybenko | 240 | 2 / 210 / 113.15 | 6 / 236 / 30.08 |
| nevsky_rybatskoye | 60 | empty | 2 / 55 / 6.63 |
| nevsky_rybatskoye | 120 | 1 / 116 / 70.97 | 4 / 119 / 6.63 |
| nevsky_rybatskoye | 240 | 2 / 239 / 151.67 | 6 / 223 / 6.63 |
| nevsky_elizarovskaya | 60 | empty | 3 / 55 / 9.05 |
| nevsky_elizarovskaya | 120 | 1 / 115 / 69.78 | 5 / 115 / 9.05 |
| nevsky_elizarovskaya | 240 | 2 / 210 / 69.78 | 6 / 235 / 9.05 |

## Computation and request volume

Measured Python wall times (one run, local Python3.14; not a device benchmark):

| Cell | Median ms | Max ms |
|---|---:|---:|
| 77 + C1 | 0.652 | 3.606 |
| 126 + C1 | 1.243 | 4.308 |
| 77 + v2 | 3.809 | 22.236 |
| 126 + v2 | 6.274 | 33.863 |

V2 searches at most 24 candidates, 32 states per depth and six depths. Python
evaluation makes zero native requests. Flutter allows at most three supplied
POI sets, six total route builds and twelve native leg requests; reservations
are conservative if a build fails early. The C1 removal-only worst case was
six builds of 6+5+4+3+2+1 legs (21 native requests). Fake tests exercise the new
cap, failure classes, alternate acceptance and stale-response rejection.

## Remaining gaps and native acceptance

Accepted additions concentrate on Nevsky, Frunzensky and Krasnogvardeysky.
Western/northern districts have no new verified additions in this phase:
source errors prevented defensible expansion there. Existing long visits and
restrictive history/nature filters still yield empty quick cases, as disclosed
in every district row. A positive preliminary result several kilometers away
is not evidence of useful near-start coverage.

All 1,536 real MapKit results, removed-stop counts and actual request counts are
`NOT_TESTED`/null. No Android device was attached. The
[physical checklist](PHASE_C2_ANDROID_CHECKLIST.md) requires real captured
route times, snapped access inspection and C1 GPS/navigation regressions.
Bounded search is approximate; unverified representative points, topology,
closures and SDK/network availability remain limitations.
