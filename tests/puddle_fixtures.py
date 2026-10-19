"""Synthetic C7.2 fixtures for unit and API tests.

These are SYNTHETIC and prove processing logic only — never real-world
accuracy. Tests that exercise real coverage use the pinned pilot dataset
explicitly (``backend/data/puddle_ways_spb_pilot.json``).
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from app.services.puddle_engine.engine import PuddleLegInput
from app.services.puddle_engine.ways_store import PuddleWay
from app.services.puddle_engine.weather import WeatherHour, WeatherSeries

#: The real pilot box (taken from the pinned dataset manifest) so that
#: "inside / outside pilot" decisions in tests match production semantics.
PILOT_BBOX = (59.9365, 30.307, 59.9445, 30.326)
ORIGIN_LAT, ORIGIN_LON = 59.9405, 30.3165  # pilot centroid


def latlon(x_m: float, y_m: float) -> tuple[float, float]:
    """Local planar metres → (lat, lon) near the pilot centroid."""
    lat = ORIGIN_LAT + y_m / 111_320.0
    lon = ORIGIN_LON + x_m / (111_320.0 * math.cos(math.radians(ORIGIN_LAT)))
    return lat, lon


def way(osm_id: int, y_m: float = 0.0, *, x_from: float = -120.0,
        x_to: float = 120.0, points: int = 3, **tags: str) -> PuddleWay:
    """A straight east-west walkable way ``y_m`` metres north of the origin."""
    tags.setdefault("highway", "footway")
    xs = [x_from + (x_to - x_from) * index / (points - 1)
          for index in range(points)]
    geometry = tuple(latlon(x, y_m) for x in xs)
    lats = [point[0] for point in geometry]
    lons = [point[1] for point in geometry]
    return PuddleWay(
        osm_id=osm_id, osm_version=1, osm_timestamp="2026-10-01T00:00:00Z",
        tags={str(key): str(value) for key, value in tags.items()},
        geometry=geometry,
        bbox=(min(lats), min(lons), max(lats), max(lons)))


class SyntheticWaysProvider:
    """Declared-coverage provider over explicitly listed synthetic ways."""

    def __init__(self, ways: tuple[PuddleWay, ...] = (),
                 pilot_bbox: tuple[float, float, float, float] = PILOT_BBOX):
        self._ways = tuple(ways)
        self._bbox = pilot_bbox

    def available(self) -> bool:
        return True

    def provenance(self) -> dict:
        return {"available": True,
                "source": "SYNTHETIC TEST FIXTURE — NOT REAL SPb DATA"}

    def dataset_version(self) -> str | None:
        return "test_fixture"

    def pilot_bbox(self) -> tuple[float, float, float, float]:
        return self._bbox

    def ways_in_bbox(self, bbox):
        lat_min, lon_min, lat_max, lon_max = bbox
        return [way_ for way_ in self._ways
                if way_.bbox[0] <= lat_max and way_.bbox[2] >= lat_min
                and way_.bbox[1] <= lon_max and way_.bbox[3] >= lon_min]


def series(t0: datetime, *, rain=None, temperature: float = 12.0,
           snowfall: float = 0.0, hours_before: int = 48, hours_after: int = 12,
           retrieved_at: datetime | None = None) -> WeatherSeries:
    """Hourly bins ending at ``t0 - age`` hours for age in 48 … −11.

    ``rain(age_hours) -> mm`` describes the bin that ENDS ``age_hours`` before
    the walk start, matching the provider's ``(t-1h, t]`` semantics.
    """
    rain = rain or (lambda age: 0.0)
    hours = tuple(
        WeatherHour(ends_at=t0 - timedelta(hours=age),
                    precipitation_mm=float(rain(age)),
                    temperature_c=temperature, snowfall_cm=snowfall)
        for age in range(hours_before, -hours_after, -1))
    retrieved = retrieved_at or t0
    return WeatherSeries(hours=hours, retrieved_at=retrieved,
                         valid_until=retrieved + timedelta(minutes=15))


def leg(geometry: tuple[tuple[float, float], ...], seconds: float = 300.0,
        visit: float = 0.0) -> PuddleLegInput:
    return PuddleLegInput(geometry=geometry, walking_duration_seconds=seconds,
                          visit_duration_seconds_after_leg=visit)
