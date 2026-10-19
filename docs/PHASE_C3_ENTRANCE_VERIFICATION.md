# Phase C3 Entrance Verification

**Verification date:** 2026-10-10 · **Machine-readable result:** `docs/phase_c3_entrance_audit.json`

## Question

Do the (new) catalog records have documented, public, pedestrian visitor
entrances that could serve as routing destinations instead of representative
polygon centers?

## Method

Two bounded, cached, serial Overpass acquisitions against named park/garden
polygons in the priority districts:

1. `entrance=*` nodes within 15 m of named park polygons
   (`c3_entrances_*`, 8 queries).
2. `barrier=gate` nodes within 15 m of the 13 accepted way-mapped parks
   (`c3_park_gates_*`, 5 queries; district-wide versions kept timing out on
   public gateways and are logged as failed attempts).

Each found node is bound to a park by the response's park bounds plus the
server-side around-filter; access tags are reviewed per node.

## Result

| Verdict | Parks |
| --- | ---: |
| verified public entrance | **0** |
| documented gate marked restricted (`access=private/permit`) | 6 |
| gates with no access tag (publicity NOT provable) | 1 (усадьба Орловых-Денисовых, 6 untagged gates) |
| no documented entrance nodes at all | 6 |

Example: the only documented gate of Молвинский сад (node/5819196743) is
explicitly `access=private` — a restricted, not a public, entrance.

## Honest rules applied

- Absence of `access=private` is **not** treated as proof of publicity.
- A routing coordinate is filled only for an OSM-documented visitor entrance
  node (`entrance=yes|main`) or an explicitly public gate, without access
  restrictions, bound to the canonical park.
- Relation-mapped parks were not scanned by this acquisition (way-based
  around-filter) — a documented limitation.

## Decision

**No catalog record receives routing coordinates in Phase C3.** The additive
routing schema (model properties, API fields, Flutter model fields and
destination preference) is implemented and tested but ships unfilled; every
record honestly reads `routing_access_status = "representative_point_only"`.
Filling it becomes a data-only change for a future phase once real entrance
evidence exists — no further migration is required.

Statuses reserved by the schema:
`verified_public_entrance`, `verified_public_approach`,
`representative_point_only`, `access_uncertain`, `restricted_access`.

## Physical verification

No entrance, approach or gate has been physically verified on the ground, and
no native MapKit routing to any entrance point has been executed in this phase
(see PHASE_C3_ANDROID_CHECKLIST.md, status NOT_TESTED).
