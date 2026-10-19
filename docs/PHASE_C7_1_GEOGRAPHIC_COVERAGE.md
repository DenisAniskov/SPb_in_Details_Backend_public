# Phase C7.1 — Geographic Coverage

Status: **DATA_VERIFIED** for the mapped pilot zones; the rest of the city is
**NOT COVERED** and is reported as `unknown`, never as sun.

## 1. What is covered

Six bounded zones (≈0.85 km² each, ≈5.1 km² total) inside Saint Petersburg,
chosen to include different urban conditions (§6.1). Measured from the
committed dataset (`docs/PHASE_C7_1_COVERAGE_AUDIT.json`).

| Zone | Buildings | Usable height | No height | Multipolygon | Invalid | Vertices |
| --- | --- | --- | --- | --- | --- | --- |
| centre_palace | 204 | 138 | 66 | 88 | 9 | 3 642 |
| centre_nevsky | 240 | 149 | 91 | 72 | 12 | 4 123 |
| admiralteysky | 350 | 244 | 106 | 135 | 11 | 5 110 |
| petrogradsky | 325 | 230 | 95 | 77 | 8 | 5 238 |
| vasileostrovsky | 172 | 105 | 67 | 25 | 3 | 2 657 |
| control_kupchino | 118 | 86 | 32 | 10 | 1 | 1 597 |
| **total** | **1 409** | **952** | **457** | **407** | **5** | **22 367** |

(In the table, "invalid" counts relation-level failures such as member ways
outside the box; the 5 stored invalid geometries are degenerate rings kept for
audit and never served.)

Excluded categories are counted, not hidden: 684 `building:part` polygons,
37 `building=roof` features, 39 relations whose members lie outside the box.

## 2. What is NOT covered — and how the API behaves

* Anything outside the six boxes: the analysis returns
  `status: "outside_coverage"` when the whole walk is outside, or
  `status: "partial"` with `outside_coverage_meters` when only part is inside.
  The uncovered metres are counted as **`unknown`**, never as sun.
* A mapped zone is **not** a statement of OSM completeness. An empty building
  answer for a route area yields `status: "unavailable"` (no estimate) rather
  than a 100 % sun result — the v1 rule, kept deliberately.
* The dataset version and the pilot zone list travel with every response
  (`dataset_version`, `coverage_metrics.dataset_zones_touched`,
  `dataset_provenance`), so a client can always tell which ground is mapped.

## 3. Reproducible control scenarios (§19)

Three real OSM pedestrian ways, fixed in `tests/fixtures/c7_1_scenarios.json`
with their source zone, way id and character:

| Scenario | Real geometry | Character (verified) |
| --- | --- | --- |
| `gostiny_dvor_arcade` | way 452713489, 998 m, closed loop, zone `centre_nevsky` | enclosed arcade of Gostiny Dvor: shaded at any time of day |
| `palace_square_open` | way 217399816, 644 m, `living_street`, zone `centre_palace` | open square: sunlit at midday, reached by the low evening sun |
| `isaac_blocks` | way 148613750, 690 m, `footway`, zone `admiralteysky` | dense blocks: more shade in the morning than at midday |

Model output at the recorded dataset version (1 July 2026, MSK):

| Scenario | 09:00 | 14:00 | 19:00 |
| --- | --- | --- | --- |
| `gostiny_dvor_arcade` | 97.9 % | 97.9 % | 97.9 % |
| `palace_square_open` | 0 % | 0 % | 30.6 % |
| `isaac_blocks` | 55.1 % | 34.6 % | 61.6 % |

These numbers are **regression anchors**, not field measurements: no field
validation has been performed (§19.1 — programme prepared, execution
`NOT_TESTED`).

## 4. Performance measured on this dataset (§20)

Measured on the development machine with the committed dataset (274 buildings
in the centre box):

| Operation | Measured |
| --- | --- |
| `footprints_in_bbox` (R-tree, 0.85 km² area) | 12 ms |
| 194 shadows built (Minkowski sums) | 346 ms |
| union of 194 shadow polygons | 30 ms |
| full time-aware analysis, 486–998 m real route | 5–55 ms |

The analysis prunes candidates per slice by real shadow reach and caches
`(building, solar bucket)` shadows, which is why a whole walk costs tens of
milliseconds instead of seconds. Documented budgets: ≤ 900 slices,
≤ 60 solar buckets, ≤ 6 000 buildings per analysis, 10 s request timeout,
2 concurrent analyses.
