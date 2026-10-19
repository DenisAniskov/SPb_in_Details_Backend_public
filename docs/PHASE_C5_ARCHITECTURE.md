# Phase C5 — Scenic Walk Engine Architecture

**Date:** 2026-10-11 · **Branch:** `codex/phase-c5-scenic-walk-engine`
**Base:** Phase C4.1 `f7fc42a81211ce185dac2196707ac02244485951`

## 0. Principle

Сначала географическая достоверность и реальная пешеходная проходимость,
затем туристическая привлекательность. Every scenic claim is verified
against real MapKit routing results and the verified dataset geometry —
nothing is inferred from Haversine proximity alone.

## 1. The three-way separation (§2 of the brief)

| Concept | Implementation | Counted as a tourist stop? | Arrival confirmation? |
| --- | --- | --- | --- |
| **Tourist Stop** | `PlaceModel` in the walk list; a stage endpoint with `visitMinutes` | yes (limit 6) | yes — existing two-fix/manual machinery |
| **Scenic Waypoint** | a point on a verified public way (`ScenicFeature.waypoint`) used as a routing intermediate; merged INTO the stage geometry | no | never |
| **Navigation Maneuver** | `PedestrianManeuverModel` from the C4 adapter, rebased onto the combined stage | no | no |

### The key architectural decision: combined stage legs

The engine composes a scenic stage by requesting REAL MapKit pedestrian
segments `A → Waypoint` and `Waypoint → B` and merging them into **one**
`RouteLegModel`. Invariants (enforced by `ScenicStageBuilder.combineDetailed`,
which accepts bit-identical coordinates only):

1. Segments must share the EXACT waypoint coordinate. A junction MapKit
   snapped apart is never tolerated: the engine confirms it with a real
   pedestrian route between the two projections and splices that route's own
   geometry, native time/distance and maneuvers into the stage as its own
   piece (a curved connector stays curved); if it cannot be confirmed, the
   candidate is rejected — no invented line is ever drawn or computed.
2. Maneuvers of later segments are rebased (segmentIndex shift + offset
   shift) and re-validated: position resolves on the combined geometry,
   ordering survives the merge; otherwise the stage is rejected to honest
   fallback. A spliced connector counts as one more real segment in that
   rebase.
3. The stage endpoint is the tourist stop: waypoints never produce
   arrivals, visit time, or extra stop-count entries.
4. Declared distance/duration are the sums of the native segment values;
   the walking metric stays the application's geometric polyline length.
   After junction confirmation every acceptance limit (detour share,
   extra-time cap, preset budget, score) is re-measured on these totals.

**Consequence: the Phase C4 navigation engine and the C4.1 simulator run
unchanged.** `NavigationProvider` needed only additive field threading
(`scenic` on leg copies, `walkKind` on route snapshots). A→W1→W2→B is one
leg → exactly one arrival at B, verified by tests and the simulator.

### Model map (project naming style)

- Tourist Stop / Visit — `PlaceModel` + `RouteLegModel.visitMinutes`
- Tourist Stage — `RouteLegModel` (one per stop-to-stop transition)
- Native Routing Segment — the `RouteLegModel` pieces merged by
  `ScenicStageBuilder`
- Scenic Waypoint — `ScenicFeature.waypoint` (`scenic_walk_model.dart`)
- Verified Maneuver — `PedestrianManeuverModel` (+ `rebaseOntoCombinedGeometry`)
- Requested vs built kind — `WalkKind` on `PedestrianRouteModel.walkKind`
  (scenic only when scenic segments were actually accepted)

## 2. Scenic dataset

- **Acquisition:** `docs/phase_c5_acquisition/` — Overpass queries for five
  target districts (Центральный, Петроградский, Василеостровский, Невский,
  Приморский), cache-first and idempotent, all requests logged
  (`requests.json`), raw responses cached (`scenic_<district>.json`).
- **Policy:** waypoints are points ON named public pedestrian ways
  (pedestrian streets, named embankment ways, public squares). Parks and
  gardens are excluded from waypoints — the C3 audit found no verified
  public entrances, and area centroids are not entrances. Elements tagged
  `access/foot ∈ {private, permit, no, military}` are excluded. 187
  features accepted; 130 blocked-access elements excluded; 185 park
  elements kept as audit context only.
- **Integrity:** `docs/phase_c5_scenic_dataset_manifest.json` (per-district
  counts, source SHA-256, policy) + the embedded Dart dataset
  (`lib/data/scenic/scenic_dataset.dart`, version `2026.10.11+1`).
- **No per-walk network calls:** the dataset is embedded and versioned;
  regeneration runs through the documented pipeline only.

## 3. Scenic Route Engine

`lib/services/scenic_walk_engine.dart` — runs ONCE per route build
operation (never per GPS tick):

1. **Baseline:** the already-built normal walk (real MapKit stages).
2. **Candidate pre-filter (Haversine, economisation only):** dataset
   features within a 400 m corridor of a stage piece, ≥150 m from its
   endpoints, deduplicated by name and excluding already-engaged features;
   deterministic ranking (type weight × confidence, then cross-track, then id).
3. **Native routing:** for each candidate (≤3 per step, ≤2 waypoints per
   stage): real `PedestrianRouter` segments through the waypoint. Both
   requests are reserved from the SHARED `NativeWalkRequestBudget` before
   the calls (same conservative policy as the walk build; the 12-request
   per-build cap is NOT raised).
4. **Geometry verification (§4.3):** the fraction of the combined stage
   length within 60 m of the verified feature line/point (coverage share)
   distinguishes `verifiedOnPath` (≥0.5), `verifiedNearby` (≥0.15) and
   `ambiguous` (rejected — never claimed scenic).
5. **Scoring** (see PHASE_C5_SCORING.md) and hard limits: detour share ≤1.6,
   extra walking ≤15 min per stage, and the walk-level budget
   (native walking + visit time ≤ preset budget) must all hold.
6. **Assembly:** `ScenicStageBuilder` merges the accepted pieces; the stage
   carries `ScenicStageInfo` (feature ids/names, evidence, coverage, extra
   time, score, explanation).

**Reroute behaviour (§8):** a manual reroute replaces the current stage with
a direct MapKit route — the scenic detour of that stage is dropped honestly
(the rebuilt leg has no scenic metadata), visited stops survive, and the
requested `walkKind` stays scenic at the route level. Documented and tested.

## 4. Fallback (§10)

When no candidate passes verification/scoring/budget, the normal route is
kept and the user sees: «Для этой прогулки не удалось подобрать подходящий
живописный вариант. Можно продолжить по обычному пешеходному маршруту.»
The immutable snapshot records `walkKind: normal` — a fallback is never
presented as a built scenic walk. Reasons are tracked per operation
(`ScenicFallbackReason`): noCandidates / requestBudgetExhausted /
budgetExceeded / noVerifiedGeometry / mapkitUnavailable.

## 5. Integration surface

- `RoutingProvider`: additive `_walkScenic` request flag (wizard), the
  engine call after a successful normal build, `routeComparison` +
  `scenicFallbackMessage` state, `walkKind` on snapshots. All existing
  fields and flows unchanged (501 baseline tests pass unmodified).
- **UI:** wizard walk-type selector (Обычная/Живописная + description),
  comparison card in the route details (§7.2 — both variants, extra time,
  verified benefit lines), honest fallback message. The map is NOT
  cluttered with waypoint markers: the scenic path is part of the route
  line; stop badges/maneuver markers/simulation walker keep their C4/C4.1
  semantics.
- **Simulator C4.1:** scenic stages are ordinary legs — walking, seeking
  (honest rebuild through intermediate stops), restarting, error
  injections and the purple «Симуляция» badge all work unchanged (tests in
  `scenic_navigation_test.dart`).

## 6. Compatibility statement

- Backend: untouched (414+1skip pass).
- SQLite: no schema changes.
- Existing 501 Flutter tests: pass unmodified.
- MapKit: same PedestrianRouter; scenic requests share the existing budget
  cap (12 per build), so the worst case remains bounded.

## 7. Known limitations

1. Geographic coverage of the first dataset is partial: rich in the central
   corridor, thin in Невский (6 features) and the residential outskirts —
   measured in `phase_c5_geographic_evaluation.json`; scenic alternatives
   exist for only 2 of 8 frozen scenario probes (8-direction probe).
2. MapKit-native scenic routing results on device: `NOT_TESTED`.
3. Waypoint verification uses reduced feature lines (≤12 points) — a
   waypoint is within the 60 m band of its own line, not exactly on a
   sampled point (documented in tests).
4. The scoring model is an explainable heuristic — it is NOT an objective
   measure of beauty.
