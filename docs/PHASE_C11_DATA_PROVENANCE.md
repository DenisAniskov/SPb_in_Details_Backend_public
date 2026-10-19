# Phase C11 — quiet pilot data provenance

## Source

| Field | Value |
| --- | --- |
| Provider | Geofabrik GmbH |
| Product | OpenStreetMap — Northwestern Federal District extract |
| URL | `https://download.geofabrik.de/russia/northwestern-fed-district-261016.osm.pbf` |
| Raw SHA-256 | `3051f24dd1dc0ab6efc63f0a015781ef8380d1523c02a0f09c1e6c5ed52d7fb7` |
| Raw bytes | 655,141,467 |
| Source last modified | 2026-10-17T02:13:55Z |
| OSM replication timestamp | 2026-10-16T20:21:21Z |
| Licence | ODbL-1.0, <https://opendatacommons.org/licenses/odbl/1-0/> |
| Attribution | © OpenStreetMap contributors |
| Pilot bbox (west, south, east, north) | 30.28, 59.915, 30.38, 59.965 |
| Selected classes | motorway/trunk/primary/secondary/tertiary/residential/unclassified/living_street + `_link` variants |
| Excluded classes | footway, pedestrian, path, steps, corridor, cycleway, service, construction and all non-road values |
| Tag whitelist | `highway`, `access`, `motor_vehicle`, `motorcar`, `vehicle`, `lanes`, `lanes:forward`, `lanes:backward`, `maxspeed`, `maxspeed:forward`, `maxspeed:backward`, `oneway`, `service`, `tunnel`, `bridge`, `layer`, `surface`, `sidewalk`, `foot`, `name`, `ref` |
| Source ways | 3,638 |
| Usable motor roads | 3,595 (43 excluded by access/tunnel/degenerate rules) |
| Total source-way length | ~398.7 km (sum of clipped-ish source segments; not unique network length) |

## Shipped artifacts

| Artifact | SHA-256 / bytes |
| --- | --- |
| `data/quiet_spb_pilot.json.gz` | gzip `90e395f4e2208e933098bd1f1ba09e270209dd946e171f634d89eccb27940b92`, 249,779 bytes |
| uncompressed canonical JSON | `c191b22a2df93b89c9ea513e02d020682ca43e68209d7f5a344a5e8c5be11ce3` |
| `data/quiet_spb_pilot.manifest.json` | pins both hashes, bbox, source and class counts |

The engine verifies the gzip hash, uncompressed byte length and dataset hash
against the manifest on load. A mismatch makes the whole dataset unavailable;
no partial layer is trusted.

## How the snapshot was built

```bash
cd backend
python -m pip install osmium==4.3.1   # acquisition only
python -m scripts.acquire_phase_c11_quiet_data \
  --refresh --pbf <verified northwestern-fed-district-261016.osm.pbf>
python -m scripts.acquire_phase_c11_quiet_data --verify
```

The script downloads/verifies the pinned PBF, extracts only ways intersecting
the pilot bbox, preserves OSM ids/timestamps/tags, sorts by OSM id, writes
canonical JSON, gzip with `mtime=0`, and records the manifest. The backend
runtime and Docker image do not install `osmium` and never download OSM.

## Update policy

* A new snapshot requires a new dated Geofabrik extract, a new SHA-256, a new
  `dataset_version`, a rebuilt manifest and a new model validation.
* No cron, background fetch or per-request download exists.
* Existing snapshots are not silently overwritten. A new dataset is a
  deliberate code change with its own provenance and tests.
* The Docker image copies `data/`; the snapshot is read-only at runtime.

## Known coverage limits

* OSM completeness varies by street; absence of a road within 120 m does not
  prove absence of traffic.
* Service roads, courtyards and parking aisles are deliberately excluded, so
  local traffic on them is not represented.
* Tunnels are discounted, not treated as surface noise sources.
* The source has no acoustic measurements and no hourly traffic model.
* The pilot is central SPb only. Routes outside it are unknown.
