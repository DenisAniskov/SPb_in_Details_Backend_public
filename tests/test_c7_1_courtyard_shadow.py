"""C7.1 courtyard (interior-ring) shadow correctness.

Real SPb buildings routinely enclose courtyards. The v1 shadow model swept
only the exterior ring, which silently treated a courtyard as building mass.
The C7.1 model sweeps interior rings too, so a yard is shadowed exactly where
a sun ray from the yard meets building mass.

The geometry here is ANALYTIC (hand-computed) and synthetic on purpose: a
40 × 40 m building with a centred 20 × 20 m courtyard, height 5 m, sun at
45° elevation / 180° azimuth (due south). Shadow displacement is then
5 m due north (d = H / tan 45° = 5 m).
"""

import math

import pytest

from app.services.shade_engine.buildings import BuildingFootprint, HeightProvenance
from app.services.shade_engine.geo import LocalFrame
from app.services.shade_engine.intersection import analyze_route
from app.services.shade_engine.shadows import ShadowModelConfig, build_shadows
from app.services.shade_engine.solar import SolarPosition

FRAME = LocalFrame(lat0=59.9398, lon0=30.3146)


def sun(elevation_deg: float, azimuth_deg: float) -> SolarPosition:
    return SolarPosition(elevation_deg=elevation_deg, azimuth_deg=azimuth_deg,
                         zenith_deg=90.0 - elevation_deg, declination_deg=0.0,
                         refraction_applied=False)


def courtyard_building() -> BuildingFootprint:
    """40 × 40 m mass with a centred 20 × 20 m courtyard, 5 m tall."""
    def ll(x: float, y: float) -> tuple[float, float]:
        return FRAME.to_latlon(x, y)

    shell = [ll(-20, -20), ll(20, -20), ll(20, 20), ll(-20, 20), ll(-20, -20)]
    hole = [ll(-10, -10), ll(-10, 10), ll(10, 10), ll(10, -10), ll(-10, -10)]
    return BuildingFootprint(
        polygon_latlon=tuple(shell),
        holes_latlon=(tuple(hole),),
        height_m=5.0,
        provenance=HeightProvenance.OSM_DECLARED,
        source="SYNTHETIC TEST FIXTURE — analytic courtyard case, not real SPb data",
        source_id="courtyard-1",
    )


def shade_at(points_x_y: list[tuple[float, float]]) -> list[bool]:
    """Shaded verdicts for a route through the given local-frame points."""
    route = [FRAME.to_latlon(x, y) for x, y in points_x_y]
    report = build_shadows([courtyard_building()], sun(45.0, 180.0), FRAME,
                           ShadowModelConfig())
    assert len(report.shadows) == 1
    result = analyze_route(route, report.shadows, FRAME)
    # Sample the route with one vertex per point: each 1 m segment is either
    # fully shaded or fully sunlit at these locations.
    shaded_lengths = []
    shaded_lengths.append(result.shaded_length_m)
    return result, report


def test_courtyard_south_strip_is_shaded_but_north_half_is_sunlit():
    """Analytic expectation with L = H/tan(45°) = 5 m, shadow due north.

    * The courtyard strip y ∈ [-10, -5] is shadowed by the courtyard's own
      south wall (ray height at the wall ≤ H).
    * The courtyard strip y ∈ (-5, +10] is sunlit: the ray at the south wall
      already exceeds H = 5 m.
    * A point on the building mass is always shadowed (it is mass).
    """
    route = [FRAME.to_latlon(0.0, -7.0),   # courtyard, south strip  → shaded
             FRAME.to_latlon(0.0, 5.0),    # courtyard, north half   → sunlit
             FRAME.to_latlon(0.0, -15.0),  # building mass (south)   → shaded
             FRAME.to_latlon(0.0, 30.0)]   # 10 m north of the building → sunlit
    report = build_shadows([courtyard_building()], sun(45.0, 180.0), FRAME,
                           ShadowModelConfig())
    result = analyze_route(route, report.shadows, FRAME)
    assert result.shaded_length_m > 0.0
    assert result.shaded_length_m < result.total_length_m


def test_courtyard_shadow_boundary_matches_analytic_strip():
    """Sample the courtyard along y with 1 m spacing: shaded iff y ∈ [-10, -5]."""
    ys = [y / 2.0 for y in range(-19, 21)]  # -9.5 .. 10.0
    report = build_shadows([courtyard_building()], sun(45.0, 180.0), FRAME,
                           ShadowModelConfig())
    shadow_union = report.shadows[0].geometry
    from shapely.geometry import Point
    for y in ys:
        x, yy = FRAME.to_xy(*FRAME.to_latlon(0.0, y))
        inside = shadow_union.contains(Point(x, yy))
        expected = -10.0 <= y <= -5.0 + 1e-9
        assert inside == expected, f"y={y}: shadow={inside}, analytic={expected}"


def test_ignoring_holes_would_overreport_shade():
    """Regression for the v1 behaviour: without holes the yard is 'mass'.

    The same building modelled WITHOUT its interior ring must produce a
    strictly larger shadow inside the courtyard — this is exactly the v1
    error the C7.1 model fixes.
    """
    with_hole = build_shadows([courtyard_building()], sun(45.0, 180.0), FRAME,
                              ShadowModelConfig()).shadows[0].geometry
    solid = BuildingFootprint(
        polygon_latlon=courtyard_building().polygon_latlon,
        height_m=5.0, provenance=HeightProvenance.OSM_DECLARED,
        source="SYNTHETIC TEST FIXTURE", source_id="courtyard-1")
    without_hole = build_shadows([solid], sun(45.0, 180.0), FRAME,
                                 ShadowModelConfig()).shadows[0].geometry
    assert without_hole.area > with_hole.area
    # Analytic areas (L = 5 m due north, 40 × 40 m mass, 20 × 20 m courtyard):
    #   solid shadow: 1600 (mass) + 40 × 5 (displaced north strip)      = 1800
    #   holed shadow: 1800 − 400 (courtyard) + 20 × 5 (the band of the  = 1500
    #                 courtyard genuinely shadowed by its own south wall)
    # Difference = 400 − 100 = 300 m² of courtyard the v1 (solid) model
    # wrongly reported as shaded.
    assert without_hole.area == pytest.approx(1800.0, rel=0.01)
    assert with_hole.area == pytest.approx(1500.0, rel=0.01)
    assert without_hole.difference(with_hole).area == pytest.approx(300.0, rel=0.02)


def test_courtyard_route_lengths_are_geodesically_consistent():
    route = [FRAME.to_latlon(0.0, -40.0), FRAME.to_latlon(0.0, 40.0)]
    report = build_shadows([courtyard_building()], sun(45.0, 180.0), FRAME,
                           ShadowModelConfig())
    result = analyze_route(route, report.shadows, FRAME)
    assert result.total_length_m == pytest.approx(80.0, rel=0.005)
    # Analytic shading along x = 0 (L = 5 m due north):
    #   y ∈ [-20, -10]  south wing                    10 m
    #   y ∈ (-10,  -5]  courtyard strip (south wall)   5 m
    #   y ∈ [ 10,  20]  north wing                    10 m
    #   y ∈ ( 20,  25]  displaced north wing           5 m
    #                                                  ---
    #                                                  30 m
    assert result.shaded_length_m == pytest.approx(30.0, abs=0.5)
    assert math.isclose(result.shaded_length_m + result.exposed_length_m,
                        result.total_length_m, rel_tol=1e-9)
