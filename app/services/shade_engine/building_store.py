"""SQLite-backed building geometry provider for the C7.1 pilot dataset.

The provider is READ-ONLY by contract: it serves footprints that were built
offline by ``scripts.build_phase_c7_1_buildings`` from cached OSM evidence and
never changes them, never guesses, and never falls back to synthetic data.

Honesty boundaries enforced here (C7.1 §7):

* Only rows whose ``geometry_validation_status`` is ``ok``/``repaired`` are
  returned; invalid geometry stays in the dataset for auditing but casts no
  shadow.
* An oversized query bbox or a result set beyond the documented cap raises
  [BuildingStoreLimitError] instead of silently truncating — a truncated
  building set would silently bias the shade estimate.
* Coverage is answerable per point/bbox: the dataset knows exactly which
  pilot zones it covers, so the analysis can say ``outside_coverage``
  honestly instead of reporting 100 % sun for unmapped ground.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3

from shapely import wkb

from app.services.shade_engine.buildings import BBox, BuildingFootprint, HeightProvenance

#: §7 safety boundaries (documented, tested).
MAX_BBOX_AREA_DEG2 = 0.05      # ≈ 30 × 30 km at SPb latitude: far beyond any walk
MAX_FOOTPRINTS_PER_QUERY = 60_000

_USABLE_STATUSES = ("ok", "repaired")


class BuildingStoreError(RuntimeError):
    """Raised when the dataset file exists but cannot be used honestly."""


class BuildingStoreLimitError(BuildingStoreError):
    """Raised when a query exceeds the documented §7 safety boundaries."""


@dataclass(frozen=True)
class DatasetCoverage:
    """What the dataset can and cannot answer for one query area."""

    zone_count: int
    buildings_total: int
    buildings_with_usable_height: int
    buildings_without_height: int
    covered: bool          # the query bbox lies inside the mapped zones
    zone_ids: tuple[str, ...]


class SqliteBuildingGeometryProvider:
    """Read-only OSM-derived pilot dataset provider (C7.1 level B)."""

    name = "osm_spb_pilot_sqlite"

    def __init__(self, dataset_path: str | Path) -> None:
        self._path = Path(dataset_path)

    # ------------------------------------------------------------------ meta
    @property
    def path(self) -> Path:
        return self._path

    def _connect(self) -> sqlite3.Connection:
        if not self._path.exists():
            raise BuildingStoreError(f"dataset not found: {self._path}")
        return sqlite3.connect(f"file:{self._path.as_posix()}?mode=ro", uri=True)

    def _rows(self, sql: str, params: tuple = ()) -> list[tuple]:
        """Run one read-only query, wrapping storage errors in our own type."""
        try:
            with self._connect() as con:
                return con.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            raise BuildingStoreError(
                f"dataset {self._path} is not a usable shade building store "
                f"({type(exc).__name__})") from exc

    def available(self) -> bool:
        """True when the dataset exists, opens, and holds usable buildings."""
        if not self._path.exists():
            return False
        try:
            rows = self._rows("SELECT COUNT(*) FROM buildings")
            return bool(rows and rows[0][0] > 0)
        except BuildingStoreError:
            return False

    def dataset_meta(self) -> dict:
        rows = self._rows("SELECT key, value FROM dataset_meta")
        return {key: json.loads(value) for key, value in rows}

    def provenance_description(self) -> str:
        if not self._path.exists():
            return ("Датасет геометрии зданий не найден; реальная оценка тени "
                    "недоступна (данные не подменяются оценками).")
        meta = self.dataset_meta()
        return (
            f"OpenStreetMap (ODbL 1.0, © OpenStreetMap contributors), снимок "
            f"{meta.get('snapshot_date')}, датасет {meta.get('dataset_version')}, "
            f"пилотные зоны: {', '.join(meta.get('zones', []))}. "
            "Высоты: OSM height=* и оценка по building:levels × 3.3 м; "
            "здания без высоты тень не отбрасывают и учитываются отдельно."
        )

    # ---------------------------------------------------------------- queries
    def footprints_in_bbox(self, bbox: BBox) -> tuple[BuildingFootprint, ...]:
        """Usable footprints intersecting ``bbox`` (min_lat, min_lon, max_lat, max_lon)."""
        min_lat, min_lon, max_lat, max_lon = bbox
        area = (max_lat - min_lat) * (max_lon - min_lon)
        if area <= 0:
            return ()
        if area > MAX_BBOX_AREA_DEG2:
            raise BuildingStoreLimitError(
                f"bbox area {area:.4f} deg² exceeds the documented limit "
                f"{MAX_BBOX_AREA_DEG2} deg² (§7 protection against pathological queries)")
        rows = self._rows(
            "SELECT b.osm_type, b.osm_id, b.geometry_wkb, b.height_m, b.height_provenance,"
            " b.building_levels, b.floor_height_assumption, b.geometry_source"
            " FROM buildings_rtree r JOIN buildings b ON b.id = r.id"
            " WHERE r.max_lat >= ? AND r.min_lat <= ? AND r.max_lon >= ? AND r.min_lon <= ?"
            " AND b.geometry_validation_status IN (?, ?)",
            (min_lat, max_lat, min_lon, max_lon, *_USABLE_STATUSES))
        if len(rows) > MAX_FOOTPRINTS_PER_QUERY:
            raise BuildingStoreLimitError(
                f"{len(rows)} footprints exceed the documented per-query cap "
                f"{MAX_FOOTPRINTS_PER_QUERY}; refusing to silently truncate the building set")
        return tuple(self._to_footprint(row) for row in rows)

    def _to_footprint(self, row) -> BuildingFootprint:
        osm_type, osm_id, blob, height_m, provenance, levels, floor_assumption, source = row
        polygon = wkb.loads(blob)
        shells = [polygon] if polygon.geom_type == "Polygon" else list(polygon.geoms)
        # A repaired multipolygon is stored as one polygon per shell; the first
        # shell is returned as the footprint (the store keeps one row per
        # shell, so this is a single-shell polygon in practice).
        shell = list(shells[0].exterior.coords)
        holes = [list(ring.coords) for ring in shells[0].interiors]
        return BuildingFootprint(
            polygon_latlon=tuple((y, x) for x, y in shell),
            holes_latlon=tuple(tuple((y, x) for x, y in ring) for ring in holes),
            height_m=height_m,
            provenance=HeightProvenance(provenance),
            levels=levels,
            assumed_floor_height_m=floor_assumption,
            source=source,
            source_id=f"{osm_type}/{osm_id}",
        )

    def coverage_for_bbox(self, bbox: BBox) -> DatasetCoverage:
        """How well the dataset covers a query area (for honest 'unknown')."""
        min_lat, min_lon, max_lat, max_lon = bbox
        zones = self._rows("SELECT zone_id, min_lat, min_lon, max_lat, max_lon FROM zones")
        counts = self._rows(
            "SELECT COUNT(*),"
            " SUM(CASE WHEN height_provenance != 'unavailable' THEN 1 ELSE 0 END)"
            " FROM buildings WHERE geometry_validation_status IN (?, ?)",
            _USABLE_STATUSES)[0]
        zone_ids = []
        for zone_id, z_min_lat, z_min_lon, z_max_lat, z_max_lon in zones:
            if (min_lat >= z_min_lat and min_lon >= z_min_lon
                    and max_lat <= z_max_lat and max_lon <= z_max_lon):
                zone_ids.append(zone_id)
        total, with_height = counts
        return DatasetCoverage(
            zone_count=len(zone_ids),
            buildings_total=total or 0,
            buildings_with_usable_height=with_height or 0,
            buildings_without_height=(total or 0) - (with_height or 0),
            covered=bool(zone_ids),
            zone_ids=tuple(sorted(zone_ids)),
        )

    def zone_boxes(self) -> tuple[tuple[str, float, float, float, float], ...]:
        """(zone_id, min_lat, min_lon, max_lat, max_lon) for every mapped zone."""
        rows = self._rows("SELECT zone_id, min_lat, min_lon, max_lat, max_lon FROM zones")
        return tuple((r[0], r[1], r[2], r[3], r[4]) for r in rows)

    def max_usable_height_m(self) -> float:
        """Tallest building with a usable height in the dataset (for query padding).

        The analysis pads its building query by ``H_max / tan(elevation)`` so
        that a tall building far from the route is still fetched when its
        shadow can reach the walk.
        """
        rows = self._rows(
            "SELECT MAX(height_m) FROM buildings WHERE height_provenance != 'unavailable'"
            " AND geometry_validation_status IN (?, ?)", _USABLE_STATUSES)
        return float(rows[0][0]) if rows and rows[0][0] is not None else 0.0

    def coverage_for_point(self, lat: float, lon: float) -> str | None:
        """The zone id containing the point, or None outside every mapped zone."""
        rows = self._rows(
            "SELECT zone_id FROM zones WHERE min_lat <= ? AND max_lat >= ?"
            " AND min_lon <= ? AND max_lon >= ? LIMIT 1",
            (lat, lat, lon, lon))
        return rows[0][0] if rows else None

    def coverage_for_route(self, route_latlon) -> tuple[str, tuple[str, ...]]:
        """('full' | 'partial' | 'outside', zone ids touched) for a route polyline.

        Points are tested against the mapped zone boxes; this drives the
        three-state classification §9: ground outside the pilot coverage is
        ``unknown``, never counted as sun.
        """
        points = [(lat, lon) for lat, lon in route_latlon]
        if not points:
            return "outside", ()
        zone_rows = self.zone_boxes()
        touched: set[str] = set()
        inside = 0
        for lat, lon in points:
            hit = None
            for zone_id, min_lat, min_lon, max_lat, max_lon in zone_rows:
                if min_lat <= lat <= max_lat and min_lon <= lon <= max_lon:
                    hit = zone_id
                    break
            if hit:
                inside += 1
                touched.add(hit)
        if inside == len(points):
            return "full", tuple(sorted(touched))
        if inside:
            return "partial", tuple(sorted(touched))
        return "outside", ()
