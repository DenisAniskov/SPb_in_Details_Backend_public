# Phase C7.1 — Height Provenance

Status: **DATA_VERIFIED** (counted from the committed dataset),
**UNIT_TESTED** (classification rules), **FIELD_VERIFIED: no**.

## 1. The four categories (§5.2)

| Category | Meaning | How it is produced | Pilot count |
| --- | --- | --- | --- |
| `externally_verified_height` | height confirmed by an independent, named source (survey, register) | only by an explicit import step that names the source; nothing invents it | **0** |
| `osm_declared_height` | the `height=*` value as tagged in OSM | parsed, sanity-ranged 2–400 m | 17 |
| `levels_estimate` | `building:levels` × assumed floor height | assumption recorded per building (`floor_height_assumption = 3.3`) | 935 |
| `unavailable` | no usable height | everything else | 457 |

**An OSM `height=*` tag is a DECLARED value, not a measurement.** OSM does not
verify tags, so the pilot never calls it verified and the API's
`height_provenance_metrics.externally_verified` is 0 — which is exactly why
`confidence: "high"` is unreachable in the pilot (§9).

## 2. Rules that are enforced, not documented only

* A building with `unavailable` height **casts no modelled shadow** — no
  average, no guess. It is counted (`excluded_unknown_height`) and reported.
* A building with `unavailable` height inside the shadow envelope of the walk
  converts the ground it could have shaded into **`unknown`**, never into sun
  (`unknown_meters`). The envelope is a documented 40 m plausibility height
  (`UNKNOWN_HEIGHT_ENVELOPE_M`), deliberately generous for the historic centre
  (median mapped height there is 15–25 m), so the `unknown` band can only
  under-report sun.
* The floor assumption **3.3 m** is a documented compromise between modern
  slab-to-slab heights and the taller ceilings of historic SPb buildings; it
  is stored next to every estimate (`floor_height_assumption`) so any analysis
  can be re-derived with a different assumption.
* `height` values outside 2–400 m, or unparseable text, are treated as absent
  (and counted as `implausible_height_tags`: 0 in the pilot).
* `min_height`, `roof:height` and `building:part` per-part heights are **not**
  modelled: roofs are flat-topped prisms. Multi-part buildings keep only the
  outline's height; if the outline has none, the building is `unavailable`
  rather than assembled from parts (684 part polygons excluded and counted).

## 3. Coverage consequence

| Metric | Value |
| --- | --- |
| buildings considered (six zones) | 1 409 |
| with a usable height (declared + estimate) | 952 (67.6 %) |
| without a usable height | 457 (32.4 %) |

Roughly one building in three in the pilot areas has no usable height. The
analysis therefore returns a substantial `unknown_meters` share on many real
walks — that is the honest result of the available data, not a defect to hide.
It is reported per response and in the limitations text, and the UI must show
it as "unknown", never as sun.

## 4. Growing the verified share (future work, out of C7.1 scope)

* A named external height source (city register, survey) can fill the
  `externally_verified_height` category — the schema and the confidence rule
  already support it; `confidence: "high"` becomes reachable only then.
* Multi-part height assembly (parts as separate prisms) needs a dedicated
  double-counting analysis before it can be trusted.
