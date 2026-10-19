# Phase C7.1 — Architecture

Status: **IMPLEMENTED**, **UNIT_TESTED**. Device/field: **NOT_TESTED**.

## 1. Layer map (§3)

```
A. Solar & Shadow foundation      app/services/shade_engine/{solar,geo,shadows,intersection}.py
B. Real geographic data           scripts/acquire_phase_c7_1_osm.py
                                  scripts/build_phase_c7_1_buildings.py
                                  app/services/shade_engine/{buildings,building_store,wiring}.py
C. Time-aware estimation          app/services/shade_engine/time_aware.py
                                  app/api/v1/endpoints/environment.py
D. Shade-aware route selection    frontend/lib/services/shade_routing_engine.dart
                                  frontend/lib/services/shade_analysis_service.dart
                                  frontend/lib/providers/routing_provider.dart
E. UI, overlay, simulation        frontend/lib/ui/widgets/shade_assessment_section.dart
                                  frontend/lib/ui/screens/main_map_screen.dart (overlay)
                                  frontend/lib/services/virtual_shade_clock.dart
```

Each layer has its own acceptance criteria; D and E are NOT considered done
merely because A passed unit tests.

## 2. Data flow (one estimate)

```
Flutter: route card → «Оценить тень» (departure instant chosen by the user)
   ↓ POST /api/v1/environment/shade/analyze
      { departure_at (tz-aware), legs[ {geometry, walking_duration_seconds,
        visit_duration_seconds_after_leg} ], include_segments }
Backend endpoint (worker thread, timeout, concurrency limit)
   ↓ TimeAwareShadeEngine.analyze
      • slices the walk (≈40 m), one solar position per 10-minute bucket
      • asks the read-only pilot dataset for buildings near the walk
      • models each building's shadow (2.5D Minkowski sum, courtyards included)
      • classifies every slice: shaded / exposed / UNKNOWN
   ↓ response { status, dataset_version, provenance, solar_summary,
                shaded/exposed/unknown metres, shade_fraction, confidence,
                coverage + height-provenance metrics, limitations, segments }
Flutter: estimate card + optional shade overlay + (if a real MapKit
         alternative passed the documented gates) an explicit switch offer
```

The same flow runs during a walk (C7.1.2): the live navigation sheet asks for
the REMAINING path — the same request shape, whose legs are the session's own
progress-cut geometry — evaluated from the current instant, without touching
the route or the planning lock (§15.5).

The client never computes shade, never invents a route, and never sends
building geometry.

## 3. Invariants enforced in code, not only documented

1. `shaded + exposed + unknown ≈ total` for every walk (tested, exact to 1e-6).
2. Unknown ground is never reported as sun: outside-coverage ground and the
   shadow envelope of height-less buildings are `unknown`, with their own
   overlay colour.
3. A building without a usable height casts no modelled shadow and is counted.
4. No invented route: alternatives come from real MapKit geometry only; the
   app never draws a straight line as a path.
5. The native request budget (12) is shared across the whole operation and is
   never bypassed by creating a fresh budget per sub-request.
6. A shade estimate belongs to one route and one instant; any route change or
   time change invalidates it (provider guards + `VirtualShadeClock`).
7. C4/C5 machinery is untouched: `MapKitService`, guidance mode, verified
   maneuvers, reroute, GPS hysteresis, the simulator engine and the C5 scenic
   connector geometry all run unchanged; the shade layer is additive.
8. The analysis consumes the PUBLISHED polyline — including confirmed scenic
   connectors — because every leg already carries its post-confirmation
   geometry (C4/C5 fix `521b0c1`); it never analyses a pre-confirmation
   draft.

## 4. What this phase deliberately does NOT do

* It does not generate or optimize routes: MapKit routes, the app estimates.
* It does not claim city-wide coverage: six bounded pilot zones, and every
  response says which zones were touched.
* It does not model trees, canopies, terrain relief, roof shapes or
  transparent structures (documented in `PHASE_C7_1_SHADOW_MODEL.md`).
* It does not fetch weather or use a paid API; the cloud-cover interface is
  context-only and never touches geometry.
* It does not fix the multi-section MapKit annotation limitation of C4 (out of
  scope per §16, still an open known limitation).
