"""Composition root for the C7.3 pollen engine.

Same contract as the C7.2 puddle wiring: every caller (API dependency, scripts,
tests) resolves the same production provider, and the provider is a singleton
per process so its bounded cache and single-flight lock are a real upstream
request budget rather than a per-request one.

The pollen source follows the ТЗ requirement of a separate, explicit opt-in:
the Open-Meteo free tier is non-commercial and requires attribution
(CC-BY-4.0, CAMS ENSEMBLE — see PHASE_C7_3_SOURCE_FEASIBILITY.md), so it is
enabled only when the operator sets ``POLLEN_SOURCE_ENABLED``. Until then the
API answers with an honest ``pollen_not_configured`` rather than archived
values, and synthetic fixtures are NEVER wired here.
"""

from __future__ import annotations

from functools import lru_cache

from app.core.config import settings
from app.services.pollen_engine.source import (
    DisabledPollenProvider,
    OpenMeteoPollenProvider,
    PollenProvider,
)


def _build_pollen_provider(latitude: float, longitude: float) -> PollenProvider:
    """One construction site for the Open-Meteo air-quality provider.

    Separate from the caching below so tests can substitute the transport; the
    cache — not this call — is what guarantees the fetch budget.
    """
    return OpenMeteoPollenProvider(latitude=latitude, longitude=longitude)


@lru_cache(maxsize=4)
def _cached_pollen_provider(enabled: bool, latitude: float,
                            longitude: float) -> PollenProvider:
    if not enabled:
        return DisabledPollenProvider()
    return _build_pollen_provider(latitude, longitude)


def default_pollen_provider() -> PollenProvider:
    """ONE long-lived provider per process and configuration.

    The endpoint resolves this dependency on EVERY request, so a provider
    built per call would carry its one-hour cache and its single-flight lock
    for the lifetime of a single request only — the documented "at most one
    upstream fetch per validity window, also across users" would not hold.
    C7.2 had exactly this defect; the integration test here pins it.
    """
    return _cached_pollen_provider(
        settings.POLLEN_SOURCE_ENABLED,
        settings.POLLEN_SOURCE_LATITUDE,
        settings.POLLEN_SOURCE_LONGITUDE)


def clear_provider_cache() -> None:
    """Test hook: forget the cached provider after changing the settings."""
    _cached_pollen_provider.cache_clear()
