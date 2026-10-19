"""Read-only, hash-pinned quiet-road snapshot for Phase C11.

The production object is built from ``data/quiet_spb_pilot.json.gz`` and the
SHA-256 manifest next to it. It is loaded once per process and never fetches
anything. A missing, corrupted or mismatched snapshot makes the WHOLE dataset
unavailable: a partially trusted road layer could silently turn unknown ground
into a quiet claim.
"""
from __future__ import annotations

from dataclasses import dataclass
import gzip
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from shapely.geometry import LineString, Point
from shapely.strtree import STRtree

from app.services.quiet_engine.model import (
    BUILDING_PASSAGE_FACTOR,
    EXCLUDED_ACCESS,
    ROAD_CLASS_WEIGHTS,
    TUNNEL_FACTOR,
)

SCHEMA_VERSION = 1
EARTH_RADIUS_METERS = 6_371_000.0


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def road_weight(tags: dict[str, str]) -> float | None:
    """Functional-class weight, or None when the way is not a motor source."""
    highway = tags.get("highway", "")
    weight = ROAD_CLASS_WEIGHTS.get(highway)
    if weight is None:
        return None
    access = tags.get("access", "").lower()
    motor_vehicle = tags.get("motor_vehicle", "").lower()
    motorcar = tags.get("motorcar", "").lower()
    vehicle = tags.get("vehicle", "").lower()
    if access in EXCLUDED_ACCESS and motor_vehicle != "yes":
        return None
    if motor_vehicle in EXCLUDED_ACCESS or motorcar in EXCLUDED_ACCESS:
        return None
    if vehicle in EXCLUDED_ACCESS:
        return None
    if tags.get("tunnel") == "yes":
        weight *= TUNNEL_FACTOR
    elif tags.get("tunnel") == "building_passage":
        weight *= BUILDING_PASSAGE_FACTOR
    return weight


@dataclass(frozen=True)
class QuietRoad:
    osm_id: int
    highway: str
    weight: float
    geometry_lonlat: tuple[tuple[float, float], ...]
    projected: LineString


@dataclass(frozen=True)
class QuietDataset:
    available: bool
    reason: str | None
    dataset_version: str | None
    dataset_sha256: str | None
    bbox: tuple[float, float, float, float] | None  # west, south, east, north
    roads: tuple[QuietRoad, ...]
    tree: STRtree | None
    origin_lon: float
    origin_lat: float
    provenance: dict[str, Any]
    excluded_way_count: int = 0

    def contains(self, lon: float, lat: float) -> bool:
        if self.bbox is None:
            return False
        west, south, east, north = self.bbox
        return west <= lon <= east and south <= lat <= north

    def project(self, lon: float, lat: float) -> tuple[float, float]:
        """Local equirectangular metres around the pilot centre.

        The pilot is ~8 x 5.5 km; scale error is far below the model's
        documented precision and geometry is never re-shaped by the projection.
        """
        x = math.radians(lon - self.origin_lon) * EARTH_RADIUS_METERS * math.cos(
            math.radians(self.origin_lat))
        y = math.radians(lat - self.origin_lat) * EARTH_RADIUS_METERS
        return x, y

    def nearest(self, lon: float, lat: float) -> tuple[QuietRoad, float] | None:
        if not self.available or self.tree is None or not self.roads:
            return None
        point = Point(*self.project(lon, lat))
        index = self.tree.nearest(point)
        if index is None:
            return None
        road = self.roads[int(index)]
        return road, point.distance(road.projected)

    def nearby(self, lon: float, lat: float,
               radius: float) -> list[tuple[QuietRoad, float]]:
        """All roads whose real geometry is within [radius] metres.

        The STRtree query uses bounding boxes, so each candidate is re-checked
        by true planar distance. This is what lets a parallel primary corridor
        be reported even when a residential way is physically closer.
        """
        if not self.available or self.tree is None or not self.roads:
            return []
        point = Point(*self.project(lon, lat))
        candidates = self.tree.query(point.buffer(radius))
        result: list[tuple[QuietRoad, float]] = []
        for index in candidates:
            road = self.roads[int(index)]
            distance = point.distance(road.projected)
            if distance <= radius:
                result.append((road, distance))
        return result

    @classmethod
    def load(cls, dataset_path: str | Path, manifest_path: str | Path) -> "QuietDataset":
        try:
            manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
            compressed = Path(dataset_path).read_bytes()
            if sha256(compressed) != manifest["gzip_sha256"]:
                raise ValueError("gzip checksum mismatch")
            data = gzip.decompress(compressed)
            if len(data) != manifest["dataset_bytes"]:
                raise ValueError("dataset size mismatch")
            if sha256(data) != manifest["dataset_sha256"]:
                raise ValueError("dataset checksum mismatch")
            dataset = json.loads(data)
            return cls._from_dataset(dataset, manifest["dataset_sha256"])
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return cls.unavailable("quiet_dataset_unavailable")

    @classmethod
    def from_dataset(cls, dataset: dict[str, Any], dataset_sha256: str = "test") -> "QuietDataset":
        """Test/bench hook: validates the same schema without a file."""
        return cls._from_dataset(dataset, dataset_sha256)

    @classmethod
    def unavailable(cls, reason: str) -> "QuietDataset":
        return cls(available=False, reason=reason, dataset_version=None,
                   dataset_sha256=None, bbox=None, roads=(), tree=None,
                   origin_lon=0.0, origin_lat=0.0,
                   provenance={"available": False, "reason": reason})

    @classmethod
    def _from_dataset(cls, dataset: dict[str, Any], dataset_sha256: str) -> "QuietDataset":
        if dataset.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported schema version")
        version = dataset["dataset_version"]
        if not isinstance(version, str) or not version:
            raise ValueError("missing dataset version")
        bbox = dataset["pilot_bbox"]
        if (not isinstance(bbox, list) or len(bbox) != 4
                or any(not isinstance(v, (int, float)) or not math.isfinite(v)
                       for v in bbox)):
            raise ValueError("invalid pilot bbox")
        west, south, east, north = (float(v) for v in bbox)
        if not (west < east and south < north):
            raise ValueError("empty pilot bbox")
        origin_lon = (west + east) / 2.0
        origin_lat = (south + north) / 2.0
        cos_lat = math.cos(math.radians(origin_lat))

        def project(lon: float, lat: float) -> tuple[float, float]:
            x = math.radians(lon - origin_lon) * EARTH_RADIUS_METERS * cos_lat
            y = math.radians(lat - origin_lat) * EARTH_RADIUS_METERS
            return x, y

        roads: list[QuietRoad] = []
        seen: set[int] = set()
        excluded = 0
        for row in dataset["ways"]:
            osm_id = row["osm_id"]
            if not isinstance(osm_id, int) or osm_id in seen:
                raise ValueError("duplicate or invalid way id")
            seen.add(osm_id)
            tags = {str(k): str(v) for k, v in row["tags"].items()}
            weight = road_weight(tags)
            if weight is None:
                excluded += 1
                continue
            points = tuple((float(lon), float(lat)) for lon, lat in row["geometry"])
            if len(points) < 2 or any(
                    not (-180 <= lon <= 180 and -90 <= lat <= 90)
                    or not (math.isfinite(lon) and math.isfinite(lat))
                    for lon, lat in points):
                raise ValueError(f"invalid geometry on way {osm_id}")
            projected = LineString([project(lon, lat) for lon, lat in points])
            if projected.is_empty or projected.length <= 0:
                raise ValueError(f"degenerate projected way {osm_id}")
            roads.append(QuietRoad(
                osm_id=osm_id, highway=tags.get("highway", ""),
                weight=weight, geometry_lonlat=points, projected=projected))
        if not roads:
            raise ValueError("no drivable roads")
        roads.sort(key=lambda road: road.osm_id)
        tree = STRtree([road.projected for road in roads])
        source = dataset.get("source", {})
        provenance = {
            "available": True,
            "dataset_version": version,
            "dataset_sha256": dataset_sha256,
            "source": source.get("name"),
            "provider": source.get("provider"),
            "url": source.get("url"),
            "raw_sha256": source.get("raw_sha256"),
            "osm_base_timestamp": source.get("osm_base_timestamp"),
            "license": dataset.get("license"),
            "license_url": dataset.get("license_url"),
            "attribution": dataset.get("attribution"),
            "pilot_bbox": [west, south, east, north],
            "way_count": len(roads),
            "excluded_way_count": excluded,
            "class_counts": _class_counts(roads),
        }
        return cls(available=True, reason=None, dataset_version=version,
                   dataset_sha256=dataset_sha256, bbox=(west, south, east, north),
                   roads=tuple(roads), tree=tree, origin_lon=origin_lon,
                   origin_lat=origin_lat, provenance=provenance,
                   excluded_way_count=excluded)


def _class_counts(roads: list[QuietRoad]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for road in roads:
        counts[road.highway] = counts.get(road.highway, 0) + 1
    return dict(sorted(counts.items()))
