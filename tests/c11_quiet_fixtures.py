"""Synthetic fixtures for the C11 quiet engine.

These fixtures exercise the MATHEMATICS only. Real geographical validation is
performed separately against the pinned OSM pilot in test_c11_quiet_data.py.
"""
from __future__ import annotations

from app.services.quiet_engine.source import QuietDataset

# west, south, east, north — same shape as the production pilot.
BBOX = (30.28, 59.915, 30.38, 59.965)


def way(osm_id: int, highway: str, geometry: list[list[float]], **tags: str) -> dict:
    return {
        "osm_id": osm_id,
        "osm_version": 1,
        "osm_timestamp": "2026-10-16T20:21:21Z",
        "tags": {"highway": highway, **tags},
        "geometry": geometry,
    }


def line(lon1: float, lat1: float, lon2: float, lat2: float,
         points: int = 2) -> list[list[float]]:
    if points <= 2:
        return [[lon1, lat1], [lon2, lat2]]
    result = []
    for index in range(points):
        fraction = index / (points - 1)
        result.append([lon1 + (lon2 - lon1) * fraction,
                       lat1 + (lat2 - lat1) * fraction])
    return result


def dataset(ways: list[dict]) -> dict:
    return {
        "schema_version": 1,
        "dataset_version": "test_quiet_v1",
        "pilot_bbox": list(BBOX),
        "license": "ODbL-1.0",
        "license_url": "https://opendatacommons.org/licenses/odbl/1-0/",
        "attribution": "© OpenStreetMap contributors",
        "source": {
            "name": "OpenStreetMap test fixture",
            "provider": "test",
            "url": "https://www.openstreetmap.org/",
            "raw_sha256": "0" * 64,
            "osm_base_timestamp": "2026-10-16T20:21:21Z",
        },
        "ways": ways,
    }


def quiet_dataset(ways: list[dict]) -> QuietDataset:
    return QuietDataset.from_dataset(dataset(ways))
