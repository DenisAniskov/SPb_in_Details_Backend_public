"""Shared helpers for Shade Engine tests.

Everything built here is SYNTHETIC test geometry. Nothing in this module may
be presented as real Saint Petersburg building data (see
buildings.SyntheticBuildingProvider, whose provenance string self-identifies).
"""

import math  # noqa: F401 — re-exported for analytic expectations in test modules

from app.services.shade_engine.buildings import (
    BuildingFootprint,
    HeightProvenance,
    SyntheticBuildingProvider,
)
from app.services.shade_engine.geo import LocalFrame

# Fixed analysis frame for tests: SPb, near Palace Square.
FRAME = LocalFrame(lat0=59.9398, lon0=30.3146)

LAT_M = 111_320.0  # meters per degree of latitude (approx, consistent with LocalFrame R)


def offset_latlon(frame: LocalFrame, x_m: float, y_m: float) -> tuple[float, float]:
    """Local meters (x=east, y=north) -> WGS84 via the frame (exact inverse)."""
    return frame.to_latlon(x_m, y_m)


def rect_footprint(
    frame: LocalFrame,
    center_x: float,
    center_y: float,
    width_m: float,
    depth_m: float,
    height_m: float,
    provenance: HeightProvenance = HeightProvenance.KNOWN,
    levels: float | None = None,
    assumed_floor_height_m: float | None = None,
    source_id: str | None = None,
) -> BuildingFootprint:
    """Axis-aligned rectangular footprint centered at local (x, y) meters."""
    x0, x1 = center_x - width_m / 2.0, center_x + width_m / 2.0
    y0, y1 = center_y - depth_m / 2.0, center_y + depth_m / 2.0
    corners = [offset_latlon(frame, x0, y0), offset_latlon(frame, x1, y0),
               offset_latlon(frame, x1, y1), offset_latlon(frame, x0, y1)]
    return BuildingFootprint(
        polygon_latlon=tuple(corners),
        height_m=height_m,
        provenance=provenance,
        levels=levels,
        assumed_floor_height_m=assumed_floor_height_m,
        source="SYNTHETIC TEST FIXTURE — NOT REAL SPb DATA",
        source_id=source_id,
    )


def synthetic_provider(*footprints: BuildingFootprint) -> SyntheticBuildingProvider:
    return SyntheticBuildingProvider(footprints=tuple(footprints))


def ns_route(frame: LocalFrame, x: float, y_from: float, y_to: float, points: int = 2):
    """North-south route polyline with ``points`` vertices (x constant)."""
    if points < 2:
        raise ValueError("points must be >= 2")
    step = (y_to - y_from) / (points - 1)
    return [offset_latlon(frame, x, y_from + i * step) for i in range(points)]


def sun_position(elevation_deg: float, azimuth_deg: float):
    """A SolarPosition built directly (bypasses the astronomy for geometry tests)."""
    from app.services.shade_engine.solar import SolarPosition

    return SolarPosition(
        elevation_deg=elevation_deg,
        azimuth_deg=azimuth_deg,
        zenith_deg=90.0 - elevation_deg,
        declination_deg=0.0,
        refraction_applied=False,
    )
