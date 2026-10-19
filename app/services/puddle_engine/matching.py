"""Conservative confirmation of native route geometry against pilot OSM ways.

A slice of the walk inherits surface facts from an OSM way ONLY when the
overlap is linear and unambiguous (ТЗ C7.2 §3):

* at least ``MATCH_REQUIRED_SHARE`` of points sampled ALONG the slice lie
  within ``MATCH_MAX_DISTANCE_M`` of the way — so a crossing carriageway or a
  nose-to-tail junction (a single shared point) confirms nothing;
* if two ways both confirm the slice with DIFFERENT surface facts — the
  classic pair of parallel walkways a couple of metres apart — the slice
  stays ``unknown`` instead of borrowing one of them;
* only walkable way types confirm (see ``WALKABLE_HIGHWAY``), so a line lying
  on a road never claims a sidewalk's paving;
* OSM geometry is read-only evidence here: it is never drawn, appended or
  substituted into the route.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.services.shade_engine.geo import LocalFrame, haversine_distance_m
from app.services.puddle_engine.surfaces import SurfaceFacts, surface_facts
from app.services.puddle_engine.ways_store import BBox, PuddleWay

#: Corridor half-width around a native line, metres. Wider than plausible
#: mapping offsets (a few metres) but far below the spacing of parallel
#: walkways that must NOT be confused (handled by the ambiguity rule too).
MATCH_MAX_DISTANCE_M = 8.0

#: Required share of the slice inside the corridor for confirmation.
MATCH_REQUIRED_SHARE = 0.8

#: Sampling step along the slice and its cap (40 m slices → ≤ 21 samples).
SAMPLE_STEP_M = 2.0
MAX_SAMPLES = 128


@dataclass(frozen=True)
class SliceMatch:
    way: PuddleWay | None
    ambiguous: bool
    share: float
    min_distance_m: float
    confirmed_count: int

    @property
    def facts(self) -> SurfaceFacts | None:
        return surface_facts(self.way.tags) if self.way is not None else None


def point_in_bbox(lat: float, lon: float, bbox: BBox) -> bool:
    lat_min, lon_min, lat_max, lon_max = bbox
    return lat_min <= lat <= lat_max and lon_min <= lon <= lon_max


def expand_bbox(bbox: BBox, meters: float) -> BBox:
    """Expand a (lat_min, lon_min, lat_max, lon_max) box by metres."""
    lat_min, lon_min, lat_max, lon_max = bbox
    dlat = meters / 111_320.0
    mid_lat = (lat_min + lat_max) / 2.0
    dlon = meters / (111_320.0 * max(0.05, math.cos(math.radians(mid_lat))))
    return (lat_min - dlat, lon_min - dlon, lat_max + dlat, lon_max + dlon)


def sample_along(
    geometry: tuple[tuple[float, float], ...],
    *,
    step_m: float = SAMPLE_STEP_M,
    max_samples: int = MAX_SAMPLES,
) -> list[tuple[float, float]]:
    """Points every ~``step_m`` ALONG the polyline (endpoints included)."""
    if len(geometry) < 2:
        return list(geometry)
    lengths = [haversine_distance_m(a[0], a[1], b[0], b[1])
               for a, b in zip(geometry, geometry[1:])]
    total = sum(lengths)
    if total <= 0:
        return [geometry[0], geometry[-1]]
    count = min(max_samples, max(2, int(math.ceil(total / step_m)) + 1))
    step = total / (count - 1)

    def point_at(distance: float) -> tuple[float, float]:
        acc = 0.0
        for (lat_a, lon_a), (lat_b, lon_b), seg_len in zip(
                geometry, geometry[1:], lengths):
            if seg_len <= 0:
                continue
            if acc + seg_len >= distance - 1e-9:
                frac = (distance - acc) / seg_len
                return (lat_a + (lat_b - lat_a) * frac, lon_a + (lon_b - lon_a) * frac)
            acc += seg_len
        return geometry[-1]

    return [point_at(i * step) for i in range(count)]


def _point_segment_distance_m(px: float, py: float, ax: float, ay: float,
                              bx: float, by: float) -> float:
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq <= 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def slice_match(samples: list[tuple[float, float]],
                candidates: list[PuddleWay]) -> SliceMatch:
    """The single confirmed way for these samples, or an honest unknown.

    ``candidates`` should already be bbox-filtered by the caller; matching is
    computed in a local metric frame anchored at the first sample, so an
    8 m corridor decision is never distorted by degree math.
    """
    if not samples or not candidates:
        return SliceMatch(None, False, 0.0, math.inf, 0)
    frame = LocalFrame(lat0=samples[0][0], lon0=samples[0][1])
    sample_xy = [frame.to_xy(lat, lon) for lat, lon in samples]

    confirmed: list[tuple[PuddleWay, float, float]] = []
    for way in candidates:
        if not way.matchable():
            continue
        way_xy = [frame.to_xy(lat, lon) for lat, lon in way.geometry]
        inside = 0
        min_distance = math.inf
        for x, y in sample_xy:
            distance = min(_point_segment_distance_m(
                x, y, ax, ay, bx, by)
                for (ax, ay), (bx, by) in zip(way_xy, way_xy[1:]))
            if distance < min_distance:
                min_distance = distance
            if distance <= MATCH_MAX_DISTANCE_M:
                inside += 1
        share = inside / len(sample_xy)
        if share >= MATCH_REQUIRED_SHARE:
            confirmed.append((way, share, min_distance))

    if not confirmed:
        return SliceMatch(None, False, 0.0, math.inf, 0)
    confirmed.sort(key=lambda item: (-item[1], item[2], item[0].osm_id))
    best_way, best_share, best_min = confirmed[0]
    best_facts = surface_facts(best_way.tags)
    ambiguous = any(surface_facts(other.tags) != best_facts
                    for other, _, _ in confirmed[1:])
    if ambiguous:
        return SliceMatch(None, True, best_share, best_min, len(confirmed))
    return SliceMatch(best_way, False, best_share, best_min, len(confirmed))
