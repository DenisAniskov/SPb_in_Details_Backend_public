# Phase C2 — Smart Walk v2 local route proposals

Implementation: `app/services/smart_walk_service.py`; additive response contract: `app/schemas/route.py`; focused regressions: `tests/test_smart_walk_v2.py`. The exact C1 implementation remains frozen in `scripts/phase_c1_smart_walk_baseline.py` for reproducible comparisons. Geographic results are in [PHASE_C2_GEOGRAPHIC_EVALUATION.md](PHASE_C2_GEOGRAPHIC_EVALUATION.md).

## What changed and what a proposal means

C1 chose one next stop greedily, gave an unusual POI 25 points, and only shortened the chosen order with 2-opt. A high-scoring long visit could consume enough budget to prevent two useful nearby stops. A fixed proximity bonus could also be outweighed by unusualness. Native correction could remove chosen stops but could not replace them with another backend candidate.

V2 keeps several feasible partial routes, compares different POI combinations and orders, and supplies up to two alternatives with different POI sets. Every stop remains inside the selected category filter. A single useful destination is valid. There is no minimum of three stops, change to preset durations, out-of-category rescue, or fabricated native geometry.

All backend distances are **Haversine geometric distances**; all backend walking times are **preliminary estimates** at 75 m/min. Roads, bridges, fences and entrances are absent from this model. Only the client can accept a walk after native MapKit validates its time and pedestrian geometry. Real physical-device results for C2 remain `NOT_TESTED` unless separately captured in the validation report.

## Input and identity protection

API starts must be finite numeric WGS84 coordinates; text and booleans are rejected rather than coerced. API duration is a strict integer 30..360, max places a strict integer 1..6, unusual preference a boolean, and categories existing enums. UI still has exactly 60/120/240-minute presets.

The service also validates direct callers. It excludes malformed coordinates, nonpositive/noninteger IDs, nonenum stored categories, and visits other than integer 5..180 or legacy `None` (30 minutes). `PlaceRead` is validated and cached before ranking, so a selected corrupt row cannot fail during response serialization. Existing safe handling of BLOB text and malformed unusual flags is preserved.

Exact duplicate IDs collapse. Conflicting copies of an ID, determined by the serialized public fields and catalog key, are rejected as ambiguous. Repeated catalog keys and normalized equal titles within 25 m do not create extra stops. This conservative same-title rule can merge legitimate adjacent objects with indistinguishable names; it is not a complete semantic duplicate detector.

Feasibility is checked from the origin before bounding the pool. By the triangle inequality, a point whose direct origin-to-point travel plus visit exceeds the budget cannot fit any Haversine itinerary starting there. No arbitrary maximum search radius excludes a genuinely feasible distant POI. Destinations less than 25 m from the start are excluded as an immediate coordinate-uncertainty guard; they cannot manufacture a zero-length walk or crowd meaningful stops out of the pool. This threshold is a selection guard, not a claim about GPS accuracy or verified access.

## Candidate score and relative influence

For an eligible candidate at geometric leg distance `d`, with editorial visit `v`:

```text
interest     = 50 when categories were selected, otherwise 30
sightseeing  = 0.2 × min(v, 45)                 # 1..9 points
unusual      = 4 when preferred and honestly flagged, otherwise 0
proximity    = 40 × max(0, 1 − d / 3500)
diversity    = 12 for the first stop of a category,
               when any categories or several selected categories are allowed
candidate    = interest + sightseeing + unusual + proximity + diversity
```

For a route, let `W` be total preliminary walking minutes, `F` preliminary walking minutes to its first POI, and `B` the requested unchanged budget:

```text
route utility = sum(candidate) − 120 × W/B − 80 × F/B
```

Each additional walking minute costs 2/1/0.5 points for quick/regular/extended. The first transfer additionally costs 1.333/0.667/0.333 points per minute. This gives locality stronger influence for shorter presets without a hard radius. Four unusual points cannot compensate for a large avoidable first transfer; equal useful local choices still prefer the unusual stop. Editorial duration supplies at most nine points, so a full museum visit is not automatically more valuable than several feasible local activities. No visit durations are shortened by the algorithm.

The positive value per attraction favors more useful stops when they fit. This can consume more spare time than C1, especially with the extended preset. Less preliminary slack increases the risk that native validation needs an alternative or removes a stop. V2 does not hide this risk behind an invented safety buffer or claim that a preliminary success proves native success.

## Bounded search and determinism

The candidate pool has at most **24** entries: a feasible representative of each allowed interest when diversity applies, the closest 16 feasible destinations, then remaining entries by complete singleton utility. Sorting is stable by utility, geometric distance and ID. The reservation changes ranking; it does not force any category into a final over-budget route. Same-title duplicate detection uses 25 m Earth-centered spatial buckets and the 27 adjacent buckets, followed by exact Haversine comparison; it avoids scanning every same-name record and handles poles/dateline without longitude special cases.

The beam retains at most **32 states per depth**, across at most **six depths**. The geometric matrix is at most 24×24; search makes no network requests. The expansion ceiling is 24 + 5×32×24 = **3,864 candidate extensions**, before duplicate/feasibility rejection. Work to inspect and sort the source catalog remains proportional to catalog size (`O(n log n)` for sorting); the route search does not grow factorially with that size.

States track selected POIs, endpoint, total distance/visits, first transfer, category coverage and score. For each selected set and endpoint, the better ranked state survives. Every retained feasible depth is a final-result candidate, including singletons; a later dead end cannot discard an earlier valid walk. Final duplicate sets collapse to their best retained order. Equal utility breaks ties by lower distance, lower visit duration, then the lexicographic ID sequence. Input reversal produces the same output in the scenario evaluation and regressions.

The old 2-opt helper remains available for compatibility/tests; v2 production selection explores orders directly in the bounded beam. Pruning and the 24-entry pool mean v2 does **not** guarantee the globally optimal route. It also cannot optimize a real pedestrian path on the backend.

## Strict preliminary budget and alternatives

All candidates use:

```text
walking_seconds = ceil(Haversine route length / 75 × 60)
walking_seconds + integer_visit_minutes × 60 <= budget_minutes × 60
reported_total_minutes = ceil(walking_seconds / 60 + integer_visit_minutes)
```

The upward-second rounding is enforced during expansion and response construction. A fraction of a second over the limit is rejected. No route exceeds six unique stops.

The primary response preserves C1 fields. Additive `alternatives` contains zero to two objects with the same `time_budget_minutes`, all four `estimated_*` totals, `suggested_places_sequence`, and `items` shape. Each is independently preliminary-feasible and has a POI set distinct from the primary and other alternatives. Reordering the same set does not count. Where available, alternatives replace at least one primary POI and begin at a different destination; otherwise a genuinely different shorter subset is permitted. Alternatives are choices for native validation, not three routes to publish together. Old response payloads without the additive fields still deserialize with empty alternatives and a null reason.

## Honest empty results

`status=empty` has zero totals, empty items/sequence/alternatives and one `reason_code`:

| Code | Evidence | Suggested action |
|---|---|---|
| `no_eligible_places` | No valid, unambiguous readable catalog rows | Try later or another start; inspect catalog in development |
| `category_filter_empty` | Valid catalog rows exist, none match chosen interests | Change interests or start |
| `preliminary_budget_infeasible` | No matching POI fits direct geometric travel plus editorial visit | Longer preset or another start |
| `no_meaningful_route` | Budget-feasible matches are within the 25 m start guard | Another start or a longer walk |

Success has `reason_code=null`. Complete absence of stored rows retains the existing endpoint 404 behavior. Backend empty reasons make no claim about nearby unmapped attractions, native route availability, user GPS history or network connectivity. Native/network failures belong to the client stage.

## Native acceptance and automated evidence

The Flutter client tries the primary and up to two valid, genuinely distinct proposals with a shared bound: at most **six service builds and twelve point-to-point native routing requests per action**. It reserves requests before builds and enforces the actual bound immediately before native `requestRoutes`. Alternatives that exceed the remaining bound can be skipped. A smaller alternative can precede existing preference-cost stop removal. Every published route has complete native geometry, positive native distance, and upward-rounded native walking seconds plus canonical editorial visits no greater than the selected budget.

Pedestrian no-path results may try another candidate. Service/network/timeout/unknown failures stop immediately and are never converted into “insufficient time,” including after an earlier budget failure. Mixed unavailable/over-budget candidates have an explicit mixed explanation. Request revision checks protect state after awaited calls. Navigation, manual rerouting and attribution remain covered by existing Flutter tests.

The focused backend v2 suite passes **56 tests**. It covers the greedy trap, local versus distant unusual choices, replacement alternatives, all presets, categories, meaningful one-stop outcomes, rounded boundary compliance, malformed requests/rows, ambiguous identities, bounded geometric work (including repeated titles), pole/dateline duplicate handling and additive backward compatibility. These tests use synthetic points and fixtures; they do not prove native pedestrian timings.
