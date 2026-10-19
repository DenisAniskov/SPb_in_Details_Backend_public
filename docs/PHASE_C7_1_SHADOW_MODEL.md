# Phase C7.1 — Shadow Model

Status: **UNIT_TESTED** (analytic cases), **DATA_VERIFIED** (real footprints),
**FIELD_VERIFIED: no**.

## 1. The model, stated plainly

Each building is a vertical prism over its OSM footprint, standing on flat
ground, lit by parallel rays. The ground shadow is the Minkowski sum of the
footprint polygon and the horizontal displacement

```
L = H / tan(α)          displacement = −L·(sin az, cos az)
```

with `H` the provenanced height and `α` the solar elevation. Roofs are flat;
the Sun is a point; edges are hard.

## 2. Courtyards are modelled (a fix over v1)

SPb buildings routinely enclose yards, and OSM maps them as multipolygon
relations with interior rings. The v1 prototype swept only the exterior ring,
which silently treated a courtyard as building mass. The C7.1 model sweeps
interior rings too, using the identity

```
MS(P, s) = P ∪ (P+s) ∪ ⋃_edges MS(edge, s)
```

which holds for multiply-connected polygons. A yard is therefore shaded
exactly where a sun ray from it meets building mass, and stays sunlit where it
does not — verified analytically (40 × 40 m block, 20 × 20 m courtyard,
H = 5 m, sun 45°/180°: the shaded part of the yard is the 20 × 5 m band
against its south wall; the v1 model added 300 m² of false shade).

The Winter Palace is stored with its six real courtyards and its declared
22 m height.

## 3. Low sun, tall buildings, and the honest guards

| Guard | Value | Consequence |
| --- | --- | --- |
| `min_solar_elevation_deg` | 2° | below it no estimate is produced (`sun_below_horizon`); night never becomes "100 % shade" |
| `max_shadow_length_m` | 1000 m | longer shadows are capped, and the cap is counted: the estimate can only be too optimistic |
| `UNKNOWN_HEIGHT_ENVELOPE_M` | 40 m | ground that a height-less building COULD shade is `unknown`, never sun |

The 40 m envelope is a deliberately generous plausibility height for the
historic centre (median mapped height 15–25 m). It can only convert sun into
unknown — never shade into sun — so the model errs toward admitting ignorance.

## 4. Not modelled (surfaced in every response)

Roof shapes (gables, towers, spires), terrain relief, trees and canopies,
bridges and arcades as such, reflective glass, penumbra, and multi-part
building heights (`building:part` polygons are excluded and counted, 684 in
the pilot). Reflections and indirect light are not modelled.

## 5. Geometry honesty

* Distances are geodesic (haversine); the planar frame is used only for
  polygon operations, and each segment's shaded fraction is applied to its
  geodesic length (no planar-distance reporting).
* Vertex count is never a distance or time proxy.
* A slice is a real piece of the published polyline — every interior vertex of
  the route inside the slice is kept (C7.1.1), so a turn inside a slice is
  classified and drawn on the walked geometry, never as a chord; overlapping
  shadows are unioned, so a point is never counted twice.
* WHERE a band sits is reported as MEASURED intervals (`shaded_spans`,
  `unknown_spans`, metres along the slice), not as a length a client would have
  to distribute by assumption. `unknown` is the envelope MINUS the confirmed
  shadow, so overlapping metres are attributed once and genuinely sunlit
  ground is never moved into `unknown` by an overlap (C7.1.1).
* Invalid footprints (self-touching, degenerate) are repaired or excluded,
  counted in the audit, and never silently replaced.

## 6. Verification

* `tests/test_c7_1_courtyard_shadow.py` — four analytic cases (yard band,
  boundary sampling, v1-regression area, geodesic consistency).
* Ported v1 shadow/intersection tests (38) run unmodified.
* Real-data scenario tests (`test_c7_1_time_aware.py`) show the expected
  physics: an enclosed arcade shaded at 09:00/14:00/19:00, an open square
  sunlit at midday and reached only by the low evening sun, dense blocks
  darker in the morning than at midday.
