# Catalog data notice

`seed_places.json` contains information from [OpenStreetMap](https://www.openstreetmap.org/copyright), © OpenStreetMap contributors. The catalog database, including its adaptations and additions, is made available under the [Open Database License 1.0 (ODbL)](https://opendatacommons.org/licenses/odbl/1-0/).

The machine-readable catalog is `seed_places.json`; per-record `source_url`, `coordinate_kind`, `source_checked_on` and `address_status` preserve geographic provenance. The associated source selections are recorded in `docs/osm_source_snapshot.json` and the Phase B provenance artifacts. Preserve this notice and the ODbL link when redistributing the database.

Russian descriptions, visit reasons, cautions, topic tags and visit-time estimates are original SmartNavigator editorial additions. They are identified separately from copied geographic facts. The project grants permission to copy, adapt, redistribute and publicly display these original editorial contents when reusing this catalog or its derivative catalogs, including in produced works, under the ODbL terms above, without an additional restriction on database reuse. This express content permission accompanies the database licence; ODbL alone does not automatically license independent copyright in individual contents. This notice does not relicense application code, Yandex SDK data, trademarks, or third-party photographs/text.

See [Phase B source and publication review](../../docs/PHASE_B_SOURCE_LICENSES.md).

Phase C2 adds versioned geographic evidence in `docs/phase_c2_osm_source_snapshot.json`,
`docs/phase_c2_acquisition/`, and `docs/phase_c2_boundary_evidence.json`. Union these
with the unchanged Phase B evidence to reproduce the complete expanded catalog.
The full catalog and all OSM factual source/audit exports carry this same ODbL
notice and attribution. See the [Phase C2 supplement](../../docs/PHASE_C2_SOURCE_LICENSES.md).

Phase C3 adds versioned geographic evidence in `docs/phase_c3_osm_source_snapshot.json`
and `docs/phase_c3_acquisition/`. Union these with the unchanged Phase B and C2
evidence to reproduce the complete 173-record catalog. The C3 candidate review,
entrance audit and per-record verification evidence are part of the same ODbL
source offer. See the [Phase C3 supplement](../../docs/PHASE_C3_SOURCE_LICENSES.md)
for the machine-readable source-export requirements on public redistribution.

Phase C6.1 appends seven reviewed city attractions (museums, a loft project, an
art centre, a park and a family theme park) backed by versioned evidence in
`docs/phase_c6_1_osm_source_snapshot.json` and `docs/phase_c6_1_acquisition/`,
extending the catalog to 180 records. Names, addresses and operating status of
the additions were re-checked against the attractions' own official sources
(`docs/phase_c6_1_source_verification.json`); the candidate review and close-pair
review are part of the same ODbL source offer. Union these with the unchanged
Phase B/C2/C3 evidence to reproduce the complete 180-record catalog.
