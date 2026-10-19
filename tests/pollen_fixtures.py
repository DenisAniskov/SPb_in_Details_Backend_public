"""Synthetic C7.3 pollen fixtures for unit and API tests.

These are SYNTHETIC and prove processing logic only — never real-world
accuracy. The one real, stored source evidence lives in
``docs/phase_c7_3_acquisition/`` and is verified by
``scripts.probe_phase_c7_3_pollen --verify``; it is never read at runtime.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from app.services.pollen_engine.engine import PollenLegInput
from app.services.pollen_engine.source import (
    GRID_STEP_DEGREES,
    SPECIES_CODES,
    UNIT,
    PollenHour,
    PollenSeries,
)

#: The real pilot centroid used by the production provider, and the CAMS cell
#: it snaps to (verified: docs/phase_c7_3_acquisition/pollen_grid_probe.json).
ORIGIN_LAT, ORIGIN_LON = 59.9405, 30.3165
CELL_CENTRE = (59.9, 30.3)
CELL_BOUNDS = (59.85, 30.25, 59.95, 30.35)
#: A point that legitimately belongs to a DIFFERENT CAMS cell (59.5 → cell
#: centre 59.5), used to prove that a single-cell fetch is not applied there.
OTHER_CELL_LAT, OTHER_CELL_LON = 59.50, 30.31


def latlon(x_m: float, y_m: float) -> tuple[float, float]:
    """Local planar metres → (lat, lon) near the pilot centroid."""
    lat = ORIGIN_LAT + y_m / 111_320.0
    lon = ORIGIN_LON + x_m / (111_320.0 * math.cos(math.radians(ORIGIN_LAT)))
    return lat, lon


def hour_floor(when: datetime) -> datetime:
    """The bin a moment falls into: pollen bins cover ``[t, t+1h)``."""
    return when.replace(minute=0, second=0, microsecond=0)


def constant(value: float):
    """A ``concentration(code, starts_at)`` callable with one value."""
    return lambda code, starts_at: float(value)


def series(t0: datetime, *, concentration=None, hours_before: int = 2,
           hours_after: int = 19, retrieved_at: datetime | None = None,
           missing: frozenset[datetime] | set[datetime] = frozenset(),
           cell: tuple[float, float] = CELL_CENTRE,
           contacted: tuple[float, float] = (ORIGIN_LAT, ORIGIN_LON),
           unit_species: tuple[str, ...] = SPECIES_CODES) -> PollenSeries:
    """Hourly bins around ``t0``; the bin stamped ``t`` is valid for ``[t, t+1h)``.

    ``concentration(code, starts_at) -> float | None`` supplies values; the
    default is ``0.0`` for every species (the off-season state the real source
    actually returns). ``missing`` lists bin stamps whose WHOLE panel is
    ``null`` — a gap, which is unknown and never a zero.

    This is the pollen convention, the OPPOSITE of the C7.2 rain fixture,
    where a bin describes ``(t-1h, t]``.
    """
    concentration = concentration or constant(0.0)
    base = hour_floor(t0) - timedelta(hours=hours_before)
    hours = []
    for index in range(hours_before + hours_after):
        starts_at = base + timedelta(hours=index)
        values: tuple[float | None, ...]
        if starts_at in missing:
            values = (None,) * len(SPECIES_CODES)
        else:
            values = tuple(
                None if code not in unit_species
                else concentration(code, starts_at)
                for code in SPECIES_CODES)
        hours.append(PollenHour(starts_at=starts_at,
                                concentrations=values))
    retrieved = retrieved_at or t0
    return PollenSeries(hours=tuple(hours), retrieved_at=retrieved,
                        valid_until=retrieved + timedelta(minutes=60),
                        cell_latitude=cell[0], cell_longitude=cell[1],
                        contacted_latitude=contacted[0],
                        contacted_longitude=contacted[1])


def leg(geometry: tuple[tuple[float, float], ...], seconds: float = 600.0,
        visit: float = 0.0) -> PollenLegInput:
    return PollenLegInput(geometry=geometry, walking_duration_seconds=seconds,
                          visit_duration_seconds_after_leg=visit)


def straight(distance_m: float = 100.0, *, x_from: float = -50.0,
             y_m: float = 0.0, points: int = 2) -> tuple[tuple[float, float], ...]:
    """A straight east-west leg of ``distance_m`` near the centroid."""
    x_to = x_from + distance_m
    if points <= 2:
        return (latlon(x_from, y_m), latlon(x_to, y_m))
    step = (x_to - x_from) / (points - 1)
    return tuple(latlon(x_from + step * index, y_m) for index in range(points))


#: Re-exported so tests can assert the grid facts without importing internals.
GRID_STEP = GRID_STEP_DEGREES
CONCENTRATION_UNIT = UNIT
