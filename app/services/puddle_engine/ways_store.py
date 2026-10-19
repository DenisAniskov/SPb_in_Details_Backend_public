"""Read-only pilot OSM ways for C7.2 — a pinned snapshot, never synthesised.

The dataset is built by ``scripts/build_phase_c7_2_ways.py`` from a
hash-pinned OSM extract (ODbL-1.0, see PHASE_C7_2_DATA_PROVENANCE.md). This
store NEVER falls back to synthetic geometry and NEVER fetches anything at
runtime: if the file is missing or does not validate, the provider reports
``available() == False`` and the API answers with an honest unavailable
status instead of guessing someone's surface.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

#: Only these way types can ever confirm surface facts for a pedestrian walk
#: (the same walkable set the coverage audit reports on). A route line lying
#: on a carriageway or a service road stays unconfirmed on purpose.
WALKABLE_HIGHWAY = frozenset(
    {"footway", "pedestrian", "path", "steps", "corridor", "living_street"})

#: Ways the walk's own access rules exclude for pedestrians: their surface
#: tags may describe a different (private/car) surface, so they never confirm.
_EXCLUDED_FOOT = {"no"}
_EXCLUDED_ACCESS = {"no", "private", "customers", "permit"}

BBox = tuple[float, float, float, float]  # (lat_min, lon_min, lat_max, lon_max)


@dataclass(frozen=True)
class PuddleWay:
    """One walkable OSM way: identity, tags, original (lat, lon) geometry."""

    osm_id: int
    osm_version: int
    osm_timestamp: str
    tags: dict[str, str]
    geometry: tuple[tuple[float, float], ...]  # (lat, lon), never simplified
    bbox: BBox

    def matchable(self) -> bool:
        if self.tags.get("highway") not in WALKABLE_HIGHWAY:
            return False
        if self.tags.get("foot") in _EXCLUDED_FOOT:
            return False
        if self.tags.get("access") in _EXCLUDED_ACCESS:
            return False
        return True


class EmptyPuddleWaysProvider:
    """No verified data source: the analysis reports unavailability honestly."""

    def available(self) -> bool:
        return False

    def provenance(self) -> dict:
        return {
            "available": False,
            "reason": "Пилотный датасет OSM не найден или не прошёл проверку.",
        }

    def dataset_version(self) -> str | None:
        return None

    def pilot_bbox(self) -> BBox | None:
        return None

    def ways_in_bbox(self, bbox: BBox) -> list[PuddleWay]:
        return []


class PuddleWaysProvider(Protocol):
    def available(self) -> bool: ...

    def provenance(self) -> dict: ...

    def dataset_version(self) -> str | None: ...

    def pilot_bbox(self) -> BBox | None: ...

    def ways_in_bbox(self, bbox: BBox) -> list[PuddleWay]: ...


class JsonPuddleWaysProvider:
    """Strict loader for the pinned pilot dataset (``puddle_ways_spb_pilot.json``).

    Validation happens once at construction: a file that is truncated, has an
    unsupported schema version, duplicated ids, degenerate geometry or
    non-finite coordinates makes the whole provider unavailable — a partially
    trusted dataset would silently drop or invent surface facts.
    """

    SCHEMA_VERSION = 1

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._available = False
        self._error: str | None = None
        self._ways: tuple[PuddleWay, ...] = ()
        self._bbox: BBox | None = None
        self._provenance: dict = {"available": False}
        self._dataset_version: str | None = None
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_bytes())
            if raw.get("schema_version") != self.SCHEMA_VERSION:
                raise ValueError("unsupported schema version")
            version = raw["dataset_version"]
            if not isinstance(version, str) or not version:
                raise ValueError("missing dataset version")
            bbox = raw["pilot_bbox"]
            if (not isinstance(bbox, list) or len(bbox) != 4
                    or any(not isinstance(v, (int, float)) or not math.isfinite(v)
                           for v in bbox)):
                raise ValueError("invalid pilot bbox")
            west, south, east, north = (float(v) for v in bbox)
            self._bbox = (south, west, north, east)
            ways: list[PuddleWay] = []
            seen: set[int] = set()
            for row in raw["ways"]:
                osm_id = row["osm_id"]
                if not isinstance(osm_id, int) or osm_id in seen:
                    raise ValueError("duplicate or invalid way id")
                seen.add(osm_id)
                points = tuple((float(lat), float(lon)) for lon, lat in row["geometry"])
                if len(points) < 2 or any(
                        not (-90 <= lat <= 90 and -180 <= lon <= 180)
                        for lat, lon in points):
                    raise ValueError(f"invalid geometry on way {osm_id}")
                lat_min = min(p[0] for p in points)
                lon_min = min(p[1] for p in points)
                lat_max = max(p[0] for p in points)
                lon_max = max(p[1] for p in points)
                ways.append(PuddleWay(
                    osm_id=osm_id,
                    osm_version=int(row["osm_version"]),
                    osm_timestamp=str(row["osm_timestamp"]),
                    tags={str(k): str(v) for k, v in row["tags"].items()},
                    geometry=points,
                    bbox=(lat_min, lon_min, lat_max, lon_max)))
            if not ways:
                raise ValueError("empty dataset")
            self._ways = tuple(ways)
            source = raw.get("source", {})
            self._provenance = {
                "available": True,
                "source": "OpenStreetMap",
                "source_format": source.get("source_format"),
                "endpoint": source.get("endpoint"),
                "retrieved_at_utc": raw.get("retrieved_at_utc"),
                "osm_base_timestamp": raw.get("osm_base_timestamp"),
                "raw_sha256": source.get("raw_sha256"),
                "license": raw.get("license"),
                "license_url": raw.get("license_url"),
                "attribution": raw.get("attribution"),
                "dataset_version": version,
                "schema_version": self.SCHEMA_VERSION,
                "pilot_bbox": [west, south, east, north],
                "way_count": len(self._ways),
            }
            self._dataset_version = version
            self._available = True
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self._error = type(exc).__name__
            self._available = False

    def available(self) -> bool:
        return self._available

    def provenance(self) -> dict:
        if self._available:
            return dict(self._provenance)
        return {
            "available": False,
            "reason": "Пилотный датасет OSM не найден или не прошёл проверку.",
            "error_type": self._error,
        }

    def dataset_version(self) -> str | None:
        return self._dataset_version

    def pilot_bbox(self) -> BBox | None:
        return self._bbox

    def ways_in_bbox(self, bbox: BBox) -> list[PuddleWay]:
        """Ways whose own bounding box intersects the query box (cheap filter)."""
        if not self._available:
            return []
        lat_min, lon_min, lat_max, lon_max = bbox
        return [way for way in self._ways
                if way.bbox[0] <= lat_max and way.bbox[2] >= lat_min
                and way.bbox[1] <= lon_max and way.bbox[3] >= lon_min]
