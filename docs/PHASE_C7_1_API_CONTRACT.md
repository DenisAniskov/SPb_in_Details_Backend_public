# Phase C7.1 — API Contract

`POST /api/v1/environment/shade/analyze`

Status: **IMPLEMENTED**, **UNIT_TESTED**. Device: **NOT_TESTED**.

## 1. Migration from the v1 prototype (explicit, per §11.2)

The v1 prototype took one polyline and one instant
(`route` + `evaluated_at`, response `estimated_shade_distance_m`, status
`ok | sun_below_horizon | unavailable`). No shipped client ever used that
contract, so C7.1 evolves the endpoint IN PLACE rather than duplicating it
under a second address:

| v1 | C7.1 |
| --- | --- |
| `route: [Coordinates]` | `legs: [{geometry, walking_duration_seconds, visit_duration_seconds_after_leg}]` — one walk, real native durations, real visit times |
| `evaluated_at` | `departure_at` (still timezone-aware, still rejected otherwise) |
| status `ok` | `available` / `partial` |
| `estimated_shade_distance_m`, `estimated_sun_distance_m` | `estimated_shaded_meters`, `estimated_exposed_meters`, plus `unknown_meters` |
| — | `dataset_version`, `dataset_provenance`, `solar_summary`, `evaluated_period`, `confidence`, `coverage_metrics`, `height_provenance_metrics`, `classified_segments` |

The v1 honesty rules survive unchanged: a naive datetime is rejected, a
degenerate route is rejected, a route outside the SPb region is rejected, and
without verified building data the response is `unavailable` with a reason and
**no numbers at all**.

## 2. Request

```json
{
  "departure_at": "2026-07-01T14:00:00+03:00",
  "legs": [
    {"geometry": [{"latitude": 59.9380, "longitude": 30.3150},
                  {"latitude": 59.9390, "longitude": 30.3160}],
     "walking_duration_seconds": 420,
     "visit_duration_seconds_after_leg": 1800}
  ],
  "include_segments": false,
  "weather": {"cloud_cover_fraction": 0.2, "source": "manual"}
}
```

Limits (§11.3), enforced before any computation: ≤ 24 legs, ≤ 2000 points per
leg, ≤ 5000 points total, ≤ 25 000 m walking length, request body ≤ the
project's global limit, no building geometry accepted from the client at all.

## 3. Response

```json
{
  "status": "available",
  "method": "shade_engine_c7_1_time_aware_2.5d_noaa",
  "method_version": "c7_1.2",
  "estimate": true,
  "evaluated_period": {"start": "…", "end": "…", "timezone": "UTC+03:00"},
  "dataset_version": "c7_1_pilot_v1",
  "dataset_provenance": "OpenStreetMap (ODbL 1.0, © OpenStreetMap contributors) …",
  "solar_summary": {"min_elevation_deg": 40.1, "max_elevation_deg": 52.9,
                    "min_azimuth_deg": 170.9, "max_azimuth_deg": 233.9,
                    "bucket_count": 4},
  "route_length_meters": 486.2,
  "estimated_shaded_meters": 267.5,
  "estimated_exposed_meters": 197.4,
  "unknown_meters": 21.3,
  "shade_fraction": 0.55,
  "confidence": "medium",
  "coverage_metrics": {"dataset_zones_touched": ["centre_nevsky"],
                       "outside_coverage_meters": 0.0, "coverage_share": 1.0},
  "height_provenance_metrics": {"externally_verified": 0, "osm_declared": 1,
                                "levels_estimate": 53, "unavailable": 25,
                                "buildings_considered": 79},
  "weather_relevance": "high",
  "weather_relevance_explanation": "Малооблачно (20%): …",
  "limitations": ["…"],
  "classified_segments": [
    {"leg_index": 0,
     "geometry": [{"latitude": 59.9390, "longitude": 30.3160},
                  {"latitude": 59.9394, "longitude": 30.3167},
                  {"latitude": 59.9398, "longitude": 30.3171}],
     "length_meters": 40.0,
     "shaded_meters": 32.1, "unknown_meters": 0.0,
     "state": "mixed", "starts_at_seconds": 0.0, "ends_at_seconds": 31.6,
     "shaded_spans": [[7.9, 40.0]], "unknown_spans": []}
  ],
  "reason": null
}
```

Guarantees a client may rely on:

* `estimated_shaded_meters + estimated_exposed_meters + unknown_meters ≈
  route_length_meters` (exact in the engine; rounding in the response is to
  0.1 m).
* `shade_fraction`, `estimated_*` and `unknown_meters` are `null` unless an
  estimate was actually produced (`status` `available`/`partial`).
* `unknown_meters` is never reported as sun, and `state: "unknown"` segments
  are explicitly marked as such.
* `dataset_version` + `dataset_provenance` identify the data behind every
  number.

### 3.1 Classified segments carry geometry and MEASURED positions (C7.1.1)

Each entry of `classified_segments` describes one ≈40 m slice of the walk and
carries two things a client cannot derive on its own:

* `geometry` — the slice's **real polyline**, including every interior vertex
  of the route. A slice is never reduced to a chord between its cut points, so
  a walkway that turns inside a slice is drawn and classified as walked.
* `shaded_spans` / `unknown_spans` — `[start, end]` intervals in **metres along
  the slice**, i.e. WHERE each shadow band begins and ends. Their totals equal
  `shaded_meters` / `unknown_meters`; the rest of the slice is
  `estimated_exposed`. Spans never overlap (`shaded` wins over `unknown`), stay
  inside `[0, length_meters]`, and no boundary is inferred client-side.

`state` labels the slice: `estimated_shaded` = the whole slice is shaded,
`estimated_exposed` = the whole slice is sunlit, `unknown` = the whole slice is
unclassified (outside coverage / inside a height-less envelope), `mixed` = more
than one of those inside the slice, with the spans saying exactly where.

Rounding: lengths and span boundaries are rounded to 0.1 m in the response; the
engine's own sums are exact and are asserted in tests.

`method_version` is `c7_1.2` since C7.1.1 (was `c7_1.1`): the slice geometry,
the unknown-band arithmetic and the segment contract changed, and clients may
branch on it.

## 4. Behaviour under load and failure

* The GEOS/Shapely work runs in a worker thread (`asyncio.to_thread`) behind
  a semaphore (`SHADE_MAX_CONCURRENT_ANALYSES`, default 2) and a timeout
  (`SHADE_ANALYSIS_TIMEOUT_SECONDS`, default 10 s). A timeout returns
  `503` with a Russian message; the event loop is never blocked.
* Unexpected errors are sanitized by the project's global handler (no
  internals leak to the client).
* GPS tracks are not logged; only the request's own geometry is processed,
  and nothing is persisted.

## 5. Client usage (Flutter)

`BackendShadeAnalysisService.analyzeWalk` builds the payload from real
`RouteLegModel`s (geometry + native seconds + visit minutes) and formats the
departure with its UTC offset — Dart's `toIso8601String()` omits the offset
for local times, and the backend rejects naive instants by design.

The same request shape serves the C7.1.2 mid-walk refresh: the client sends the
remaining path as its own legs (the current leg cut at the engine's accepted
progress, then every leg still ahead, its walking time scaled by the remaining
share of the leg) with `departure_at` = the current instant. Nothing in this
contract changed; the backend still evaluates exactly the geometry it is given.
