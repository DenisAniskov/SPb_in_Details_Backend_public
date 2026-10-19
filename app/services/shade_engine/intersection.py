"""Route / shadow intersection — Phase 4 of the Shade Engine foundation.

Pure algorithmic API: given a route polyline and shadow polygons, classify
the route into sun-exposed and shaded portions.

DISTANCE HONESTY:

    Vertex count is NEVER used as a distance proxy. Every route segment
    contributes its geodesic (haversine) length; the shaded portion of each
    segment is measured in the local metric plane as a fraction of that
    segment, and the fraction is applied to the geodesic length (see
    geo.LocalFrame for why this two-instrument approach is used).

    The result is an ESTIMATE with city-scale precision (roughly meters,
    bounded by footprint/height data quality), never centimeter precision.
"""

import math
from dataclasses import dataclass, field
from typing import Sequence

from shapely.geometry import LineString
from shapely.ops import unary_union

from app.services.shade_engine.geo import LocalFrame, haversine_distance_m
from app.services.shade_engine.shadows import ShadowPolygon


class InvalidRouteError(ValueError):
    """Raised for routes the analysis cannot honestly process."""


@dataclass(frozen=True)
class RouteSubSegment:
    """A contiguous piece of the route, classified for reporting."""

    start_lat: float
    start_lon: float
    end_lat: float
    end_lon: float
    length_m: float
    shaded: bool


@dataclass(frozen=True)
class RouteShadeResult:
    total_length_m: float
    shaded_length_m: float
    exposed_length_m: float
    shade_fraction: float
    segment_count: int
    subsegments: tuple[RouteSubSegment, ...] = field(default=())


def _shaded_ranges(hit, seg_x0: float, seg_y0: float, seg_dx: float, seg_dy: float) -> list[tuple[float, float]]:
    """Parameter ranges [(t0, t1)] (t in [0, 1]) of a route segment in shadow.

    ``hit`` is the intersection of one route segment with the shadow union.
    Every LineString part lies ON the segment (possibly with reversed vertex
    order), so each part's extent is recovered by projecting its endpoints
    onto the segment direction — accumulated part-internal distance would
    misplace parts that do not start at the segment origin. Point-sized
    contacts (tangency) contribute zero length and are ignored by design.
    """
    if hit.is_empty:
        return []
    parts = [hit] if hit.geom_type == "LineString" else (
        [g for g in hit.geoms if g.geom_type == "LineString"] if hasattr(hit, "geoms") else [])
    denom = seg_dx * seg_dx + seg_dy * seg_dy
    if denom <= 0.0:
        return []
    ranges: list[tuple[float, float]] = []
    for g in parts:
        coords = list(g.coords)
        if len(coords) < 2:
            continue
        t_first = ((coords[0][0] - seg_x0) * seg_dx + (coords[0][1] - seg_y0) * seg_dy) / denom
        t_last = ((coords[-1][0] - seg_x0) * seg_dx + (coords[-1][1] - seg_y0) * seg_dy) / denom
        if abs(t_last - t_first) <= 1e-12:
            continue
        ranges.append((min(t_first, t_last), max(t_first, t_last)))
    ranges.sort()
    merged: list[tuple[float, float]] = []
    for a, b in ranges:
        if merged and a - merged[-1][1] <= 1e-9:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def _make_sub(
    frame: LocalFrame,
    t0: float,
    t1: float,
    seg_hav: float,
    start_ll: tuple[float, float],
    end_ll: tuple[float, float],
    shaded: bool,
) -> RouteSubSegment:
    x0, y0 = frame.to_xy(*start_ll)
    x1, y1 = frame.to_xy(*end_ll)
    la, lo = frame.to_latlon(x0 + (x1 - x0) * t0, y0 + (y1 - y0) * t0)
    lb, lob = frame.to_latlon(x0 + (x1 - x0) * t1, y0 + (y1 - y0) * t1)
    return RouteSubSegment(
        start_lat=la, start_lon=lo, end_lat=lb, end_lon=lob,
        length_m=(t1 - t0) * seg_hav, shaded=shaded,
    )


def analyze_route(
    route_latlon: Sequence[tuple[float, float]],
    shadows: Sequence[ShadowPolygon],
    frame: LocalFrame,
    include_subsegments: bool = False,
) -> RouteShadeResult:
    """Classify a route polyline against the union of shadow polygons.

    Raises ``InvalidRouteError`` for routes with fewer than 2 distinct
    points or out-of-range coordinates. Consecutive duplicate points are
    tolerated (they contribute zero length); a route that is entirely
    degenerate (all points identical) raises ``InvalidRouteError`` — a
    zero-length route has no honest shade fraction.
    """
    if route_latlon is None or len(route_latlon) < 2:
        raise InvalidRouteError("route needs at least 2 points")
    clean: list[tuple[float, float]] = []
    for lat, lon in route_latlon:
        if not (math.isfinite(lat) and math.isfinite(lon)
                and -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            raise InvalidRouteError(f"route point out of range: {(lat, lon)!r}")
        if not clean or clean[-1] != (lat, lon):
            clean.append((lat, lon))
    if len(clean) < 2:
        raise InvalidRouteError("route has fewer than 2 distinct points")

    shadow_union = unary_union([s.geometry for s in shadows]) if shadows else None

    total_len = 0.0
    shaded_len = 0.0
    subsegments: list[RouteSubSegment] = []

    for (lat0, lon0), (lat1, lon1) in zip(clean, clean[1:]):
        seg_hav = haversine_distance_m(lat0, lon0, lat1, lon1)
        total_len += seg_hav
        x0, y0 = frame.to_xy(lat0, lon0)
        x1, y1 = frame.to_xy(lat1, lon1)
        seg_local_len = math.hypot(x1 - x0, y1 - y0)
        if seg_hav <= 0.0 or seg_local_len <= 0.0:
            continue  # degenerate segment: contributes zero length either way

        if shadow_union is None or shadow_union.is_empty:
            shaded_frac = 0.0
            runs: list[tuple[float, float]] = []
        else:
            hit = LineString([(x0, y0), (x1, y1)]).intersection(shadow_union)
            runs = _shaded_ranges(hit, x0, y0, x1 - x0, y1 - y0)
            shaded_local = sum(b - a for a, b in runs) * seg_local_len
            shaded_frac = min(1.0, max(0.0, shaded_local / seg_local_len))

        shaded_len += shaded_frac * seg_hav

        if include_subsegments:
            cursor = 0.0
            for a, b in runs:
                a, b = max(0.0, min(a, 1.0)), max(0.0, min(b, 1.0))
                if a - cursor > 1e-9:
                    subsegments.append(_make_sub(frame, cursor, a, seg_hav, (lat0, lon0), (lat1, lon1), False))
                subsegments.append(_make_sub(frame, a, b, seg_hav, (lat0, lon0), (lat1, lon1), True))
                cursor = b
            if 1.0 - cursor > 1e-9:
                subsegments.append(_make_sub(frame, cursor, 1.0, seg_hav, (lat0, lon0), (lat1, lon1), False))

    shade_fraction = min(1.0, max(0.0, shaded_len / total_len)) if total_len > 0 else 0.0
    return RouteShadeResult(
        total_length_m=total_len,
        shaded_length_m=shaded_len,
        exposed_length_m=total_len - shaded_len,
        shade_fraction=shade_fraction,
        segment_count=len(clean) - 1,
        subsegments=tuple(subsegments),
    )
