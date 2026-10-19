"""2.5D shadow model — Phase 3 of the Shade Engine foundation.

MODEL (deliberately honest and simple):

    Each building is a vertical prism over its footprint polygon, standing on
    FLAT ground, lit by PARALLEL sun rays. The shadow on the ground is then
    the Minkowski sum of the footprint with a horizontal displacement vector:

        displacement = L * (unit vector opposite the solar azimuth)
        L = H / tan(alpha)

    where H is the (provenanced) building height and alpha the solar
    elevation. For a CONVEX footprint this equals the convex hull of the
    footprint and its translate; for non-convex footprints the exact swept
    region is computed as the union of the footprint, its translate, and the
    swept quad of every edge (Minkowski sum distributes over union), so
    concave courtyards are NOT falsely filled in.

WHAT IS DELIBERATELY NOT MODELED (documented limitations, surfaced by the
engine in every API response):

    * roof shapes (gables, towers) — the prism is flat-topped;
    * terrain relief — the ground is flat;
    * trees, canopies, bridges — only building-like polygons enter the model;
    * reflected/indirect light — a point in shadow is simply not directly lit;
    * shadow softness/penumbra — edges are hard (the Sun is treated as a point).

LOW-SUN BEHAVIOR (stated, not hidden):

    L = H / tan(alpha) diverges as alpha -> 0. Two explicit, configurable
    guards replace the unphysical infinity:

    * ``min_solar_elevation_deg`` (default 2°): below this, shadow direction
      is dominated by footprint errors and the length exceeds city scale, so
      the building is EXCLUDED from this analysis and counted.
    * ``max_shadow_length_m`` (default 1000 m): shadows longer than this are
      CAPPED, and the capping is counted and reported. Capping always makes
      the shade estimate an UNDERestimate for tall buildings at low sun.
"""

import math
from dataclasses import dataclass
from typing import Sequence

from shapely.affinity import translate
from shapely.geometry import MultiPoint, Point, Polygon
from shapely.geometry.polygon import orient
from shapely.ops import unary_union

from app.services.shade_engine.buildings import BuildingFootprint, HeightProvenance
from app.services.shade_engine.geo import LocalFrame
from app.services.shade_engine.solar import SolarPosition


@dataclass(frozen=True)
class ShadowModelConfig:
    min_solar_elevation_deg: float = 2.0
    max_shadow_length_m: float = 1000.0


@dataclass(frozen=True)
class ShadowPolygon:
    """One building's ground shadow in the local metric frame."""

    building_source_id: str | None
    height_m: float
    provenance: HeightProvenance
    geometry: Polygon            # planar meters in the analysis LocalFrame
    shadow_length_m: float       # horizontal displacement applied (post-cap)
    capped: bool                 # True when length was limited by the cap


@dataclass(frozen=True)
class ShadowBuildReport:
    shadows: tuple[ShadowPolygon, ...]
    excluded_unknown_height: int
    excluded_low_sun: int
    skipped_invalid_geometry: int
    capped_count: int


def footprint_to_local(fp: BuildingFootprint, frame: LocalFrame) -> Polygon:
    shell = [frame.to_xy(lat, lon) for lat, lon in fp.polygon_latlon]
    holes = [[frame.to_xy(lat, lon) for lat, lon in ring] for ring in fp.holes_latlon]
    poly = Polygon(shell, holes)
    if not poly.is_valid:
        poly = poly.buffer(0)  # documented repair for self-touching rings
    if poly.is_empty or poly.area <= 0:
        raise ValueError("footprint is degenerate after projection")
    return poly


def _swept_shadow(footprint: Polygon, dx: float, dy: float) -> Polygon:
    """Exact Minkowski sum of a (possibly non-convex, holed) polygon with a segment.

    Union of the footprint, the translated footprint, and the convex quad
    swept by EVERY boundary edge — exterior and interior (courtyard) rings
    alike. The identity MS(P, s) = P ∪ (P+s) ∪ ⋃ MS(edge, s) holds for
    multiply-connected polygons, so a courtyard is shadowed exactly where a
    sun ray from the yard actually meets building mass, and stays sunlit
    where it does not. ``shapely.ops.unary_union`` merges the overlapping
    parts into a valid (multi)polygon.
    """
    if dx == 0.0 and dy == 0.0:
        return footprint
    moved = translate(footprint, dx, dy)
    parts = [footprint, moved]
    for ring in [footprint.exterior, *footprint.interiors]:
        coords = list(ring.coords)
        for i in range(len(coords) - 1):
            swept = MultiPoint([
                Point(coords[i]), Point(coords[i + 1]),
                Point(coords[i + 1][0] + dx, coords[i + 1][1] + dy),
                Point(coords[i][0] + dx, coords[i][1] + dy),
            ]).convex_hull
            parts.append(swept)
    return unary_union(parts)


def _shadow_displacement(solar: SolarPosition, height_m: float, config: ShadowModelConfig) -> tuple[float, float, float, bool]:
    """Horizontal shadow displacement (dx, dy) in meters, its length, and cap flag.

    Shadow extends OPPOSITE the solar azimuth (azimuth measured clockwise
    from north): the displacement in the local east-north frame is
    (-L*sin(az), -L*cos(az)).
    """
    length = height_m / math.tan(math.radians(solar.elevation_deg))
    capped = False
    if not math.isfinite(length) or length > config.max_shadow_length_m:
        length = config.max_shadow_length_m
        capped = True
    az_rad = math.radians(solar.azimuth_deg)
    return -length * math.sin(az_rad), -length * math.cos(az_rad), length, capped


def build_shadows(
    footprints: Sequence[BuildingFootprint],
    solar: SolarPosition,
    frame: LocalFrame,
    config: ShadowModelConfig = ShadowModelConfig(),
) -> ShadowBuildReport:
    """Ground shadows for all usable buildings at one solar position.

    Buildings with UNAVAILABLE height provenance and buildings below the
    minimum solar elevation are excluded (and counted) rather than guessed.
    """
    shadows: list[ShadowPolygon] = []
    excluded_unknown = 0
    excluded_low_sun = 0
    skipped_invalid = 0
    capped_count = 0

    if not solar.above_horizon:
        return ShadowBuildReport(shadows=(), excluded_unknown_height=excluded_unknown,
                                 excluded_low_sun=excluded_low_sun,
                                 skipped_invalid_geometry=skipped_invalid, capped_count=0)

    for fp in footprints:
        if fp.provenance == HeightProvenance.UNAVAILABLE or fp.height_m is None:
            excluded_unknown += 1
            continue
        if solar.elevation_deg < config.min_solar_elevation_deg:
            excluded_low_sun += 1
            continue
        try:
            local = footprint_to_local(fp, frame)
            if local.geom_type != "Polygon":
                raise ValueError("footprint not a polygon after repair")
            local = orient(local, 1.0)
        except (ValueError, AttributeError):
            skipped_invalid += 1
            continue
        dx, dy, length, capped = _shadow_displacement(solar, fp.height_m, config)
        if capped:
            capped_count += 1
        shadows.append(ShadowPolygon(
            building_source_id=fp.source_id,
            height_m=fp.height_m,
            provenance=fp.provenance,
            geometry=_swept_shadow(local, dx, dy),
            shadow_length_m=length,
            capped=capped,
        ))

    return ShadowBuildReport(shadows=tuple(shadows), excluded_unknown_height=excluded_unknown,
                             excluded_low_sun=excluded_low_sun,
                             skipped_invalid_geometry=skipped_invalid, capped_count=capped_count)
