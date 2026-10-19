# Phase C11 — Quiet Engine model (c11_1)

## 1. What the number means

`road_noise_exposure_index` is a **dimensionless relative proxy in [0, 1]** of
how close a pedestrian route runs to mapped motor roads. Higher means closer
to busier roads. It is not dB, not dB(A), not a measured sound level and not a
health or sanitary assessment. The API also returns coverage and the unknown
part so a client cannot present the proxy over ground that was not checked.

A value of `0.0` means: inside the verified pilot, no mapped motor road is
within 120 m of any part of the route. It does **not** mean guaranteed silence:
OSM may be incomplete, and the proxy ignores many acoustic factors.

## 2. Inputs

* Real MapKit pedestrian geometry (lat/lon), already required by the existing
  environmental endpoints. The backend never routes or moves the walk.
* The hash-pinned central-SPb OSM road snapshot (3,595 usable roads).
* An optional `assessed_at` for report alignment. The model is static; the
  timestamp does not change the index.

## 3. Geometry method

1. Each route segment is split into chunks of at most 10 m **by length**, not by
   vertex count. Densifying the same polyline does not change the result.
2. A chunk is `known` only if both endpoints are inside the pilot bbox. A chunk
   crossing the boundary is conservatively `unknown`; no partial guess is made.
3. For each known chunk midpoint, the nearest mapped motor road is found in a
   local equirectangular projection (metres around the pilot centre).
4. Exposure of a chunk:
   `max over roads (class_weight × max(0, 1 − distance/120)²)`.
   The maximum prevents two parallel corridors from double-counting the same
   sound field; it also means a single crossing cannot make a whole walk noisy.
5. Route index = length-weighted mean over known chunks. Shares (`low`,
   `major`) are also length-weighted, never vertex-weighted.

## 4. Class weights

| Highway | Weight | Status |
| --- | --- | --- |
| motorway, trunk | 1.00 | engineering assumption |
| primary | 0.85 | engineering assumption |
| secondary | 0.70 | engineering assumption |
| tertiary | 0.55 | engineering assumption |
| residential | 0.30 | engineering assumption |
| unclassified | 0.25 | engineering assumption |
| living_street | 0.15 | engineering assumption |
| links | parent class | engineering assumption |
| tunnel=yes | ×0.15 | engineering assumption |
| tunnel=building_passage | ×0.10 | engineering assumption |

These are **not** measured coefficients and are not presented as scientifically
validated. They are transparent, versioned assumptions chosen so that a
primary road dominates a residential street at the same distance and a tunnel
surface is not treated as a street-level source. `access=no/private/customers/
permit` roads are excluded unless `motor_vehicle=yes`; `destination` remains.

## 5. Thresholds and interpretation

| Constant | Value | Use |
| --- | --- | --- |
| influence radius | 120 m | beyond it, contribution is zero |
| sample step | 10 m | length-based chunking |
| low-exposure threshold | 0.20 | interpretable share only |
| major-adjacent threshold | 0.50 | interpretable share only |
| quiet candidate minimum improvement | 0.05 | engineering threshold |
| comparable coverage | ≥0.80 and ≥ normal − 0.05 | refuses unfair comparison |
| detour limit | +10% / +180 s (whichever larger), +15% / +300 m (whichever larger) | preserves the walk budget |

`low_exposure_share` is labelled as "share of the checked path away from major
roads"; it is **not** a share of silence. The index is rounded to 4 decimals;
lengths are rounded to 0.1 m with `unknown = total − known` so the client's
consistency checks hold.

## 6. Status semantics

| Status | Meaning |
| --- | --- |
| `available` | all route length inside the pilot; index published |
| `partial` | some length inside; index only for that part; unknown reported |
| `outside_coverage` | no length inside; index null |
| `unavailable` | engine disabled, dataset missing/mismatched, or no usable data |

Coverage is `known / total`. `0% coverage` is **not** `0% noise`. Disabled
engine/dataset returns 200 with `unavailable` and a reason, never invented
numbers.

## 7. Sensitivity checks

The automated tests assert the qualitative and quantitative behaviour:

* route on a primary: index ≈0.85; on a residential: ≈0.30; on a living
  street: ≈0.15;
* primary at ~55 m: ≈0.25; beyond 120 m: exactly 0.0 with full coverage;
* a single primary crossing: index <0.10 and low-exposure share >0.80;
* tunnel primary: ≈0.1275;
* 2-point vs 101-point densification: identical length and index within 1e-6;
* outside pilot: null index, unknown = total; boundary crossing: `partial` and
  known + unknown = total;
* `access=no`: no contribution.

## 8. What would be needed for an acoustic claim

A calibrated source of traffic counts/speeds, terrain and building attenuation,
source height, receiver height, ground absorption, and field measurements at a
documented set of SPb locations. None of these is present. `FIELD_VALIDATED`
therefore remains false; the model is a planning proxy only.
