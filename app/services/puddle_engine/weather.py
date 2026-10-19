"""Server-owned modeled weather. Never labels archived forecasts observations."""
from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol

import httpx

UTC = timezone.utc
SOURCE_URL = "https://open-meteo.com/en/docs"
ENDPOINT = "https://api.open-meteo.com/v1/forecast"


class WeatherUnavailable(Exception):
    def __init__(self, reason: str = "weather_unavailable"):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class WeatherHour:
    # Open-Meteo precipitation at t describes (t-1 hour, t], not [t,t+1).
    ends_at: datetime
    precipitation_mm: float
    temperature_c: float
    snowfall_cm: float


@dataclass(frozen=True)
class WeatherSeries:
    hours: tuple[WeatherHour, ...]
    retrieved_at: datetime
    valid_until: datetime
    provider: str = "Open-Meteo"
    model: str = "icon_global"
    kind: str = "modeled_forecast_and_recent_archived_forecast"
    spatial_resolution_meters: int = 11000

    def provenance(self) -> dict:
        return {
            "available": True, "provider": self.provider, "model": self.model,
            "kind": self.kind, "observed_at": None, "model_issued_at": None,
            "retrieved_at": self.retrieved_at.isoformat(),
            "valid_until": self.valid_until.isoformat(),
            "temporal_resolution_seconds": 3600,
            "spatial_resolution_meters": self.spatial_resolution_meters,
            "interval_start": (self.hours[0].ends_at - timedelta(hours=1)).isoformat()
                if self.hours else None,
            "interval_end": self.hours[-1].ends_at.isoformat() if self.hours else None,
            "license": "CC-BY-4.0", "source_url": SOURCE_URL,
            "attribution": "Weather data by Open-Meteo.com; DWD ICON model",
            "time_semantics": "precipitation describes the preceding hour (t-1h,t]",
        }


def unavailable_provenance(reason: str) -> dict:
    return {
        "available": False, "provider": "Open-Meteo", "model": "icon_global",
        "kind": "modeled_forecast_and_recent_archived_forecast",
        "observed_at": None, "model_issued_at": None, "retrieved_at": None,
        "valid_until": None, "temporal_resolution_seconds": 3600,
        "spatial_resolution_meters": 11000, "interval_start": None,
        "interval_end": None, "license": "CC-BY-4.0", "source_url": SOURCE_URL,
        "reason": reason,
    }


class WeatherProvider(Protocol):
    async def fetch(self) -> WeatherSeries: ...


class DisabledWeatherProvider:
    async def fetch(self) -> WeatherSeries:
        raise WeatherUnavailable("weather_not_configured")


def parse_open_meteo(payload: dict, now: datetime) -> WeatherSeries:
    """Strict units/UTC/chronology validation; null hours remain gaps."""
    try:
        if payload.get("utc_offset_seconds") != 0:
            raise ValueError()
        units = payload["hourly_units"]
        if (units["time"] != "unixtime" or units["precipitation"] != "mm"
                or units["snowfall"] != "cm" or units["temperature_2m"] != "°C"):
            raise ValueError()
        hourly = payload["hourly"]
        times, rain, temp, snow = (hourly[k] for k in
                                  ("time", "precipitation", "temperature_2m", "snowfall"))
        if not 1 <= len(times) <= 200 or not len(times) == len(rain) == len(temp) == len(snow):
            raise ValueError()
        hours = []
        previous = None
        for stamp, amount, temperature, snowfall in zip(times, rain, temp, snow):
            if type(stamp) is not int or (previous is not None and stamp - previous != 3600):
                raise ValueError()
            previous = stamp
            if any(value is None for value in (amount, temperature, snowfall)):
                continue
            if any(type(value) not in (float, int) or not math.isfinite(value)
                   for value in (amount, temperature, snowfall)):
                raise ValueError()
            if not (0 <= amount <= 1000 and -90 <= temperature <= 65 and 0 <= snowfall <= 500):
                raise ValueError()
            hours.append(WeatherHour(datetime.fromtimestamp(stamp, UTC), amount,
                                     temperature, snowfall))
        return WeatherSeries(tuple(hours), now, now + timedelta(minutes=15))
    except (KeyError, TypeError, ValueError, OverflowError):
        raise WeatherUnavailable() from None


class OpenMeteoWeatherProvider:
    """One fixed pilot centroid, one bounded cache; no user's route sent upstream.

    Instantiate ONLY after the operator's explicit noncommercial free-tier opt-in.
    No paid keys or arbitrary upstream URLs accepted by this implementation.
    """
    def __init__(self, *, latitude: float, longitude: float,
                 clock: Callable[[], datetime] | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.latitude = latitude
        self.longitude = longitude
        self.clock = clock or (lambda: datetime.now(UTC))
        self.transport = transport
        self._cache: WeatherSeries | None = None
        self._lock = asyncio.Lock()

    async def fetch(self) -> WeatherSeries:
        now = self.clock().astimezone(UTC)
        if self._cache and self._cache.retrieved_at <= now < self._cache.valid_until:
            return self._cache
        async with self._lock:
            now = self.clock().astimezone(UTC)
            if self._cache and self._cache.retrieved_at <= now < self._cache.valid_until:
                return self._cache
            try:
                async with asyncio.timeout(4):
                    async with httpx.AsyncClient(timeout=3.5, transport=self.transport,
                                                 follow_redirects=False) as client:
                        async with client.stream("GET", ENDPOINT, params={
                            "latitude": self.latitude, "longitude": self.longitude,
                            "models": "icon_global", "timezone": "UTC", "timeformat": "unixtime",
                            "hourly": "precipitation,temperature_2m,snowfall",
                            "past_days": 2, "forecast_days": 3,
                        }) as response:
                            response.raise_for_status()
                            body = bytearray()
                            async for chunk in response.aiter_bytes():
                                body.extend(chunk)
                                if len(body) > 131072:
                                    raise WeatherUnavailable()
                            import json
                            payload = json.loads(body)
                self._cache = parse_open_meteo(payload, self.clock().astimezone(UTC))
                return self._cache
            except (httpx.HTTPError, TimeoutError, ValueError, TypeError):
                raise WeatherUnavailable() from None
