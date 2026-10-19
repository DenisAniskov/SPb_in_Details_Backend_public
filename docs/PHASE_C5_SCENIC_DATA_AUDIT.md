# Phase C5 — Scenic Data Audit

**Date:** 2026-10-11 · Dataset version `2026.10.11+1`

## 1. Sources

| Property | Value |
| --- | --- |
| Data source | OpenStreetMap via Overpass API |
| Endpoints used | `overpass-api.de` (primary; a custom User-Agent is required — the default urllib UA receives HTTP 406), `overpass.kumi.systems` (HTTP 500 at acquisition time), `overpass.mail.ru` (non-JSON response) |
| License | ODbL 1.0, © OpenStreetMap contributors |
| Acquisition evidence | `docs/phase_c5_acquisition/requests.json` — every request logged (endpoint, timestamp, status); raw responses cached per district |
| Verification date | 2026-10-11 (recorded per feature as `verifiedOn`) |
| Reproducibility | cache-first scripts: `acquire_scenic.py` (fetch, idempotent, `--refresh` forces new requests) and `build_scenic_dataset.py` (dataset + manifest generation) |

## 2. What became a waypoint — and what deliberately did not

Accepted feature categories (a waypoint = a point ON the public way):

1. **Embankments** (73) — ways named «…набережная…» with a walkable highway
   type (`pedestrian`, `footway`, `service`, `residential`,
   `living_street`, `unclassified`, `steps`). City-level public quays.
2. **Pedestrian streets** (47) — `highway=pedestrian` with a name.
3. **Public squares** (67) — `place=square` with a name (node or way;
   for ways a point on the square geometry).

Deliberately excluded:

- **Parks and gardens** — fetched as audit context only (185 elements).
  The C3 entrance audit found **no verified public park entrances**, and
  the Phase C5 brief explicitly forbids treating park centroids as
  entrances. No park/garden element became a waypoint; the dataset tests
  enforce this.
- **Access-restricted elements** — 130 elements with
  `access/foot ∈ {private, permit, no, military}` were excluded.
- **Unnamed / degenerate** elements.

No invented coordinates: every waypoint is an OSM way/node coordinate, and
every feature carries its OSM id and URL.

## 3. Per-dataset-element record

Each feature stores: stable id (`spb-scenic-<district>-<osmType>-<osmId>`),
type, name, district + label, waypoint coordinates, reduced way line
(≤12 points, for line-based verification), approximate way extent, OSM id /
URL, source, license, verification date, raw access evidence tags,
confidence (embankment/pedestrian street 0.9, square 0.8) and an explicit
uncertainty note: the waypoint lies on a named public pedestrian way per
OSM; NO entrance status onto enclosed territories is claimed.

## 4. Per-district coverage (dataset-level)

| District | Features | Embankments | Pedestrian streets | Squares |
| --- | ---: | ---: | ---: | ---: |
| Центральный | 76 | 8 | 36 | 32 |
| Петроградский | 33 | 26 | 3 | 4 |
| Василеостровский | 30 | 11 | 3 | 16 |
| Невский | **6** | 1 | 0 | 5 |
| Приморский | 42 | 27 | 5 | 10 |

## 5. Corridor evaluation against frozen C2 scenarios

`evaluate_scenic_geography.py` probes 8 deterministic 600 m stage
directions from each frozen C2 start in the five target districts and
counts dataset features inside the 400 m candidate corridor
(`phase_c5_geographic_evaluation.json`). This is a **geometric pre-filter
metric** — explicitly not a MapKit-native one.

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

**Honest conclusion:** only 2 of 8 frozen-scenario probes have corridor
candidates. The verified scenic dataset is rich in the central corridor
and thin around residential metro districts — especially Невский. Scenic
alternatives there will honestly fall back to the normal route; the
dataset must be extended (more named public spaces, embankment paths of
Обводного/Невы in the east, park paths once entrances are verified) before
scenic coverage outside the centre can be claimed.

## 6. Update process

1. Extend/adjust `acquire_scenic.py` queries (or widen districts).
2. `python acquire_scenic.py --refresh` (new raw caches + request log).
3. Review the acquisition, then `python build_scenic_dataset.py`
   (regenerates the Dart dataset + manifest with new version/date).
4. Run `scenic_dataset_test.dart` and update
   `phase_c5_scenic_dataset_manifest.json` review notes.

## 7. Attribution

OpenStreetMap data is used under ODbL 1.0 with attribution
«© OpenStreetMap contributors». The dataset embeds the license and
attribution strings; the app's existing sources dialog remains the
user-facing attribution surface.
