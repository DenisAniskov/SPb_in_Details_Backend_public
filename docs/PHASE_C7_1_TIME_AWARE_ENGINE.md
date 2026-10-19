# Phase C7.1 — Time-Aware Shade Engine

Status: **IMPLEMENTED**, **UNIT_TESTED**. Device: **NOT_TESTED**.

## 1. Why one instant is not an analysis

A four-hour walk crosses shadows that move with the Sun. This engine gives
every piece of the walk the solar position of the moment the tourist is
actually there:

```
walk timeline
  leg 1 walking  (native duration) → visit 30 min → leg 2 → … → destination visit
  ‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾                 ‾‾‾‾‾‾‾‾‾‾
  slices of ≈40 m, each classified at the Sun of its own mid-time
```

## 2. Documented discretisation (and its honest limit)

| Parameter | Value | Why this value |
| --- | --- | --- |
| slice length | 40 m | ≈ half a city block: finer slices mostly recompute the same shadow; coarser ones blur a façade shadow |
| solar bucket | 10 min | upper end of the §10.2 range 5–10 min; the Sun moves ≈0.5–2° of azimuth in 10 min near midday |
| walking time share | proportional to geodesic length | simple, deterministic; the real pedestrian speed varies and is stated as a limitation |
| visit durations | added to the clock, per leg | a museum visit moves the Sun for everything walked afterwards |

Consequences that are stated rather than hidden: a slice is classified at its
mid-time (a 40 m slice spans ≈30 s of walking), and a walk with very few
slices computes very few solar positions (buckets exist only where ground is
walked).

## 3. Three states, never collapsed (§9)

| State | Meaning | Conditions |
| --- | --- | --- |
| `estimated_shaded` | modelled shadow covers the WHOLE slice | ≥ `length − 0.25 m` of the slice intersects the union of shadows of buildings with usable heights, and no envelope band remains |
| `estimated_exposed` | sunlit according to the model AND the data | inside mapped coverage, outside every shadow band and outside the height-unknown envelope |
| `unknown` | the data cannot answer | the WHOLE slice is outside the pilot zones, or inside the shadow envelope of buildings without usable heights |
| `mixed` | more than one state inside the slice | the measured `shaded_spans` / `unknown_spans` say exactly which metres are which |

Slice classification tolerance: `0.25 m` (`STATE_EPSILON_M`). The label is a
coarse reading of the slice; the spans carry the exact measured boundaries and
the client draws those, never a whole-slice colour for a `mixed` slice.

Invariants, asserted in tests to 1e-6:
`estimated_shaded + estimated_exposed + unknown ≈ total walking length`;
per slice the spans are disjoint, inside `[0, length]` and sum to the reported
`shaded_m` / `unknown_m`.

### 3.1 Slices are REAL polylines (C7.1.1)

A slice keeps every interior vertex of the source geometry between its two cut
points. Previously a slice was a straight chord between the cut points, which
silently analysed a different path whenever the walkway turned inside the
40 m: on the three real §19 scenarios 11, 10 and 13 of the 25/16/17 slices
contain such turns. On `gostiny_dvor_arcade` this cost 2.1 % of the shade and
invented 21 m of `unknown`; on `isaac_blocks` it inflated the shade from
31.1 % to 34.6 %. A regression test asserts that a turning route's slices
reconstruct the leg vertex-by-vertex and that each slice's polyline length
equals its along-path length.

### 3.2 The unknown band excludes confirmed shadow (C7.1.1)

`unknown` is measured as *envelope MINUS confirmed shadow* (interval
subtraction on the measured spans), so a metre inside both the 40 m envelope
of a height-less building and the shadow of a building with a known height is
attributed once — to the confirmed shadow. The earlier arithmetic subtracted
lengths and could move genuinely sunlit ground into `unknown` while the sum
invariant still held; a regression test arranges exactly that overlap and
asserts `unknown = 0` (the old code reported ≈ 20 m of 80 m).

## 4. Bounded computation (§20)

| Bound | Value |
| --- | --- |
| slices per analysis | ≤ 900 (≈36 km of walking) |
| solar buckets | ≤ 60 (10 h) |
| buildings per analysis | ≤ 6 000 |
| building-query pad | ≤ 500 m (the reach of the tallest usable height at the lowest sun of the walk) |
| request timeout / concurrency | 10 s (configurable) / 2 |

Two optimisations keep a real walk cheap: per-slice candidate pruning by the
actual shadow reach, and a per-analysis cache of `(building, solar bucket)`
shadow polygons, so a building's shadow is computed at most once per bucket
however many slices see it. The prune is *sound* rather than heuristic: every
point of a slice lies within half the slice length of its mid-point, so the
margin is `reach + L/2 + 1 m`. Measured after C7.1.1: 30–150 ms per real route
analysis (644–998 m of real OSM footway) with the committed dataset, the
longest being the 998 m arcade loop (25 slices, 31 shadow computations).

## 5. Status vocabulary (§11.2)

`available` (full coverage, all classified), `partial` (some ground unknown),
`unavailable` (no usable data — no numbers are produced at all),
`outside_coverage` (the walk lies outside the mapped pilot zones),
`sun_below_horizon` (the Sun never rises during the walk: no percentages are
invented, the reason is stated).

## 6. Confidence (§9), measurable

| Level | Criteria |
| --- | --- |
| `high` | full coverage, `unknown` < 5 %, and at least one height from an **externally verified** source — therefore unreachable in the pilot (documented, tested) |
| `medium` | full coverage and `unknown` < 25 % |
| `low` | partial coverage or `unknown` ≥ 25 % |
| `unavailable` | no estimate |

## 7. Tests

`tests/test_c7_1_time_aware.py`: short/1-hour/4-hour walks, sun movement
(azimuth swing > 40° over a 4-hour walk), a 30-minute visit shifting later
legs, visit time never inflating for scenic waypoints, speed change scaling
time but not geometry, start-time change changing the estimate, determinism
(two runs are `==`), the unknown band, the invariant, three real §19
scenarios, and the outside-coverage rule.

C7.1.1 regressions (same file): every interior vertex survives slicing, span
interval subtraction, spans report POSITION (head/tail/middle of a line),
a mid-slice shadow band is labelled `mixed` with its measured interval,
the unknown band excludes confirmed shadow, and an out-of-coverage slice is
wholly `unknown` with a full span.
