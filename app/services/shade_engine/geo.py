"""Geodesic and planar helpers for the Shade Engine.

Two complementary instruments, each used only where it is fit for purpose:

1. Haversine distance on a sphere (WGS84 coordinates, mean Earth radius) —
   used for REPORTED distances (route segment lengths). Same convention as
   ``app.services.smart_walk_service`` (EARTH_RADIUS_METERS = 6 371 000 m).

2. A local equirectangular tangent plane anchored at a reference point —
   used ONLY for planar geometry operations (shadow polygon construction and
   line/polygon intersection). Accuracy of the planar approximation at SPb
   latitudes (59.5–60.5° N) for extents of ~25 km:
       * north/south scale: meridian radius of curvature at 60° differs from
         the mean Earth radius by ≈ +0.2 %;
       * east/west scale: using cos(lat0) instead of cos(lat) along a point
         0.35° of latitude away distorts distances by ≈ 1 %.
   Reported shade/exposed distances are therefore NOT taken from the plane:
   each route segment's shaded fraction is measured in the plane and then
   applied to the segment's geodesic (haversine) length. This keeps the
   reported distances geodesically consistent while the ≤ ~1 % planar
   distortion only affects the classification boundary, not the metrics.
"""

import math
from dataclasses import dataclass

# Kept identical to app.services.smart_walk_service.EARTH_RADIUS_METERS on purpose.
EARTH_RADIUS_METERS = 6_371_000.0


def haversine_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters between two WGS84 points.

    Spherical model (mean radius). For city-scale segments the difference to
    the true WGS84 geodesic is far below the other error sources of the shade
    estimate (building heights, roof shapes), so a spherical model is used
    and documented here rather than pretending sub-0.1 % precision.
    """
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = (math.sin(d_phi / 2.0) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2.0) ** 2)
    a = min(1.0, max(0.0, a))  # floating-point clamp, same as smart_walk_service
    return EARTH_RADIUS_METERS * 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


@dataclass(frozen=True)
class LocalFrame:
    """Local equirectangular metric frame anchored at (lat0, lon0).

    x = east, y = north, meters. The transform is affine, so the inverse is
    exact — geometry mapped to the plane and back loses nothing except the
    (documented) planar scale distortion above.
    """

    lat0: float
    lon0: float

    @staticmethod
    def covering(points: list[tuple[float, float]]) -> "LocalFrame":
        """Frame anchored at the centroid of ``points`` (minimizes distortion)."""
        lat0 = sum(p[0] for p in points) / len(points)
        lon0 = sum(p[1] for p in points) / len(points)
        return LocalFrame(lat0=lat0, lon0=lon0)

    def to_xy(self, lat: float, lon: float) -> tuple[float, float]:
        x = EARTH_RADIUS_METERS * math.radians(lon - self.lon0) * math.cos(math.radians(self.lat0))
        y = EARTH_RADIUS_METERS * math.radians(lat - self.lat0)
        return x, y

    def to_latlon(self, x: float, y: float) -> tuple[float, float]:
        lat = self.lat0 + math.degrees(y / EARTH_RADIUS_METERS)
        lon = self.lon0 + math.degrees(x / (EARTH_RADIUS_METERS * math.cos(math.radians(self.lat0))))
        return lat, lon
