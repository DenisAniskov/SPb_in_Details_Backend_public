# Phase C3 Geographic Evaluation

**Method:** offline, reproducible, four-way comparison (`python -m scripts.evaluate_phase_c3`;
full data in `docs/phase_c3_geographic_evaluation.json`).

- Distances are Haversine geometry at 75 m/min plus editorial visit minutes —
  **preliminary estimates, not street distances and not native MapKit times**.
- Starts: the frozen C2 matrix of 32 synthetic points (unchanged) **plus** 10
  new exploratory synthetic points (2 per priority district), reported separately.
- Budgets 60/120/240; filters: no categories / nature / history / mixed.
- Every proposal re-validated (categories, ≤6 unique stops, exact seconds
  budget) and repeated with reversed input for determinism.

## Four-way comparison (42 starts × 3 budgets × 4 filters = 504 cases per cell)

| Cell | Success | Mean stops | First stop ≤1 km |
| --- | --- | ---: | ---: |
| 1. C2 catalog + C2 selection (`c2catalog_v2`) | 435 / 504 (86.3%) | 3.10 | 155 |
| 2. C3 catalog + C2 selection (`c3catalog_v2`) | **488 / 504 (96.8%)** | **3.45** | **303** |
| 3. C2 catalog + C3 routing points | 435 / 504 | 3.10 | 155 |
| 4. C3 catalog + C3 routing points | 488 / 504 | 3.45 | 303 |

**Cells 3 and 4 are degenerate copies of 1 and 2**: the C3 entrance audit found
zero verified public entrances, so zero routing points were applied. They are
computed by the real substitution mechanism (not skipped) to prove the pipeline,
but **no routing improvement is claimed** — and native MapKit validation for any
routing-point effect is `NOT_TESTED` in this phase.

## Frozen matrix vs exploratory north/west starts (cell 2)

| Dataset | Success | Mean stops |
| --- | --- | ---: |
| Frozen 32-point C2 matrix | 371 / 384 | 3.46 |
| 10 exploratory north/west points | 117 / 120 | 3.41 |

## Locality (POI count near each start)

| Catalog | ≤500 m (frozen / exploratory) | ≤1 km (frozen / exploratory) |
| --- | --- | --- |
| C2 (126) | 12 of 32 / 0 of 10 | 19 of 32 / 4 of 10 |
| C3 (173) | 15 of 32 / **9 of 10** | 27 of 32 / **9 of 10** |

Interpretation: for the new north/west exploratory starts, the C2 catalog left
6 of 10 starts with nothing within 1 km; the C3 catalog gives 9 of 10 starts a
POI within 500 m. On the frozen matrix the same comparison shows improvement
without regressing any district:

| District (C3 catalog, all budgets/filters) | Success |
| --- | ---: |
| Невский | 60 / 60 |
| Калининский | 59 / 60 |
| Кировский | 59 / 60 |
| Выборгский | 58 / 60 |
| Красносельский | 58 / 60 |
| Приморский | 54 / 60 |
| Московский / Фрунзенский / Центральная часть | 36 / 36 each |
| Красногвардейский | 32 / 36 |

## What is NOT proven

- Real pedestrian times, real route lengths and real availability come only
  from native MapKit on a device — `NOT_TESTED` (no physical device connected
  in this phase).
- Because no verified entrances exist, no claim is made that routes now end
  closer to real park gates.
- Test coordinates were never moved to inflate success: the 32-point matrix is
  the frozen C2 file (hash-recorded in the evaluation JSON); the 10 exploratory
  points are additional and reported separately.
