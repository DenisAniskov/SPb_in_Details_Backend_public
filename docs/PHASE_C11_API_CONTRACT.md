# Phase C11 — Quiet API contract (v1)

## Endpoint

`POST /api/v1/environment/quiet/analyze`

Read-only. It analyses an EXISTING walk and never builds, chooses or changes a
route. It issues zero MapKit/OSRM requests.

## Request

```json
{
  "assessed_at": "2026-10-20T12:00:00+03:00",
  "include_legs": true,
  "legs": [
    {
      "geometry": [
        {"latitude": 59.9387, "longitude": 30.3158},
        {"latitude": 59.9410, "longitude": 30.3150}
      ]
    }
  ]
}
```

* `assessed_at` optional; if present it must have a timezone. It is used only
  to align the report. The model is static.
* `legs` 1..24; each 2..2000 points; total ≤5000 points.
* No client-supplied dataset, weights, URL or coefficients are accepted
  (`extra="forbid"`).
* The existing 16 KiB request-body cap applies. Real geometry that does not fit
  is refused (413/422), never shortened.

## Response (available)

```json
{
  "status": "available",
  "estimate": true,
  "confidence": "low",
  "method": "relative_road_noise_exposure_proxy",
  "method_version": "c11_1",
  "dataset_version": "c11_quiet_spb_central_v1",
  "dataset_sha256": "c191b22a…",
  "evaluated_at": "2026-10-20T12:00:00+03:00",
  "temporal_model": "static_osm_network",
  "traffic_time_modeled": false,
  "time_context_note": "Время используется только для согласования отчёта…",
  "route_length_meters": 146.9,
  "known_meters": 146.9,
  "unknown_meters": 0.0,
  "coverage_share": 1.0,
  "road_noise_exposure_index": 0.0505,
  "index_scale": "Безразмерный относительный прокси 0..1…",
  "low_exposure_share": 1.0,
  "major_road_adjacent_share": 0.0,
  "mean_nearest_road_distance_m": 78.4,
  "max_road_class_nearby": "secondary",
  "legs": [
    {
      "leg_index": 0,
      "length_meters": 146.9,
      "known_meters": 146.9,
      "unknown_meters": 0.0,
      "road_noise_exposure_index": 0.0505,
      "major_road_adjacent_meters": 0.0,
      "low_exposure_meters": 146.9
    }
  ],
  "reasons": [],
  "limitations": ["…не дБ…"],
  "assumptions": ["…"],
  "source_provenance": {"license": "ODbL-1.0", "attribution": "© OpenStreetMap contributors", "…": "…"},
  "diagnostics": {"road_count": 3595, "…": "…"}
}
```

`status` is `available | partial | unavailable | outside_coverage`.
For `unavailable` and `outside_coverage`, `estimate` is false, the index and
shares are null, and `reasons` names the cause (`quiet_engine_disabled`,
`quiet_dataset_unavailable`, `route_outside_pilot`, …). For `partial`, the
index describes only `known_meters`; `unknown_meters` is never called quiet.

`traffic_time_modeled` is always false. The field exists so a client can reject
a future payload that falsely turns this static proxy into an hourly forecast.

## Errors and limits

| Condition | Answer |
| --- | --- |
| outside SPb metropolitan bbox / invalid coordinate / zero length | 422 |
| over 25 km or over 5000 points | 422 |
| body larger than 16 KiB | 413 (middleware) |
| analysis timeout / engine failure | 503, safe message |
| engine disabled or dataset missing | 200 `unavailable` (feature availability, not a server fault) |

The endpoint does not log route geometry or GPS. It does not store requests.
No upstream call is made. Concurrency is bounded by
`QUIET_MAX_CONCURRENT_ANALYSES`; the engine runs in a worker thread under
`QUIET_ANALYSIS_TIMEOUT_SECONDS`.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `QUIET_ENGINE_ENABLED` | `false` | explicit opt-in; staging enables after verification |
| `QUIET_DATASET` | `data/quiet_spb_pilot.json.gz` | packaged snapshot |
| `QUIET_DATASET_MANIFEST` | `data/quiet_spb_pilot.manifest.json` | hash pin |
| `QUIET_MAX_CONCURRENT_ANALYSES` | `2` | worker limit |
| `QUIET_ANALYSIS_TIMEOUT_SECONDS` | `10` | analysis deadline |
