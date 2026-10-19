"""Building geometry model and the data-provider boundary — Phase 5.

The Shade Engine NEVER invents city data. Real building footprints/heights
must arrive through a ``BuildingGeometryProvider`` implementation backed by a
verified source (planned: OpenStreetMap ``building=*`` ways with
``height``/``building:levels`` tags). v1 ships only:

    * the provider CONTRACT (Protocol);
    * ``EmptyBuildingProvider`` — the production default, which honestly
      reports that no verified data source is configured;
    * ``SyntheticBuildingProvider`` — explicitly-labeled test fixtures whose
      provenance string identifies them as synthetic. These must NEVER be
      wired into production or surfaced to users as real SPb data.

HEIGHT PROVENANCE (strict honesty rule):

    A building without a usable height must NOT silently receive an invented
    precise height. Four provenance levels are defined (C7.1 §5.2):

    EXTERNALLY_VERIFIED — height confirmed by an independent, named source
                     (official survey, monument register); the source is
                     recorded on the footprint.
    OSM_DECLARED    — the ``height=*`` value as tagged in OSM. This is a
                     DECLARED value, not a measurement: OSM itself does not
                     verify it.
    LEVELS_ESTIMATE — height derived from ``building:levels`` multiplied by an
                     explicitly documented assumed floor height. This is an
                     ESTIMATE by definition and is carried through the whole
                     pipeline (API responses expose provenance counts).
    UNAVAILABLE     — no usable height: the building cannot cast a shadow in
                     the model and is counted in the analysis output instead
                     of being guessed.
"""

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Protocol, Sequence

BBox = tuple[float, float, float, float]  # (min_lat, min_lon, max_lat, max_lon)


class HeightProvenance(str, Enum):
    """Auditable origin of a building height (C7.1 §5.2)."""

    EXTERNALLY_VERIFIED = "externally_verified_height"
    OSM_DECLARED = "osm_declared_height"
    LEVELS_ESTIMATE = "levels_estimate"
    UNAVAILABLE = "unavailable"
    # Legacy v1 spelling: `KNOWN` was the single "height present" level of the
    # v1 prototype. It maps exactly onto OSM_DECLARED (the v1 fixtures always
    # carried a declared height), so it stays as an enum alias and the ported
    # v1 tests keep their original meaning.
    KNOWN = "osm_declared_height"


#: Provenance levels that may cast a shadow in the model.
USABLE_HEIGHT_PROVENANCE = frozenset({
    HeightProvenance.EXTERNALLY_VERIFIED,
    HeightProvenance.OSM_DECLARED,
    HeightProvenance.LEVELS_ESTIMATE,
})


@dataclass(frozen=True)
class BuildingFootprint:
    """A building footprint polygon with explicitly-provenanced height.

    polygon_latlon — ≥ 3 vertices, WGS84 (lat, lon) degrees, no self-crossing
                     guarantee is enforced here (the shadow layer repairs or
                     skips invalid geometry and reports it).
    height_m       — required when provenance is any usable level
                     (EXTERNALLY_VERIFIED / OSM_DECLARED / LEVELS_ESTIMATE).
    levels         — OSM ``building:levels`` value, when that was the source.
    assumed_floor_height_m — the documented per-floor assumption, when the
                     height came from levels (kept so the estimate is auditable).
    source / source_id — provenance strings, e.g. "OpenStreetMap way/123".
    """

    polygon_latlon: tuple[tuple[float, float], ...]
    #: Interior rings (courtyards). C7.1: many SPb buildings enclose yards;
    #: the shadow model must sweep them like the exterior, otherwise yards
    #: would be reported as building mass. Optional and default empty.
    holes_latlon: tuple[tuple[tuple[float, float], ...], ...] = ()
    height_m: float | None = None
    provenance: HeightProvenance = HeightProvenance.UNAVAILABLE
    levels: float | None = None
    assumed_floor_height_m: float | None = None
    source: str | None = None
    source_id: str | None = None

    def __post_init__(self) -> None:
        pts = self.polygon_latlon
        if not isinstance(pts, tuple) or len(pts) < 3:
            raise ValueError("footprint needs at least 3 vertices")
        for ring in (pts, *self.holes_latlon):
            if len(ring) < 3:
                raise ValueError("every ring (shell and holes) needs at least 3 vertices")
            for lat, lon in ring:
                if not (math.isfinite(lat) and math.isfinite(lon)
                        and -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
                    raise ValueError(f"footprint vertex out of range: {(lat, lon)!r}")
        if self.provenance in USABLE_HEIGHT_PROVENANCE:
            if self.height_m is None or not math.isfinite(self.height_m) or self.height_m <= 0:
                raise ValueError(
                    f"provenance={self.provenance.value} requires a positive finite height_m; "
                    "never guess a height for buildings without one"
                )


def levels_to_height(
    levels: float,
    assumed_floor_height_m: float = 3.3,
    source: str | None = None,
    source_id: str | None = None,
) -> tuple[float, float]:
    """Explicit level→height ESTIMATE. Returns (height_m, assumed_floor_height_m).

    Default assumption 3.3 m per floor is a documented compromise between
    modern residential floors (~2.8–3.0 m slab-to-slab) and the taller
    ceilings of historic SPb buildings (~3.3–4.0 m). The assumption is always
    carried alongside the resulting height so the estimate can be audited
    and re-derived with a different assumption later.
    """
    if levels is None or not math.isfinite(levels) or levels <= 0:
        raise ValueError("levels must be a positive finite number")
    if not math.isfinite(assumed_floor_height_m) or assumed_floor_height_m <= 0:
        raise ValueError("assumed_floor_height_m must be a positive finite number")
    return levels * assumed_floor_height_m, assumed_floor_height_m


class BuildingGeometryProvider(Protocol):
    """Contract for building data sources (e.g. a future OSM-backed provider).

    Implementations must be able to say honestly whether they have data at
    all (``available``) and where it came from (``provenance_description``).
    """

    name: str

    def available(self) -> bool:
        """True when this provider has verified data it may serve."""
        ...

    def provenance_description(self) -> str:
        """Human-readable data lineage; synthetic fixtures must self-identify."""
        ...

    def footprints_in_bbox(self, bbox: BBox) -> Sequence[BuildingFootprint]:
        """All footprints intersecting the bbox. Coordinates are WGS84 degrees."""
        ...


@dataclass(frozen=True)
class EmptyBuildingProvider:
    """Production default: honestly reports that no building data source is configured."""

    name: str = "none_configured"
    _reason: str = "No verified building geometry source is configured for this deployment."

    def available(self) -> bool:
        return False

    def provenance_description(self) -> str:
        return self._reason

    def footprints_in_bbox(self, bbox: BBox) -> Sequence[BuildingFootprint]:
        return ()


@dataclass(frozen=True)
class SyntheticBuildingProvider:
    """EXPLICITLY synthetic footprints for tests only.

    Its provenance string is written so that any accidental leak into a user
   -facing surface is self-identifying as fake data. Never construct this in
    production wiring; production uses ``EmptyBuildingProvider`` until a
    verified OSM-backed provider exists.
    """

    footprints: tuple[BuildingFootprint, ...] = field(default_factory=tuple)
    name: str = "synthetic_test_fixtures"

    def available(self) -> bool:
        return True

    def provenance_description(self) -> str:
        return (
            "SYNTHETIC TEST FIXTURES — NOT REAL SAINT PETERSBURG DATA. "
            f"{len(self.footprints)} hand-made geometries for tests only."
        )

    def footprints_in_bbox(self, bbox: BBox) -> Sequence[BuildingFootprint]:
        min_lat, min_lon, max_lat, max_lon = bbox
        selected = []
        for fp in self.footprints:
            lats = [p[0] for p in fp.polygon_latlon]
            lons = [p[1] for p in fp.polygon_latlon]
            if (max(lats) >= min_lat and min(lats) <= max_lat
                    and max(lons) >= min_lon and min(lons) <= max_lon):
                selected.append(fp)
        return tuple(selected)


def merge_footprints(provider_footprints: Iterable[BuildingFootprint]) -> int:
    """Counting helper used by the engine diagnostics (kept trivial on purpose)."""
    return sum(1 for _ in provider_footprints)
