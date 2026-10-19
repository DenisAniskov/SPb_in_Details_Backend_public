"""C7.3 exposure math over a pollen series: piecewise-constant, in real units.

WHAT THIS MODULE COMPUTES

For each species the engine integrates the source's hourly concentration over
the walk's *walking* windows and reports the result in the units of the
source::

    exposure[grains·h/m³] = Σ_bins  c[grains/m³] × overlap_seconds / 3600

so ``exposure`` is a concentration-time integral per cubic metre of air, and
``mean = exposure / known_hours`` is the time-weighted mean over the parts of
the walk that HAVE a value.

WHAT IT DELIBERATELY DOES NOT DO

* It never interpolates between hours, never extrapolates past the last known
  hour and never fills a ``null`` with a zero: a value stamped ``t`` is the
  source's own statement about the hour ``[t, t+1h)``, which is the finest
  resolution CAMS publishes. ``null`` means the hour is unknown.
* It does not sum species into a single index and does not compare a
  concentration with any threshold. No pollen-risk or medical category exists
  here: the source provides concentrations, not danger levels, and this
  version therefore has no thresholds at all (ТЗ C7.3 §4).
* ``exposure`` covers ONLY the known share of the walk. It is written to the
  response together with ``known_share`` precisely so a client cannot mistake
  a partial integral for a whole-walk one.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.services.pollen_engine.source import PollenHour, PollenSeries

HOUR_SECONDS = 3600.0


@dataclass(frozen=True)
class Window:
    """A walking window as offsets in seconds from the assessment moment."""

    start_seconds: float
    end_seconds: float

    @property
    def duration_seconds(self) -> float:
        return max(0.0, self.end_seconds - self.start_seconds)


@dataclass(frozen=True)
class SpeciesExposure:
    code: str
    known_seconds: float
    total_seconds: float
    exposure_grains_hours_m3: float
    mean_grains_m3: float | None
    peak_grains_m3: float | None
    peak_at: datetime | None

    @property
    def known_share(self) -> float:
        return (self.known_seconds / self.total_seconds
                if self.total_seconds > 0 else 0.0)

    @property
    def status(self) -> str:
        return exposure_status(self.known_seconds, self.total_seconds)


def exposure_status(known_seconds: float, total_seconds: float) -> str:
    if total_seconds <= 0 or known_seconds <= 0:
        return "unknown"
    if known_seconds >= total_seconds - 1e-9:
        return "available"
    return "partial"


def total_seconds(windows: tuple[Window, ...]) -> float:
    return sum(window.duration_seconds for window in windows)


def _bin_starts(series: PollenSeries) -> list[datetime]:
    return [hour.starts_at for hour in series.hours]


def _overlapping_bins(series: PollenSeries, starts: list[datetime],
                      window: Window, departure: datetime):
    """Yield ``(hour, overlap_seconds)`` for every bin touching the window."""
    window_start = departure + timedelta(seconds=window.start_seconds)
    window_end = departure + timedelta(seconds=window.end_seconds)
    index = max(0, bisect.bisect_right(starts, window_start) - 1)
    for hour in series.hours[index:]:
        bin_start = hour.starts_at
        if bin_start >= window_end:
            break
        bin_end = bin_start + timedelta(seconds=HOUR_SECONDS)
        overlap = (min(window_end, bin_end)
                   - max(window_start, bin_start)).total_seconds()
        if overlap > 0:
            yield hour, overlap


def exposure_for_species(series: PollenSeries, windows: tuple[Window, ...],
                         departure: datetime, species_index: int) -> SpeciesExposure:
    """Integrate one species over the windows; gaps stay out of the numbers."""
    starts = _bin_starts(series)
    known = 0.0
    integral = 0.0
    peak: float | None = None
    peak_at: datetime | None = None
    for window in windows:
        for hour, overlap in _overlapping_bins(series, starts, window, departure):
            value = hour.concentrations[species_index]
            if value is None:
                continue
            known += overlap
            integral += value * overlap / HOUR_SECONDS
            if peak is None or value > peak:
                peak = value
                peak_at = hour.starts_at
    return SpeciesExposure(
        code="", known_seconds=known, total_seconds=total_seconds(windows),
        exposure_grains_hours_m3=integral,
        mean_grains_m3=(integral / (known / HOUR_SECONDS)) if known > 0 else None,
        peak_grains_m3=peak, peak_at=peak_at)


def panel_known_seconds(series: PollenSeries, windows: tuple[Window, ...],
                        departure: datetime) -> float:
    """Seconds of the walk for which EVERY species has a value.

    The response's route-level ``known_meters`` is built from this number: the
    card shows the whole panel together, so an hour where any species is
    missing is not presented as a complete pollen picture. Per-species
    ``known_share`` is reported separately for the exact view.
    """
    starts = _bin_starts(series)
    known = 0.0
    for window in windows:
        for hour, overlap in _overlapping_bins(series, starts, window, departure):
            if all(value is not None for value in hour.concentrations):
                known += overlap
    return known


def panel_known_seconds_per_window(series: PollenSeries,
                                   windows: tuple[Window, ...],
                                   departure: datetime) -> list[float]:
    """``panel_known_seconds`` per window, in the same order as ``windows``."""
    return [panel_known_seconds(series, (window,), departure)
            for window in windows]


def hours_in(seconds: float) -> float:
    return seconds / HOUR_SECONDS


def overlapping_hours(series: PollenSeries, windows: tuple[Window, ...],
                      departure: datetime) -> float:
    """SECONDS of the walk that the source's interval actually covers.

    A diagnostic, not an input to any number: it separates "the walk ran past
    the model's horizon" from "the source returned no value for hours it does
    cover", which are different failures and must not share a reason code.
    """
    starts = _bin_starts(series)
    covered = 0.0
    for window in windows:
        for _, overlap in _overlapping_bins(series, starts, window, departure):
            covered += overlap
    return covered


__all__ = ["HOUR_SECONDS", "SpeciesExposure", "Window", "exposure_for_species",
           "exposure_status", "hours_in", "overlapping_hours",
           "panel_known_seconds", "panel_known_seconds_per_window",
           "total_seconds"]
