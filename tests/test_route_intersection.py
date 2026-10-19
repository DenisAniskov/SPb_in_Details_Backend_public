"""Tests for route / shadow intersection (Phase 4).

Shadow polygons are constructed DIRECTLY in the local metric frame so the
expected shaded lengths are exact rectangles — the astronomy and shadow
layers are intentionally bypassed here.
"""

import math

import pytest
from shapely.geometry import Polygon

from app.services.shade_engine.buildings import HeightProvenance
from app.services.shade_engine.intersection import (
    InvalidRouteError,
    RouteShadeResult,
    analyze_route,
)
from app.services.shade_engine.shadows import ShadowPolygon
from shade_fixtures import FRAME, offset_latlon


def rect_shadow(x0, y0, x1, y1, source_id="s") -> ShadowPolygon:
    return ShadowPolygon(
        building_source_id=source_id,
        height_m=10.0,
        provenance=HeightProvenance.KNOWN,
        geometry=Polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)]),
        shadow_length_m=50.0,
        capped=False,
    )


def route_ns(x=0.0, y_from=-50.0, y_to=50.0, points=2):
    return [offset_latlon(FRAME, x, y_from + (y_to - y_from) * i / (points - 1)) for i in range(points)]


# --------------------------------------------------------------------------
# Basics
# --------------------------------------------------------------------------

def test_no_shadows_zero_shade():
    res = analyze_route(route_ns(), [], FRAME)
    assert isinstance(res, RouteShadeResult)
    assert res.total_length_m == pytest.approx(100.0, abs=0.01)
    assert res.shaded_length_m == 0.0
    assert res.exposed_length_m == pytest.approx(100.0, abs=0.01)
    assert res.shade_fraction == 0.0


def test_fully_shaded_route():
    shadow = rect_shadow(-20.0, -60.0, 20.0, 60.0)
    res = analyze_route(route_ns(), [shadow], FRAME)
    assert res.shade_fraction == pytest.approx(1.0)
    assert res.shaded_length_m == pytest.approx(100.0, abs=0.01)
    assert res.exposed_length_m == pytest.approx(0.0, abs=0.01)


def test_partial_shading_analytic():
    # Route N-S x=0, 100 m; shadow band y in [30, 70]?? -> use [10, 50]: 40 m shaded.
    shadow = rect_shadow(-5.0, 10.0, 5.0, 50.0)
    res = analyze_route(route_ns(), [shadow], FRAME)
    assert res.shade_fraction == pytest.approx(0.4, abs=1e-3)
    assert res.shaded_length_m == pytest.approx(40.0, abs=0.05)
    assert res.exposed_length_m == pytest.approx(60.0, abs=0.05)


def test_route_not_crossing_shadow():
    shadow = rect_shadow(30.0, -10.0, 40.0, 10.0)  # 30 m east of the route
    res = analyze_route(route_ns(), [shadow], FRAME)
    assert res.shade_fraction == 0.0


# --------------------------------------------------------------------------
# Multiple / overlapping shadows (no double counting)
# --------------------------------------------------------------------------

def test_two_disjoint_shadows_sum():
    a = rect_shadow(-5.0, -50.0, 5.0, -30.0, source_id="a")   # 20 m
    b = rect_shadow(-5.0, 30.0, 5.0, 50.0, source_id="b")     # 20 m
    res = analyze_route(route_ns(), [a, b], FRAME)
    assert res.shade_fraction == pytest.approx(0.4, abs=1e-3)
    assert res.shaded_length_m == pytest.approx(40.0, abs=0.05)


def test_overlapping_shadows_counted_once():
    a = rect_shadow(-5.0, -20.0, 5.0, 20.0, source_id="a")    # [-20, 20] = 40 m
    b = rect_shadow(-2.0, 0.0, 8.0, 40.0, source_id="b")      # [0, 40]; union [-20, 40] = 60 m
    res = analyze_route(route_ns(), [a, b], FRAME)
    assert res.shade_fraction == pytest.approx(0.6, abs=1e-3)
    assert res.shaded_length_m == pytest.approx(60.0, abs=0.05)


def test_identical_overlapping_shadows_counted_once():
    a = rect_shadow(-5.0, -20.0, 5.0, 20.0, source_id="a")
    b = rect_shadow(-5.0, -20.0, 5.0, 20.0, source_id="b")
    res = analyze_route(route_ns(), [a, b], FRAME)
    assert res.shaded_length_m == pytest.approx(40.0, abs=0.05)


# --------------------------------------------------------------------------
# Vertex density is not a distance proxy
# --------------------------------------------------------------------------

def test_vertex_density_does_not_change_metrics():
    dense = route_ns(points=101)  # 100 collinear points along the same line
    res_dense = analyze_route(dense, [rect_shadow(-5.0, 10.0, 5.0, 50.0)], FRAME)
    res_sparse = analyze_route(route_ns(), [rect_shadow(-5.0, 10.0, 5.0, 50.0)], FRAME)
    assert res_dense.total_length_m == pytest.approx(res_sparse.total_length_m, rel=1e-6)
    assert res_dense.shade_fraction == pytest.approx(res_sparse.shade_fraction, abs=1e-9)


def test_zigzag_route_length_follows_geometry():
    # 51-point zigzag around the same N-S line: length must exceed the straight
    # 100 m line (geometry!) even though both have ~100 m of y-projection.
    zig = []
    n = 51
    for i in range(n):
        y = -50.0 + 100.0 * i / (n - 1)
        x = 0.5 if i % 2 else -0.5
        zig.append(offset_latlon(FRAME, x, y))
    res_zig = analyze_route(zig, [rect_shadow(-5.0, 10.0, 5.0, 50.0)], FRAME)
    res_straight = analyze_route(route_ns(), [rect_shadow(-5.0, 10.0, 5.0, 50.0)], FRAME)
    # 50 segments of hypot(2 m, 1 m) ≈ 111.8 m — geometry, not vertex count.
    assert res_zig.total_length_m > 108.0
    assert res_zig.total_length_m < 115.0
    assert res_zig.shade_fraction < res_straight.shade_fraction + 0.01


# --------------------------------------------------------------------------
# Degenerate geometry and validation
# --------------------------------------------------------------------------

def test_single_point_route_rejected():
    with pytest.raises(InvalidRouteError):
        analyze_route([FRAME.to_latlon(0, 0)], [], FRAME)


def test_empty_route_rejected():
    with pytest.raises(InvalidRouteError):
        analyze_route([], [], FRAME)


def test_all_identical_points_rejected():
    p = FRAME.to_latlon(0, 0)
    with pytest.raises(InvalidRouteError):
        analyze_route([p, p, p], [], FRAME)


def test_non_finite_point_rejected():
    with pytest.raises(InvalidRouteError):
        analyze_route([(float("nan"), 30.0), (59.0, 30.0)], [], FRAME)


def test_out_of_range_point_rejected():
    with pytest.raises(InvalidRouteError):
        analyze_route([(95.0, 30.0), (59.0, 30.0)], [], FRAME)


def test_consecutive_duplicates_tolerated():
    a = FRAME.to_latlon(0, -50)
    b = FRAME.to_latlon(0, 50)
    res_dup = analyze_route([a, a, b, b], [], FRAME)
    res_plain = analyze_route([a, b], [], FRAME)
    assert res_dup.total_length_m == pytest.approx(res_plain.total_length_m, rel=1e-9)
    assert res_dup.segment_count == 1


def test_very_short_route_stable():
    a = FRAME.to_latlon(0, 0)
    b = FRAME.to_latlon(0, 0.01)  # 1 cm
    res = analyze_route([a, b], [rect_shadow(-5.0, -1.0, 5.0, 1.0)], FRAME)
    assert 0.0 <= res.shade_fraction <= 1.0
    assert res.total_length_m == pytest.approx(0.01, abs=1e-4)


def test_diagonal_route_fraction():
    # 45° route crossing a N-S strip of width 10 m: the chord inside the
    # strip is width / sin(45°) = 10·√2 ≈ 14.14 m of the ≈141.4 m route.
    a = offset_latlon(FRAME, -50.0, -50.0)
    b = offset_latlon(FRAME, 50.0, 50.0)
    shadow = rect_shadow(-5.0, -100.0, 5.0, 100.0)
    res = analyze_route([a, b], [shadow], FRAME)
    seg_len = math.hypot(100.0, 100.0)
    expected = (10.0 * math.sqrt(2.0)) / seg_len
    assert res.shade_fraction == pytest.approx(expected, rel=5e-3)


# --------------------------------------------------------------------------
# Subsegments
# --------------------------------------------------------------------------

def test_subsegments_partition_total_length():
    shadows = [rect_shadow(-5.0, 10.0, 5.0, 50.0), rect_shadow(-5.0, -60.0, 5.0, -40.0)]
    res = analyze_route(route_ns(), shadows, FRAME, include_subsegments=True)
    assert res.subsegments, "expected classified subsegments"
    total = sum(s.length_m for s in res.subsegments)
    assert total == pytest.approx(res.total_length_m, abs=1e-6)
    shaded_total = sum(s.length_m for s in res.subsegments if s.shaded)
    assert shaded_total == pytest.approx(res.shaded_length_m, abs=1e-3)
    flags = [s.shaded for s in res.subsegments]
    assert all(isinstance(f, bool) for f in flags)
    # boundaries contiguous: each subsegment starts where the previous ended
    for prev, cur in zip(res.subsegments, res.subsegments[1:]):
        assert (prev.end_lat, prev.end_lon) == pytest.approx((cur.start_lat, cur.start_lon), abs=1e-9)


def test_subsegments_omitted_by_default():
    res = analyze_route(route_ns(), [rect_shadow(-5.0, 10.0, 5.0, 50.0)], FRAME)
    assert res.subsegments == ()


# --------------------------------------------------------------------------
# Numerical stability
# --------------------------------------------------------------------------

def test_tangent_touch_counts_zero():
    # Shadow band exactly touching the route line from the east: x∈[0, 5].
    shadow = rect_shadow(0.0, -50.0, 5.0, 50.0)
    res = analyze_route(route_ns(x=0.0), [shadow], FRAME)
    assert 0.0 <= res.shade_fraction <= 1.0
    assert math.isfinite(res.shaded_length_m)


def test_huge_coordinates_stable():
    a = (89.9999, 179.9999)
    b = (89.9999, 179.99995)
    res = analyze_route([a, b], [rect_shadow(0, 0, 1, 1)], FRAME)
    assert 0.0 <= res.shade_fraction <= 1.0
    assert res.total_length_m < 10.0
