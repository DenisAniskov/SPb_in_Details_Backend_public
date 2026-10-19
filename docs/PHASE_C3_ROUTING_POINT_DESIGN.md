# Phase C3 Routing Point Design

**Status:** architecture implemented and tested; **data unfilled** (zero
verified public entrances found in Phase C3 — see
PHASE_C3_ENTRANCE_VERIFICATION.md).

## Problem

Many POI coordinates are OSM nodes, polygon bounds centers or representative
points. Such a coordinate can sit deep inside a fenced park, far from any real
pedestrian entrance, so a walking route that ends there is not a correct
pedestrian destination.

## Design: separate display and routing coordinates

| Field | Meaning |
| --- | --- |
| `display_latitude` / `display_longitude` | the published representative point (map marker, cards) |
| `routing_latitude` / `routing_longitude` | verified pedestrian destination (only with evidence) |
| `routing_point_source` | OSM URL of the entrance/approach node |
| `routing_point_verified_on` | evidence check date |
| `routing_access_status` | `verified_public_entrance` / `verified_public_approach` / `representative_point_only` / `access_uncertain` / `restricted_access` |

### Backend

- Editorial routing metadata lives in the reviewed seed catalogue
  (`app/db/seed_places.json`), like all other provenance content —
  **no new DB columns and no schema migration**. The `Place` model exposes
  validated properties (`app/models/place.py`):
  - routing coordinates are returned **only** when the seed carries a verified
    status, finite coordinates, an OSM source URL and a check date;
  - anything half-filled, non-finite, or non-OSM honestly reads as `None`
    with status `representative_point_only`.
- API: `PlaceRead`/`PlaceDetail` expose the six fields additively
  (`display_*` mirrors the stored point; `routing_*` nullable). Old clients
  ignore the new keys; no existing key changed.

### Flutter

- `PlaceModel` parses the fields tolerantly (absent → defaults) and round-trips
  them in `toJson`.
- Pedestrian destinations prefer the routing point when present:
  `routeLatitude`/`routeLongitude` (routing ?? display). Used in
  `MapKitService.calculateWalkRoute` leg endpoints and
  `calculatePedestrianRoute`; **markers and cards keep the display point**.
- With the current catalog the preference degenerates to the display point —
  behavior is identical to C2 (covered by tests).

### Safety properties

- No fictitious straight lines: MapKit remains the only source of walking
  geometry; the proposal layer only changes the *destination* point handed to
  MapKit.
- No entrance may be invented: the gate validates that no record carries
  routing fields (`validate_phase_c3_catalog`).
- Filling the schema later is a reviewed data-only change; the same gate then
  verifies each routing point's source, status and date.

## Multiple entrances (research note)

Several public entrances per POI were considered and deliberately **not**
implemented: there is no measurable benefit yet (zero verified entrances), and
a per-start nearest-entrance optimization would add untested selection logic.
The singular design keeps that extension open: `routing_point_source` can later
become a list without breaking the API (additive).
