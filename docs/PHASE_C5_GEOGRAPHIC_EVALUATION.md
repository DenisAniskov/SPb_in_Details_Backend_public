# Phase C5 — Geographic Evaluation

**Date:** 2026-10-11 · Data: `phase_c5_geographic_evaluation.json`
(reproducible via `docs/phase_c5_acquisition/evaluate_scenic_geography.py`)

## 1. Method and honesty statement

The evaluation is a **geometric pre-filter measurement** (Haversine
corridor counting): for each frozen C2 scenario start in the five scenic
target districts it probes 8 deterministic 600 m stage directions and
counts verified dataset features inside the 400 m candidate corridor.

**This is NOT a MapKit-native metric.** Real pedestrian routing results,
real walking-time deltas and real scenic passage are device-checked
separately — **`NOT_TESTED`** in this phase. Haversine and native results
are never merged into one number.

## 2. Dataset coverage (verified features)

| District | Features | Embankments | Pedestrian streets | Squares |
| --- | ---: | ---: | ---: | ---: |
| Центральный | 76 | 8 | 36 | 32 |
| Петроградский | 33 | 26 | 3 | 4 |
| Василеостровский | 30 | 11 | 3 | 16 |
| Невский | **6** | 1 | 0 | 5 |
| Приморский | 42 | 27 | 5 | 10 |

## 3. Frozen-scenario probes (8 directions × 600 m each)

| Start | District | Corridor candidates (best direction) | Nearest feature, m |
| --- | --- | ---: | ---: |
| nevsky_phase_b | Невский | 0 | 2012 |
| nevsky_bolshevikov | Невский | 0 | 2811 |
| nevsky_dybenko | Невский | 0 | 3080 |
| nevsky_rybatskoye | Невский | 0 | 7921 |
| nevsky_elizarovskaya | Невский | 1 | 587 |
| primorsky_komendantsky | Приморский | 1 | 337 |
| primorsky_begovaya | Приморский | 0 | 1728 |
| primorsky_pionerskaya | Приморский | 0 | 1103 |

Summary: 8 scenarios evaluated; **2 (25 %)** have at least one probe
direction with corridor candidates; median candidates 0.

## 4. Honest reading

1. **The scenic dataset is central-weighted.** The verified public-way
   categories (embankments, pedestrian streets, squares) concentrate in
   the historic core. That is where scenic alternatives will genuinely
   appear.
2. **Residential districts are a real coverage gap** — especially Невский
   (6 features; nearest verified feature up to 7.9 km from some starts).
   In those areas the engine will honestly fall back to the normal route
   with the documented message; nothing is fabricated.
3. The probe is directional-sampling, not routing: a real walk may still
   find candidates in directions the probe missed, and vice versa — the
   native verification step remains the authority on any scenic claim.

## 5. Engine-level measurements (deterministic, synthetic router)

From the automated suite (`scenic_walk_engine_test.dart`,
`scenic_navigation_test.dart`): candidate shortlisting, verification
(coverage bands), detour/budget rejections, request accounting (2 requests
per accepted waypoint; shared 12-request cap), one-arrival-per-stop
invariant for A→W1→W2→B, reroute degradation. These are **synthetic**
metrics — labelled as such; they validate the ENGINE, not MapKit coverage.

## 6. Native MapKit-native metrics

| Metric | Status |
| --- | --- |
| Real scenic route geometry for SPb streets | **NOT_TESTED** |
| Real walking-time delta on device | **NOT_TESTED** |
| Real request latency/behaviour under the shared budget | **NOT_TESTED** |
| Simulator scenic walk on device | **NOT_TESTED** |

## 7. Areas with insufficient data (explicit)

- Невский район: only 6 verified features — scenic routing is
  non-functional there in practice.
- Приморский/Выборгский/Калининский residential quarters beyond the
  probed starts: no verified features within the corridor of most probes.
- Parks and gardens: excluded dataset-wide until entrances are verified
  (C3 audit) — park interiors cannot be claimed as scenic passages.

Dataset expansion recommendations are listed in
`PHASE_C5_SCENIC_DATA_AUDIT.md` §5.
