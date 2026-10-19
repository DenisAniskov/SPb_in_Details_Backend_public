# Phase C11 backend architecture — Quiet Engine

## Components

```
app/services/quiet_engine/
  model.py      documented weights, thresholds, limitations
  source.py     strict hash-pinned gzip/JSON loader + local projection + STRtree
  engine.py     length-based geometry chunks -> relative exposure proxy
  wiring.py     one cached dataset per process; disabled unless opted in
app/schemas/quiet.py       strict request/response contract
app/api/v1/endpoints/quiet.py  read-only POST, limits, timeout, concurrency
scripts/acquire_phase_c11_quiet_data.py  reproducible acquisition/verify
scripts/validate_phase_c11_quiet_data.py  standalone provenance validation
data/quiet_spb_pilot.json.gz   versioned real OSM snapshot
data/quiet_spb_pilot.manifest.json  SHA-256 pin
```

## Request flow

1. FastAPI validates the body (1..24 legs, ≤5000 points, timezone-aware time,
   no extra fields).
2. The endpoint rejects routes outside the SPb outer bbox, degenerate or over
   25 km routes with 422.
3. A process-wide semaphore and `asyncio.timeout` bound the worker-thread call.
4. `QuietEngine` splits by length, checks pilot coverage, queries the nearest
   road in a projected STRtree and computes the length-weighted proxy.
5. The endpoint rounds lengths consistently (`unknown = total − known`), adds
   provenance, limitations and diagnostics, and returns 200.

## Boundaries

* No route generation, no MapKit/OSRM call, no route reordering.
* No runtime OSM download; the snapshot is baked into the Docker image from
  `data/`.
* No user accounts, no user storage, no request geometry in logs.
* Dataset disabled/missing => honest `unavailable`; never a synthetic fallback.
* Existing Shade/Puddle/Pollen models, thresholds and provenance are untouched.
* The old `/routes/evaluate` placeholder now points to the separate Quiet
  endpoint instead of claiming that quietness is unimplemented.
