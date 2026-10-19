"""Shade Engine v1 — foundation for sun/shade analysis of an EXISTING route.

This package is a pure calculation layer. It does NOT choose or optimize
routes and must never be presented as shade-aware routing.

Layer map:
    geo           — geodesic distances and a documented local metric frame
    solar         — solar elevation/azimuth (NOAA / Meeus algorithm)
    buildings     — building footprint + height provenance + provider contract
    shadows       — 2.5D parallel-ray shadow polygons
    intersection  — route/shadow intersection, length-weighted shade fraction
    weather       — cloud-cover relevance context (never changes geometry)
    engine        — orchestration of the layers into one analysis result

Honesty contract of v1 (see docs/SHADE_ENGINE_V1.md):
    * results are ESTIMATES driven by whatever building geometry the
      configured provider supplies — never "exact real-world shade";
    * buildings without usable height are excluded, not guessed;
    * synthetic fixtures exist ONLY for tests and are labeled as such.
"""

from app.services.shade_engine.engine import ShadeEngine, ShadeAnalysisResult
from app.services.shade_engine.buildings import (
    BuildingFootprint,
    BuildingGeometryProvider,
    EmptyBuildingProvider,
    HeightProvenance,
)
from app.services.shade_engine.solar import SolarPosition, solar_position
from app.services.shade_engine.weather import ShadeRelevance, WeatherContext

__all__ = [
    "ShadeEngine",
    "ShadeAnalysisResult",
    "BuildingFootprint",
    "BuildingGeometryProvider",
    "EmptyBuildingProvider",
    "HeightProvenance",
    "SolarPosition",
    "solar_position",
    "WeatherContext",
    "ShadeRelevance",
]
