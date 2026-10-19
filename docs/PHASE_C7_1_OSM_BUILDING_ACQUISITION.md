# Phase C7.1 — OSM Building Acquisition (pilot dataset)

Status: **DATA_VERIFIED** (real OpenStreetMap responses, SHA-256 pinned) and
**UNIT_TESTED** (builder, provider, evidence chain). Device/field status:
**NOT_TESTED** (no Android device run for this phase is recorded).

## 1. Channel and what actually happened

The acquisition channel had to be chosen on evidence, not on preference. All
attempts are recorded in `docs/phase_c7_1_acquisition/requests.json` (22
entries, no silent retries).

| Channel | Attempts | Outcome |
| --- | --- | --- |
| `overpass-api.de` | 4 probes | 2 ok / 2 failed (`504 Gateway Timeout`) |
| `lz4.overpass-api.de` | 4 probes | 1 ok / 3 failed (`504`) |
| `maps.mail.ru/osm/tools/overpass` | 4 probes | 2 ok / 2 failed (nginx `504`) |
| `overpass.kumi.systems` | 4 probes | 2 ok / 2 failed (`500 Internal Server Error`) |
| **`api.openstreetmap.org/api/0.6/map`** | **6 zone requests** | **6 ok, 0 failed** (~5 MB each, seconds) |

Public Overpass instances were **intermittent** in this environment: tiny
probes sometimes succeeded and sometimes returned 500/504, while the bounded
building queries stalled mid-stream at a fixed byte offset (reproduced on two
instances with a 240 s and a 150 s cap). A single 0.85 km² zone query never
completed. Because the *source* (OpenStreetMap) was reachable through its
official API, the pilot uses the **official OSM API `map` call** as the
acquisition channel — same source data, official endpoint — and the Overpass
failures are kept as evidence rather than quietly dropped. Neither channel was
abused: one request per zone, serial, with a 1.5 s delay and an identifying
User-Agent.

## 2. Pilot zones (bounded on purpose)

Six ≈0.85 km² boxes, never the whole city in one request:

| Zone | Bbox (s, w, n, e) | Character |
| --- | --- | --- |
| `centre_palace` | 59.9358, 30.3075, 59.9438, 30.3245 | Дворцовая площадь, Эрмитаж: дворы, открытая площадь |
| `centre_nevsky` | 59.9305, 30.3265, 59.9385, 30.3435 | Невский проспект: улица-коридор |
| `admiralteysky` | 59.9280, 30.2975, 59.9360, 30.3145 | Исаакиевский собор: дворы-колодцы |
| `petrogradsky` | 59.9540, 30.3085, 59.9620, 30.3255 | Петропавловская крепость, набережные |
| `vasileostrovsky` | 59.9380, 30.2900, 59.9460, 30.3070 | Университетская набережная, Стрелка |
| `control_kupchino` | 59.8490, 30.3915, 59.8570, 30.4085 | контрольная территория вне центра |

## 3. Evidence chain

* Raw responses: `docs/phase_c7_1_acquisition/*.osm.xml` — **29 944 084 bytes
  total, local only** (gitignored: ~30 MB of XML is not repository material).
* `docs/phase_c7_1_acquisition/manifest.json` — SHA-256 per zone, byte size,
  exact bbox, retrieval timestamp, license, acquisition note.
* The builder (`scripts/build_phase_c7_1_buildings.py`) **refuses to run** if a
  cached file's SHA-256 does not match the manifest, so the committed dataset
  is provably a function of exactly those bytes.
* The derived dataset **is committed**: `data/shade_buildings_spb_pilot.sqlite`
  (884 736 bytes, byte-reproducible — verified by `--check-reproducible`).
* License: **ODbL 1.0, © OpenStreetMap contributors**; attribution string is
  stored inside the dataset (`dataset_meta.attribution`) and returned by the
  API in `dataset_provenance`.

## 4. What the dataset contains

1 409 buildings across the six zones, 22 367 vertices, 407 of them multipolygon
relations (courtyard outlines included as interior rings), plus an R-tree
spatial index and per-zone coverage rows.

Excluded and counted (never silently dropped, see
`PHASE_C7_1_COVERAGE_AUDIT.json`):

| Class | Count | Why |
| --- | --- | --- |
| `building:part` polygons | 684 | components of a parent outline; counting them as buildings would double the mass |
| `building=roof` | 37 | not vertical prisms (canopies, stadium covers) |
| relations with members outside the box | 39 | would be half-drawn |
| invalid geometry | 5 | degenerate after closure; stored for audit, never served |

Height provenance (the honesty rule of §5.2):

| Provenance | Count |
| --- | --- |
| `externally_verified_height` | **0** — no independent source was used, so the category is empty and the API reports it as 0 |
| `osm_declared_height` | 17 (`height=*` tag, parsed and range-checked) |
| `levels_estimate` | 935 (`building:levels` × 3.3 m documented floor assumption) |
| `unavailable` | 457 — no usable height; they cast no modelled shadow and are reported separately |

## 5. Reproducing

```bash
cd backend
python -m scripts.acquire_phase_c7_1_osm            # downloads + manifest + hash verify
python -m scripts.build_phase_c7_1_buildings --check-reproducible
python -m scripts.extract_c7_1_test_fixture          # test fixtures + §19 scenarios
python -m scripts.acquire_phase_c7_1_osm --verify    # hash-check the cache offline
```
