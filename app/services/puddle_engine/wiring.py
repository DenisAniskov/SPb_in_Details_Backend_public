"""Composition root for the C7.2 puddle engine.

Same contract as the shade wiring: every caller (API dependency, scripts,
tests) resolves the same production providers.

* a configured, present, valid pilot dataset → [JsonPuddleWaysProvider];
* anything else → [EmptyPuddleWaysProvider], which honestly reports that no
  verified data source is available. Synthetic fixtures are NEVER wired here.

The weather provider follows the ТЗ requirement of a separate, explicit
opt-in: the Open-Meteo free tier is enabled only when the operator sets
``PUDDLE_WEATHER_ENABLED`` (non-commercial use conditions, see
PHASE_C7_2_DATA_PROVENANCE.md). Until then the API returns an honest
"weather_not_configured" unavailability instead of archived test values.

Both providers are singletons per process (an ``lru_cache`` keyed on the
relevant settings): the weather provider's bounded cache and single-flight
lock must outlive a single request to be a real upstream-request budget.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from app.core.config import settings
from app.services.puddle_engine.ways_store import (
    EmptyPuddleWaysProvider,
    JsonPuddleWaysProvider,
    PuddleWaysProvider,
)
from app.services.puddle_engine.weather import (
    DisabledWeatherProvider,
    OpenMeteoWeatherProvider,
    WeatherProvider,
)

#: backend/ — the anchor for the default relative dataset path, independent of
#: the process working directory (dev runs from backend/, Docker from /app).
BACKEND_ROOT = Path(__file__).resolve().parents[3]


def resolve_ways_dataset_path(configured: str | None = None) -> Path | None:
    value = (configured if configured is not None
             else settings.PUDDLE_WAYS_DATASET) or ""
    value = value.strip()
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else BACKEND_ROOT / path


@lru_cache(maxsize=4)
def _cached_provider(dataset_path: str) -> PuddleWaysProvider | None:
    provider = JsonPuddleWaysProvider(dataset_path)
    return provider if provider.available() else None


def default_puddle_ways_provider() -> PuddleWaysProvider:
    """The production ways provider (never synthetic)."""
    path = resolve_ways_dataset_path()
    if path is None:
        return EmptyPuddleWaysProvider()
    provider = _cached_provider(str(path))
    return provider if provider is not None else EmptyPuddleWaysProvider()


def _build_weather_provider(latitude: float, longitude: float) -> WeatherProvider:
    """One construction site for the Open-Meteo provider.

    Separate from the caching below so tests can substitute the transport;
    the cache — not this call — is what guarantees the fetch budget.
    """
    return OpenMeteoWeatherProvider(latitude=latitude, longitude=longitude)


@lru_cache(maxsize=4)
def _cached_weather_provider(enabled: bool, latitude: float,
                             longitude: float) -> WeatherProvider:
    if not enabled:
        return DisabledWeatherProvider()
    return _build_weather_provider(latitude, longitude)


def default_puddle_weather_provider() -> WeatherProvider:
    """ONE long-lived provider per process and configuration.

    The endpoint resolves this dependency on EVERY request, so a provider
    built per call would carry its 15-minute cache and its single-flight lock
    for the lifetime of one request only — the documented "at most one
    upstream fetch per validity window, also across different users" would
    simply not hold. The instance is therefore cached here.
    """
    return _cached_weather_provider(
        settings.PUDDLE_WEATHER_ENABLED,
        settings.PUDDLE_WEATHER_LATITUDE,
        settings.PUDDLE_WEATHER_LONGITUDE)


def clear_provider_cache() -> None:
    """Test hook: forget cached providers after changing the settings."""
    _cached_provider.cache_clear()
    _cached_weather_provider.cache_clear()
