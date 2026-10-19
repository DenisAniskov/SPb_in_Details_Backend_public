"""Composition root for the C11 quiet engine.

One hash-verified snapshot per process. There is no runtime OSM download, no
synthetic fallback and no partially trusted dataset: disabled, missing or
mismatched data yields an honest ``unavailable`` response.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from app.core.config import settings
from app.services.quiet_engine.source import QuietDataset

BACKEND_ROOT = Path(__file__).resolve().parents[3]


def resolve_dataset_path(configured: str | None = None) -> Path | None:
    value = (configured if configured is not None else settings.QUIET_DATASET) or ""
    value = value.strip()
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else BACKEND_ROOT / path


def resolve_manifest_path(configured: str | None = None) -> Path | None:
    value = (configured if configured is not None
             else settings.QUIET_DATASET_MANIFEST) or ""
    value = value.strip()
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else BACKEND_ROOT / path


@lru_cache(maxsize=4)
def _cached_dataset(dataset_path: str, manifest_path: str) -> QuietDataset:
    return QuietDataset.load(dataset_path, manifest_path)


def default_quiet_dataset() -> QuietDataset:
    if not settings.QUIET_ENGINE_ENABLED:
        return QuietDataset.unavailable("quiet_engine_disabled")
    dataset = resolve_dataset_path()
    manifest = resolve_manifest_path()
    if dataset is None or manifest is None:
        return QuietDataset.unavailable("quiet_dataset_not_configured")
    return _cached_dataset(str(dataset), str(manifest))


def clear_provider_cache() -> None:
    """Test hook after changing QUIET_* settings."""
    _cached_dataset.cache_clear()
