"""Tests for the 2.5D shadow model (Phase 3).

Expectations are analytic: for parallel rays the shadow of a prism is the
Minkowski sum of its footprint with a horizontal segment of length
L = H / tan(alpha) pointing OPPOSITE the solar azimuth. For a vertical sweep
this gives the exact area identity  area(shadow) = area(P) + L * x_width(P)
(Cavalieri), which non-convex footprints must also satisfy.
"""

import math

import pytest

from app.services.shade_engine.buildings import HeightProvenance
from app.services.shade_engine.geo import LocalFrame
from app.services.shade_engine.shadows import (
    ShadowModelConfig,
    build_shadows,
    footprint_to_local,
    _swept_shadow,
)
from shade_fixtures import FRAME, rect_footprint, sun_position

TAN = math.tan


def _poly_extent(poly):
    minx, miny, maxx, maxy = poly.bounds
    return minx, miny, maxx, maxy


# --------------------------------------------------------------------------
# Direction: shadow is cast OPPOSITE the solar azimuth
# --------------------------------------------------------------------------

@pytest.mark.parametrize("az,expected_dx_sign,expected_dy_sign", [
    (0.0, 0, -1),      # sun due north → shadow due south
    (90.0, -1, 0),     # sun due east → shadow due west
    (180.0, 0, 1),     # sun due south → shadow due north
    (270.0, 1, 0),     # sun due west → shadow due east
])
def test_shadow_direction_opposite_azimuth(az, expected_dx_sign, expected_dy_sign):
    fp = rect_footprint(FRAME, 0.0, 0.0, 20.0, 10.0, height_m=30.0)
    solar = sun_position(elevation_deg=30.0, azimuth_deg=az)
    report = build_shadows([fp], solar, FRAME)
    assert len(report.shadows) == 1
    local = footprint_to_local(fp, FRAME)
    fpx0, fpy0, fpx1, fpy1 = _poly_extent(local)
    sx0, sy0, sx1, sy1 = _poly_extent(report.shadows[0].geometry)
    dx = (sx0 - fpx0) + (sx1 - fpx1)
    dy = (sy0 - fpy0) + (sy1 - fpy1)
    if expected_dx_sign:
        assert math.copysign(1, dx) == expected_dx_sign
    else:
        assert abs(dx) < 1e-6
    if expected_dy_sign:
        assert math.copysign(1, dy) == expected_dy_sign
    else:
        assert abs(dy) < 1e-6


# --------------------------------------------------------------------------
# Length: L = H / tan(alpha)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("elevation,height", [(10.0, 20.0), (30.0, 30.0), (60.0, 50.0)])
def test_shadow_length_matches_formula(elevation, height):
    fp = rect_footprint(FRAME, 0.0, 0.0, 20.0, 10.0, height_m=height)
    solar = sun_position(elevation_deg=elevation, azimuth_deg=180.0)  # shadow due north
    report = build_shadows([fp], solar, FRAME)
    local = footprint_to_local(fp, FRAME)
    _, _, _, fpy1 = _poly_extent(local)
    _, _, _, sy1 = _poly_extent(report.shadows[0].geometry)
    expected_L = height / TAN(math.radians(elevation))
    assert abs((sy1 - fpy1) - expected_L) < max(0.05, expected_L * 1e-4)


def test_overhead_sun_shadow_collapses_to_footprint():
    fp = rect_footprint(FRAME, 0.0, 0.0, 20.0, 10.0, height_m=30.0)
    solar = sun_position(elevation_deg=89.999, azimuth_deg=180.0)
    report = build_shadows([fp], solar, FRAME)
    local = footprint_to_local(fp, FRAME)
    assert abs(report.shadows[0].geometry.area - local.area) < local.area * 1e-4


def test_non_convex_footprint_area_identity():
    # L-shaped footprint swept due north: area(shadow) = area(P) + L * x_width(P).
    x_left, x_right, y_top, y_bottom = -10.0, 10.0, 5.0, -5.0
    notch_x0, notch_x1, notch_y0 = -3.0, 3.0, 0.0  # courtyard cut into the north side
    ring = [
        FRAME.to_latlon(x_left, y_bottom), FRAME.to_latlon(x_right, y_bottom),
        FRAME.to_latlon(x_right, y_top), FRAME.to_latlon(notch_x1, y_top),
        FRAME.to_latlon(notch_x1, notch_y0), FRAME.to_latlon(notch_x0, notch_y0),
        FRAME.to_latlon(notch_x0, y_top), FRAME.to_latlon(x_left, y_top),
    ]
    from app.services.shade_engine.buildings import BuildingFootprint

    fp = BuildingFootprint(polygon_latlon=tuple(ring), height_m=30.0,
                           provenance=HeightProvenance.KNOWN, source_id="L")
    solar = sun_position(elevation_deg=30.0, azimuth_deg=180.0)
    report = build_shadows([fp], solar, FRAME)
    local = footprint_to_local(fp, FRAME)
    L = 30.0 / TAN(math.radians(30.0))
    x_width = 20.0
    expected_area = local.area + L * x_width
    assert abs(report.shadows[0].geometry.area - expected_area) < expected_area * 1e-6
    # The notch must remain a hole: shadow area must be well below the filled hull.
    hull_area = (x_right - x_left) * (y_top + L - y_bottom)
    assert report.shadows[0].geometry.area < hull_area - 1.0


# --------------------------------------------------------------------------
# Height provenance honesty
# --------------------------------------------------------------------------

def test_unknown_height_building_excluded_not_guessed():
    fp = rect_footprint(FRAME, 0.0, 0.0, 20.0, 10.0, height_m=None,
                        provenance=HeightProvenance.UNAVAILABLE)
    solar = sun_position(elevation_deg=30.0, azimuth_deg=180.0)
    report = build_shadows([fp], solar, FRAME)
    assert report.shadows == ()
    assert report.excluded_unknown_height == 1


def test_levels_estimate_is_labeled_and_used():
    fp = rect_footprint(FRAME, 0.0, 0.0, 20.0, 10.0, height_m=5 * 3.3,
                        provenance=HeightProvenance.LEVELS_ESTIMATE,
                        levels=5, assumed_floor_height_m=3.3, source_id="osm-way-est")
    solar = sun_position(elevation_deg=45.0, azimuth_deg=180.0)
    report = build_shadows([fp], solar, FRAME)
    assert len(report.shadows) == 1
    s = report.shadows[0]
    assert s.provenance == HeightProvenance.LEVELS_ESTIMATE
    assert abs(s.height_m - 16.5) < 1e-9
    local = footprint_to_local(fp, FRAME)
    _, _, _, fpy1 = _poly_extent(local)
    _, _, _, sy1 = _poly_extent(s.geometry)
    assert abs((sy1 - fpy1) - (16.5 / TAN(math.radians(45.0)))) < 0.05


def test_known_and_levels_counts_aggregated():
    fps = [
        rect_footprint(FRAME, -50.0, 0.0, 10.0, 10.0, height_m=20.0,
                       provenance=HeightProvenance.KNOWN, source_id="a"),
        rect_footprint(FRAME, 50.0, 0.0, 10.0, 10.0, height_m=5 * 3.3,
                       provenance=HeightProvenance.LEVELS_ESTIMATE, levels=5,
                       assumed_floor_height_m=3.3, source_id="b"),
        rect_footprint(FRAME, 0.0, 0.0, 10.0, 10.0, height_m=None,
                       provenance=HeightProvenance.UNAVAILABLE, source_id="c"),
    ]
    solar = sun_position(elevation_deg=30.0, azimuth_deg=180.0)
    report = build_shadows(fps, solar, FRAME)
    assert report.excluded_unknown_height == 1
    assert len(report.shadows) == 2


# --------------------------------------------------------------------------
# Low sun, cap, below horizon, invalid geometry
# --------------------------------------------------------------------------

def test_low_sun_excluded():
    fp = rect_footprint(FRAME, 0.0, 0.0, 20.0, 10.0, height_m=30.0)
    solar = sun_position(elevation_deg=1.5, azimuth_deg=180.0)
    report = build_shadows([fp], solar, FRAME, ShadowModelConfig(min_solar_elevation_deg=2.0))
    assert report.shadows == ()
    assert report.excluded_low_sun == 1


def test_shadow_cap_flagged_and_counted():
    fp = rect_footprint(FRAME, 0.0, 0.0, 20.0, 10.0, height_m=100.0)
    solar = sun_position(elevation_deg=3.0, azimuth_deg=180.0)  # L ≈ 1908 m > cap
    report = build_shadows([fp], solar, FRAME, ShadowModelConfig(max_shadow_length_m=1000.0))
    assert len(report.shadows) == 1
    s = report.shadows[0]
    assert s.capped is True
    assert s.shadow_length_m == 1000.0
    assert report.capped_count == 1
    local = footprint_to_local(fp, FRAME)
    _, _, _, fpy1 = _poly_extent(local)
    _, _, _, sy1 = _poly_extent(s.geometry)
    assert abs((sy1 - fpy1) - 1000.0) < 1e-6


def test_below_horizon_no_shadows_at_all():
    fp = rect_footprint(FRAME, 0.0, 0.0, 20.0, 10.0, height_m=30.0)
    solar = sun_position(elevation_deg=-5.0, azimuth_deg=350.0)
    report = build_shadows([fp], solar, FRAME)
    assert report.shadows == ()
    assert report.excluded_unknown_height == 0 and report.excluded_low_sun == 0


def test_self_intersecting_footprint_never_crashes():
    from app.services.shade_engine.buildings import BuildingFootprint

    bowtie = tuple(
        [FRAME.to_latlon(-10, -5), FRAME.to_latlon(10, 5),
         FRAME.to_latlon(10, -5), FRAME.to_latlon(-10, 5)]
    )
    fp = BuildingFootprint(polygon_latlon=bowtie, height_m=30.0,
                           provenance=HeightProvenance.KNOWN, source_id="bowtie")
    solar = sun_position(elevation_deg=30.0, azimuth_deg=180.0)
    report = build_shadows([fp], solar, FRAME)
    # Honest robustness contract: a self-crossing ring is either repaired by
    # the documented buffer(0) step into a valid shadow, or skipped AND
    # counted — it must never crash, and never silently vanish uncounted.
    if report.shadows:
        assert len(report.shadows) == 1
        assert report.shadows[0].geometry.is_valid
        assert report.skipped_invalid_geometry == 0
    else:
        assert report.skipped_invalid_geometry == 1


def test_swept_shadow_direct_displacement_math():
    local = LocalFrame(lat0=0.0, lon0=0.0)
    from shapely.geometry import Polygon

    poly = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
    out = _swept_shadow(poly, 0.0, 40.0)
    assert abs(out.area - (100.0 + 40.0 * 10.0)) < 1e-6  # 100 + L*width
    out_zero = _swept_shadow(poly, 0.0, 0.0)
    assert out_zero is poly
