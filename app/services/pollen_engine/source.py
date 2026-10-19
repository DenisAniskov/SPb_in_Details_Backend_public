"""C7.3 pollen source: modelled CAMS pollen, never labelled an observation.

The provider is a single fixed pilot centroid — the user's route geometry is
never sent upstream — and it follows the C7.2 weather contract (bounded cache,
single-flight, byte cap, timeouts, strict units). Two source properties are
load-bearing for the engine and are enforced here, not assumed:

* **Hour semantics are NOT the precipitation semantics.** Open-Meteo air
  quality values are "an instantaneous value for the indicated hour", so a
  value stamped ``t`` is valid for ``[t, t+1h)`` — the opposite of the
  C7.2 rain bins, which describe ``(t-1h, t]``. Reusing the accumulated-rain
  logic on pollen would be a category error (ТЗ C7.3 §5).
* **``null`` is a gap, never a zero.** Past the four-day pollen horizon the
  API keeps emitting hour stamps and fills the values with ``null``; those
  hours stay unknown. Concentrations of exactly ``0.0`` are *values* the
  source reports and are preserved as such.
"""

from __future__ import annotations

import asyncio
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol

import httpx

UTC = timezone.utc
SOURCE_URL = "https://open-meteo.com/en/docs/air-quality-api"
ENDPOINT = "https://air-quality-api.open-meteo.com/v1/air-quality"
USER_AGENT = ("SmartNavigator-SPb-C7.3/1.0 research pilot "
              "https://github.com/DenisAniskov/SmartNavigator_Spb")

#: The complete pollen panel the CAMS European forecast provides for Europe.
UNIT = "grains/m³"


@dataclass(frozen=True)
class PollenSpecies:
    code: str
    name_ru: str


SPECIES: tuple[PollenSpecies, ...] = (
    PollenSpecies("alder_pollen", "Ольха"),
    PollenSpecies("birch_pollen", "Берёза"),
    PollenSpecies("grass_pollen", "Злаки"),
    PollenSpecies("mugwort_pollen", "Полынь"),
    PollenSpecies("ragweed_pollen", "Амброзия"),
    PollenSpecies("olive_pollen", "Маслина"),
)
SPECIES_CODES: tuple[str, ...] = tuple(species.code for species in SPECIES)

#: CAMS Europe is a regular 0.1° grid and a requested point is snapped to the
#: nearest cell centre (verified in docs/phase_c7_3_acquisition/
#: pollen_grid_probe.json: 59.849 → 59.8, 59.851 → 59.9, 30.249 → 30.2,
#: 30.251 → 30.3). ~11 km nominal.
GRID_STEP_DEGREES = 0.1
NOMINAL_RESOLUTION_METERS = 11000

#: Sanity bound in grains/m³: catches a unit change upstream instead of
#: passing an implausible number through as a concentration.
MAX_CONCENTRATION = 100_000.0
MAX_SERIES_HOURS = 600


class PollenUnavailable(Exception):
    def __init__(self, reason: str = "pollen_unavailable"):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class PollenHour:
    """Concentrations valid for ``[starts_at, starts_at + 1 hour)``.

    ``concentrations[i]`` belongs to ``SPECIES[i]``; ``None`` is a gap.
    """

    starts_at: datetime
    concentrations: tuple[float | None, ...]


@dataclass(frozen=True)
class PollenSeries:
    hours: tuple[PollenHour, ...]
    retrieved_at: datetime
    valid_until: datetime
    cell_latitude: float
    cell_longitude: float
    contacted_latitude: float
    contacted_longitude: float
    provider: str = "Open-Meteo"
    model: str = "cams_europe"
    kind: str = "modeled_forecast_and_recent_archived_forecast"

    @property
    def interval_start(self) -> datetime | None:
        return self.hours[0].starts_at if self.hours else None

    @property
    def interval_end(self) -> datetime | None:
        """End of the last bin — the model's own horizon edge."""
        return (self.hours[-1].starts_at + timedelta(hours=1)
                if self.hours else None)

    def cell_bounds(self) -> tuple[float, float, float, float]:
        """(lat_min, lon_min, lat_max, lon_max) of the snapped model cell.

        This is the ONLY area a fetch at the pilot centroid can describe: a
        point outside it belongs to a different CAMS cell whose value was not
        requested. Bounds are closed on purpose — a point exactly on the cell
        edge lies within the model's own ~11 km uncertainty, so neither
        neighbouring cell is better supported there.
        """
        half = GRID_STEP_DEGREES / 2.0
        return (round(self.cell_latitude - half, 6),
                round(self.cell_longitude - half, 6),
                round(self.cell_latitude + half, 6),
                round(self.cell_longitude + half, 6))

    def contains_point(self, latitude: float, longitude: float) -> bool:
        lat_min, lon_min, lat_max, lon_max = self.cell_bounds()
        return (lat_min <= latitude <= lat_max
                and lon_min <= longitude <= lon_max)

    def provenance(self) -> dict:
        return {
            "available": True, "provider": self.provider, "model": self.model,
            "model_name": ("CAMS European Air Quality Forecast "
                           "(CAMS ENSEMBLE)"),
            "kind": self.kind, "observed_at": None, "model_issued_at": None,
            "retrieved_at": self.retrieved_at.isoformat(),
            "valid_until": self.valid_until.isoformat(),
            "temporal_resolution_seconds": 3600,
            "spatial_resolution_meters": NOMINAL_RESOLUTION_METERS,
            "grid_step_degrees": GRID_STEP_DEGREES,
            "cell_latitude": self.cell_latitude,
            "cell_longitude": self.cell_longitude,
            "cell_bounds": list(self.cell_bounds()),
            "contacted_point": [self.contacted_latitude,
                                self.contacted_longitude],
            "interval_start": (self.interval_start.isoformat()
                               if self.interval_start else None),
            "interval_end": (self.interval_end.isoformat()
                             if self.interval_end else None),
            "unit": UNIT,
            "species": list(SPECIES_CODES),
            "license": "CC-BY-4.0", "source_url": SOURCE_URL,
            "attribution": ("Pollen data by Open-Meteo.com; CAMS ENSEMBLE "
                            "(Copernicus Atmosphere Monitoring Service) model"),
            "time_semantics": ("instantaneous concentration valid for the "
                               "indicated hour [t, t+1h); null is a gap, not zero"),
        }


def unavailable_provenance(reason: str) -> dict:
    return {
        "available": False, "provider": "Open-Meteo", "model": "cams_europe",
        "model_name": "CAMS European Air Quality Forecast (CAMS ENSEMBLE)",
        "kind": "modeled_forecast_and_recent_archived_forecast",
        "observed_at": None, "model_issued_at": None, "retrieved_at": None,
        "valid_until": None, "temporal_resolution_seconds": 3600,
        "spatial_resolution_meters": NOMINAL_RESOLUTION_METERS,
        "grid_step_degrees": GRID_STEP_DEGREES, "cell_latitude": None,
        "cell_longitude": None, "cell_bounds": None, "contacted_point": None,
        "interval_start": None, "interval_end": None, "unit": UNIT,
        "species": list(SPECIES_CODES), "license": "CC-BY-4.0",
        "source_url": SOURCE_URL,
        "attribution": ("Pollen data by Open-Meteo.com; CAMS ENSEMBLE "
                        "(Copernicus Atmosphere Monitoring Service) model"),
        "reason": reason,
    }


class PollenProvider(Protocol):
    async def fetch(self) -> PollenSeries: ...


class DisabledPollenProvider:
    """The honest default: no source until the operator opts in."""

    async def fetch(self) -> PollenSeries:
        raise PollenUnavailable("pollen_not_configured")


def parse_open_meteo_air_quality(
    payload: dict, now: datetime, *,
    contacted_latitude: float, contacted_longitude: float,
    cell_tolerance_degrees: float = 0.25,
) -> PollenSeries:
    """Strict shape/unit/chronology validation; ``null`` hours remain gaps."""
    try:
        if not isinstance(payload, dict) or payload.get("error"):
            raise ValueError()
        if payload.get("utc_offset_seconds") != 0:
            raise ValueError()
        units = payload["hourly_units"]
        if units["time"] != "unixtime" or any(
                units[code] != UNIT for code in SPECIES_CODES):
            raise ValueError()
        hourly = payload["hourly"]
        times = hourly["time"]
        columns = [hourly[code] for code in SPECIES_CODES]
        if not 1 <= len(times) <= MAX_SERIES_HOURS:
            raise ValueError()
        if any(len(column) != len(times) for column in columns):
            raise ValueError()
        latitude = payload["latitude"]
        longitude = payload["longitude"]
        if any(type(value) not in (float, int) or not math.isfinite(value)
               for value in (latitude, longitude)):
            raise ValueError()
        if abs(latitude - contacted_latitude) > cell_tolerance_degrees \
                or abs(longitude - contacted_longitude) > cell_tolerance_degrees:
            raise ValueError()

        hours = []
        previous = None
        for index, stamp in enumerate(times):
            if type(stamp) is not int or (previous is not None
                                          and stamp - previous != 3600):
                raise ValueError()
            previous = stamp
            concentrations: list[float | None] = []
            for column in columns:
                value = column[index]
                if value is None:
                    concentrations.append(None)
                    continue
                # bool is an int subclass; a boolean is never a concentration.
                if type(value) not in (float, int) or not math.isfinite(value):
                    raise ValueError()
                if not 0.0 <= value <= MAX_CONCENTRATION:
                    raise ValueError()
                concentrations.append(float(value))
            hours.append(PollenHour(datetime.fromtimestamp(stamp, UTC),
                                    tuple(concentrations)))
        return PollenSeries(
            hours=tuple(hours), retrieved_at=now,
            valid_until=now + timedelta(minutes=60),
            cell_latitude=round(latitude / GRID_STEP_DEGREES)
            * GRID_STEP_DEGREES,
            cell_longitude=round(longitude / GRID_STEP_DEGREES)
            * GRID_STEP_DEGREES,
            contacted_latitude=contacted_latitude,
            contacted_longitude=contacted_longitude)
    except (KeyError, TypeError, ValueError, OverflowError):
        raise PollenUnavailable("pollen_source_invalid") from None


class OpenMeteoPollenProvider:
    """One fixed pilot centroid, one bounded cache; never the user's route.

    Instantiate ONLY after the operator's explicit noncommercial free-tier
    opt-in. No paid keys and no arbitrary upstream URLs are accepted here.
    """

    def __init__(self, *, latitude: float, longitude: float,
                 clock: Callable[[], datetime] | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.latitude = latitude
        self.longitude = longitude
        self.clock = clock or (lambda: datetime.now(UTC))
        self.transport = transport
        self._cache: PollenSeries | None = None
        self._lock = asyncio.Lock()

    async def fetch(self) -> PollenSeries:
        now = self.clock().astimezone(UTC)
        if self._cache and self._cache.retrieved_at <= now < self._cache.valid_until:
            return self._cache
        async with self._lock:
            now = self.clock().astimezone(UTC)
            if self._cache and self._cache.retrieved_at <= now < self._cache.valid_until:
                return self._cache
            try:
                async with asyncio.timeout(8):
                    async with httpx.AsyncClient(timeout=6.0,
                                                 transport=self.transport,
                                                 follow_redirects=False) as client:
                        async with client.stream(
                                "GET", ENDPOINT, headers={"User-Agent": USER_AGENT},
                                params={
                                    "latitude": self.latitude,
                                    "longitude": self.longitude,
                                    "hourly": ",".join(SPECIES_CODES),
                                    "models": "cams_europe",
                                    "timezone": "UTC", "timeformat": "unixtime",
                                    "past_days": 1, "forecast_days": 4,
                                }) as response:
                            response.raise_for_status()
                            body = bytearray()
                            async for chunk in response.aiter_bytes():
                                body.extend(chunk)
                                if len(body) > 262144:
                                    raise PollenUnavailable()
                            payload = json.loads(body)
                self._cache = parse_open_meteo_air_quality(
                    payload, self.clock().astimezone(UTC),
                    contacted_latitude=self.latitude,
                    contacted_longitude=self.longitude)
                return self._cache
            except PollenUnavailable:
                raise
            except (httpx.HTTPError, TimeoutError, ValueError, TypeError):
                raise PollenUnavailable() from None
