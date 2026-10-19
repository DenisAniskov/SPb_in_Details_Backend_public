"""Composition root for the Shade Engine's building geometry provider.

Production wiring in one place, so every caller (API dependency, scripts,
tests) resolves the provider identically:

* a configured, present, usable pilot dataset → [SqliteBuildingGeometryProvider];
* anything else → [EmptyBuildingProvider], which honestly reports that no
  verified data source is available. Synthetic fixtures are NEVER wired here.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from app.core.config import settings
from app.services.shade_engine.building_store import SqliteBuildingGeometryProvider
from app.services.shade_engine.buildings import (
    BuildingGeometryProvider,
    EmptyBuildingProvider,
)

#: backend/ — the anchor for the default relative dataset path, independent of
#: the process working directory (dev runs from backend/, Docker from /app).
BACKEND_ROOT = Path(__file__).resolve().parents[3]


def resolve_dataset_path(configured: str | None = None) -> Path | None:
    value = (configured if configured is not None else settings.SHADE_BUILDING_DATASET) or ""
    value = value.strip()
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else BACKEND_ROOT / path


@lru_cache(maxsize=4)
def _cached_provider(dataset_path: str) -> BuildingGeometryProvider | None:
    provider = SqliteBuildingGeometryProvider(dataset_path)
    return provider if provider.available() else None


def default_building_provider() -> BuildingGeometryProvider:
    """The production building provider (never synthetic)."""
    path = resolve_dataset_path()
    if path is None:
        return EmptyBuildingProvider()
    provider = _cached_provider(str(path))
    return provider if provider is not None else EmptyBuildingProvider()


def clear_provider_cache() -> None:
    """Test hook: forget cached providers after changing the dataset setting."""
    _cached_provider.cache_clear()
