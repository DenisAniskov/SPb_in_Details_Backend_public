# Phase C3 Source Licenses Supplement

Phase C3 continues the ODbL compliance chain of Phase B and C2
(see `PHASE_B_SOURCE_LICENSES.md`, `PHASE_C2_SOURCE_LICENSES.md`,
and `app/db/README.md`).

## What Phase C3 added

- 47 new catalog records with canonical OSM sources
  (`docs/phase_c3_osm_source_snapshot.json`, license ODbL 1.0,
  attribution © OpenStreetMap contributors).
- Raw acquisition evidence: `docs/phase_c3_acquisition/*.json` with
  hash-bound requests (`requests.json`), including failed attempts and the
  invalidated regional-mirror responses.
- Derived audits: `docs/phase_c3_candidate_review.json`,
  `docs/phase_c3_entrance_audit.json`, `docs/phase_c3_nearby_pairs.json`,
  `docs/phase_c3_district_assignments.json`,
  `docs/phase_c3_geographic_evaluation.json`.
- Russian editorial texts remain original SmartNavigator content under the
  express permission documented in `app/db/README.md`.

## Machine-readable ODbL source offer on public redistribution

If the repository or the catalog is published, the complete source offer must
ship **one machine-readable export per catalog record**, unioning:

1. `docs/osm_source_snapshot.json` (Phase B),
2. `docs/phase_c2_osm_source_snapshot.json` (+ `phase_c2_acquisition/`),
3. `docs/phase_c3_osm_source_snapshot.json` (+ `phase_c3_acquisition/`),
4. `docs/phase_b_boundary_evidence.json` + `docs/phase_c2_boundary_evidence.json`
   (district polygons used for assignments),
5. the per-record `source_url`, `coordinate_kind`, `source_checked_on`,
   `address_status`, `coordinate_uncertainty`, `verification_evidence` fields,
   and — when routing points are filled in a later phase —
   `routing_point_source` / `routing_point_verified_on` /
   `routing_access_status` per record.

The export must preserve OSM object versions as acquired, carry the ODbL
notice and attribution, and be regenerable by the committed offline validators
(`validate_phase_b_catalog`, `validate_phase_c2_catalog`,
`validate_phase_c3_catalog`). Yandex MapKit data, 2GIS data and any proprietary
content must **not** be mixed into that export.

## This repository

The repository remains private; no automatic publication of the source export
was performed in this phase, and the repository visibility was not changed.
