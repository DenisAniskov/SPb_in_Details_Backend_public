"""C7.3 v1 engine: pollen exposure of an EXISTING walk, per species.

The engine assesses the walk the user actually has (native MapKit geometry and
native durations). It never creates, replaces or re-orders a route, never
issues a routing request and never sends geometry upstream: the only upstream
point is one fixed pilot centroid, fetched once per hour.

TIME MODEL (ТЗ C7.3 §5)

A leg's crossing time is its share of the leg's native walking duration, so a
leg occupies exactly ``[clock, clock + walking_duration]`` and a POI visit
shifts every later leg by its dwell time. Unlike C7.2 there is no 40 m
sub-slicing: the source's finest temporal resolution is one hour and its
spatial resolution is ~11 km, so slicing a leg finer would imply a resolution
neither the hour bins nor the single cell actually has. A leg that straddles
an hour boundary is integrated exactly across that boundary (the integral is a
sum of per-bin overlaps), which is the honest treatment.

POI dwell time is NOT accumulated: the source says nothing about the pollen
level inside a museum, cafe or courtyard, so the visit is used only to shift
later legs (ТЗ C7.3 §5).

HONESTY RULES THIS MODULE ENFORCES

* ``null`` (past the four-day horizon) is unknown — never zero, never
  extrapolated;
* a confirmed zero is reported as a zero: the source does report ``0.0``, and
  this engine preserves that value instead of hiding it as a gap;
* spatial claims stop at the model's own cell: a leg outside the fetched
  CAMS cell stays unknown rather than inheriting a neighbouring cell's value;
* the same concentration applies to every leg that falls in the same hour —
  two neighbouring streets are never given an artificial difference, and a
  leg's value depends on its TIME ONLY, never on where it is;
* an ``unavailable`` answer publishes NO numbers at all, per-species included
  (C7.3.1): when not one hour of the walk has the complete six-species panel,
  the route-level verdict is "unavailable" and every species is published as
  ``unknown`` — while its ``known_seconds``/``known_share`` stay exactly as the
  source produced them, because withholding a number must never claim the
  source had none;
* "the source had nothing at all" and "the source had values, but no hour had
  all six species at once" are different failures and carry different reason
  codes (``pollen_no_data`` vs ``pollen_no_complete_panel``);
* no species is summed into an "allergy risk" and no concentration is
  compared with a danger threshold — this version publishes concentrations,
  coverage and the exposure integral, nothing more.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Sequence

from app.services.pollen_engine.model import (
    Window,
    exposure_for_species,
    exposure_status,
    panel_known_seconds_per_window,
    total_seconds,
)
from app.services.pollen_engine.source import SPECIES, UNIT, PollenSeries
from app.services.shade_engine.geo import haversine_distance_m

METHOD = "experimental_pollen_exposure"
#: c7_3.1 — first pollen version: instantaneous [t, t+1h) bins, per-species
#: exposure integral over walking time only, coverage limited to the fetched
#: CAMS cell.
METHOD_VERSION = "c7_3.1"

MAX_TOTAL_POINTS = 5000
MAX_ELAPSED_SECONDS = 24 * 3600

#: A series older than this is not reused even if a caller hands one over.
SOURCE_MAX_AGE_MINUTES = 90.0

BASE_LIMITATIONS: tuple[str, ...] = (
    "Это концентрации пыльцы в воздухе, а не индивидуальная вдыхаемая доза и "
    "не прогноз симптомов.",
    "Данные — модельный прогноз CAMS (Copernicus), а не измерения: временной "
    "шаг 1 час, шаг сетки 0,1° (около 11 км) для всей городской агломерации.",
    "Модель не различает улицу, двор, парк и тротуар внутри одной ячейки: "
    "различия между соседними улицами не рассчитываются и не показываются.",
    "Порогов опасности и медицинских категорий в этой версии нет: они "
    "потребовали бы проверенного источника для каждого вида пыльцы.",
    "Концентрации разных видов не суммируются в единый «риск аллергии».",
    "Часы за пределами четырёхдневного горизонта прогноза остаются "
    "неизвестными: пропуск не заполняется нулём и не продолжается по инерции.",
    "Ноль — это значение модели, а не подтверждённое отсутствие пыльцы в "
    "воздухе; в межсезонье ноль ожидаем и не означает индивидуальной "
    "безопасности.",
    "Время осмотра POI не входит в накопление: о среде внутри POI данных нет.",
)

BASE_ASSUMPTIONS: tuple[str, ...] = (
    "Концентрация считается постоянной внутри часа [t, t+1h) — источник не "
    "даёт более тонкого временного разрешения, интерполяция между часами не "
    "выполняется.",
    "Время прохождения участка — доля его длины от native-длительности этапа "
    "(как в C7.1/C7.2); посещение POI сдвигает все последующие этапы.",
    "Экспозиция суммируется только по времени ходьбы; при остановке на POI "
    "накопление не продолжается.",
    "Все этапы относятся к одной модельной ячейке CAMS: значение зависит "
    "только от часа прохождения и не зависит от координат этапа внутри ячейки.",
    "Экспозиция покрывает только известную долю прогулки — рядом с ней "
    "публикуется доля покрытия, продлевать интеграл на неизвестное время "
    "нельзя.",
)


class PollenAnalysisInputError(ValueError):
    """Raised for inputs the analysis cannot process honestly."""


@dataclass(frozen=True)
class PollenLegInput:
    geometry: tuple[tuple[float, float], ...]  # (lat, lon) WGS84
    walking_duration_seconds: float
    visit_duration_seconds_after_leg: float = 0.0


@dataclass(frozen=True)
class SpeciesResult:
    code: str
    name_ru: str
    unit: str
    status: str  # available | partial | unknown
    known_seconds: float
    total_seconds: float
    known_share: float
    mean_grains_m3: float | None
    peak_grains_m3: float | None
    peak_at: str | None
    exposure_grains_hours_m3: float | None


@dataclass(frozen=True)
class LegResult:
    leg_index: int
    length_m: float
    known_meters: float
    unknown_meters: float
    starts_at_seconds: float
    ends_at_seconds: float
    inside_source_cell: bool
    species: tuple[SpeciesResult, ...]


@dataclass(frozen=True)
class PollenAnalysisResult:
    status: str  # available | partial | unavailable | outside_coverage
    method: str
    method_version: str
    evaluated_period_start: str
    evaluated_period_end: str
    total_length_m: float
    known_meters: float
    unknown_meters: float
    coverage_share: float
    walking_seconds: float
    elapsed_seconds: float
    confidence: str  # low | unavailable
    reasons: tuple[str, ...]
    limitations: tuple[str, ...]
    assumptions: tuple[str, ...]
    species: tuple[SpeciesResult, ...]
    legs: tuple[LegResult, ...]
    diagnostics: dict


def _length_m(geometry: Sequence[tuple[float, float]]) -> float:
    return sum(haversine_distance_m(a[0], a[1], b[0], b[1])
               for a, b in zip(geometry, geometry[1:]))


def _withhold_numbers(item: SpeciesResult) -> SpeciesResult:
    """The same species with every number withheld and its coverage kept.

    Published on an ``unavailable``/``outside_coverage`` response, where the
    contract forbids per-species numbers (ТЗ C7.3.1): the status becomes
    ``unknown`` and all four number fields become ``None``, while
    ``known_seconds``/``known_share`` are copied unchanged. The source really
    did report values for those seconds — withholding the numbers is not a
    statement that no data existed.
    """
    return SpeciesResult(
        code=item.code, name_ru=item.name_ru, unit=item.unit, status="unknown",
        known_seconds=item.known_seconds, total_seconds=item.total_seconds,
        known_share=item.known_share, mean_grains_m3=None,
        peak_grains_m3=None, peak_at=None, exposure_grains_hours_m3=None)


class PollenEngine:
    def __init__(self, clock: Callable[[], datetime] | None = None, *,
                 source_max_age_minutes: float = SOURCE_MAX_AGE_MINUTES) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._source_max_age_minutes = source_max_age_minutes

    # ------------------------------------------------------------------ api
    def analyze(
        self,
        legs: Sequence[PollenLegInput],
        departure: datetime,
        series: PollenSeries | None,
        source_reason: str | None = None,
        *,
        include_legs: bool = False,
    ) -> PollenAnalysisResult:
        windows, lengths, elapsed = self._validate(legs, departure)
        period_end = departure + timedelta(seconds=elapsed)
        walking = total_seconds(windows)
        total_length = sum(lengths)

        if series is None:
            return self._without_numbers(
                departure, period_end, windows, lengths, elapsed,
                (False,) * len(legs),
                reasons=(source_reason or "pollen_unavailable",))

        problem = self._series_problem(series)
        if problem is not None:
            return self._without_numbers(
                departure, period_end, windows, lengths, elapsed,
                (False,) * len(legs), reasons=(problem,))

        inside = tuple(self._leg_inside_cell(series, leg) for leg in legs)
        if not any(inside):
            return self._without_numbers(
                departure, period_end, windows, lengths, elapsed, inside,
                reasons=("outside_source_cell",), status="outside_coverage")

        interval_start = series.interval_start
        interval_end = series.interval_end
        assert interval_start is not None and interval_end is not None
        if period_end <= interval_start or departure >= interval_end:
            return self._without_numbers(
                departure, period_end, windows, lengths, elapsed, inside,
                reasons=self._span_reasons(inside, ()),
                extra=("pollen_outside_window",))

        species_results = self._species_results(series, windows, departure, inside)
        known = self._known_meters(series, windows, departure, lengths, inside)
        coverage = known / total_length if total_length > 0 else 0.0

        reasons = self._span_reasons(inside, species_results)
        if known <= 1e-9:
            # Not one hour of the walk has the COMPLETE six-species panel, so
            # the route-level verdict is "unavailable" and no numbers may be
            # published (C7.3.1). Two different failures hide behind that:
            # the source had no values at all, or it had values for some
            # species and never for all six at one hour. The second case says
            # so instead of being reported as "no data".
            if any(item.known_seconds > 0 for item in species_results):
                reasons.append("pollen_no_complete_panel")
            else:
                reasons.append("pollen_no_data")
            return self._without_numbers(
                departure, period_end, windows, lengths, elapsed, inside,
                reasons=tuple(reasons), species=species_results)
        for code in ("modeled_pollen_concentration", "experimental_uncalibrated"):
            if code not in reasons:
                reasons.append(code)

        limitations = list(BASE_LIMITATIONS)
        if total_length - known > 1e-6:
            limitations.append(
                "Для части прогулки значения пыльцы неизвестны (нет часа в "
                "прогнозе или этап вне ячейки источника): такие метры не "
                "получают чисел и не считаются чистыми.")
        diagnostics = self._diagnostics(inside, species_results)
        return PollenAnalysisResult(
            status="partial" if total_length - known > 1e-6 else "available",
            method=METHOD, method_version=METHOD_VERSION,
            evaluated_period_start=departure.isoformat(),
            evaluated_period_end=period_end.isoformat(),
            total_length_m=total_length, known_meters=known,
            unknown_meters=total_length - known, coverage_share=coverage,
            walking_seconds=walking, elapsed_seconds=elapsed,
            confidence="low", reasons=tuple(reasons),
            limitations=tuple(limitations), assumptions=BASE_ASSUMPTIONS,
            species=species_results,
            legs=(self._leg_results(series, windows, departure, lengths, inside)
                  if include_legs else ()),
            diagnostics=diagnostics)

    # ------------------------------------------------------------- internals
    @staticmethod
    def _validate(legs: Sequence[PollenLegInput],
                  departure: datetime) -> tuple[tuple[Window, ...],
                                                list[float], float]:
        if not legs:
            raise PollenAnalysisInputError("at least one leg is required")
        if departure.tzinfo is None or departure.tzinfo.utcoffset(departure) is None:
            raise PollenAnalysisInputError(
                "departure must be timezone-aware (naive datetimes are rejected)")
        for leg in legs:
            if len(leg.geometry) < 2:
                raise PollenAnalysisInputError("every leg needs at least 2 route points")
            if not math.isfinite(leg.walking_duration_seconds) \
                    or leg.walking_duration_seconds <= 0:
                raise PollenAnalysisInputError("walking_duration_seconds must be positive")
            if not math.isfinite(leg.visit_duration_seconds_after_leg) \
                    or leg.visit_duration_seconds_after_leg < 0:
                raise PollenAnalysisInputError(
                    "visit_duration_seconds_after_leg must be >= 0")
        if sum(len(leg.geometry) for leg in legs) > MAX_TOTAL_POINTS:
            raise PollenAnalysisInputError("too many route points for one analysis")
        windows: list[Window] = []
        lengths: list[float] = []
        clock = 0.0
        for leg in legs:
            windows.append(Window(clock, clock + leg.walking_duration_seconds))
            lengths.append(_length_m(leg.geometry))
            clock += leg.walking_duration_seconds + leg.visit_duration_seconds_after_leg
        # The schedule span. A visit AFTER the last leg is deliberately excluded,
        # exactly as the request schema excludes it: nothing follows it, so it
        # extends neither the assessed period nor the accumulation.
        elapsed = sum(leg.walking_duration_seconds for leg in legs)
        elapsed += sum(leg.visit_duration_seconds_after_leg for leg in legs[:-1])
        if elapsed > MAX_ELAPSED_SECONDS:
            raise PollenAnalysisInputError("walk elapsed time exceeds 24 hours")
        if sum(lengths) <= 0.0:
            raise PollenAnalysisInputError("the walk has no walkable length")
        return tuple(windows), lengths, elapsed

    def _series_problem(self, series: PollenSeries) -> str | None:
        now = self._clock().astimezone(timezone.utc)
        age = now - series.retrieved_at.astimezone(timezone.utc)
        if age > timedelta(minutes=self._source_max_age_minutes):
            return "pollen_stale"
        if not series.hours:
            return "pollen_unavailable"
        return None

    @staticmethod
    def _leg_inside_cell(series: PollenSeries, leg: PollenLegInput) -> bool:
        """Every vertex must be in the fetched cell — a partly covered leg is
        not silently represented by a neighbouring cell's value."""
        return all(series.contains_point(lat, lon) for lat, lon in leg.geometry)

    @staticmethod
    def _span_reasons(inside: Sequence[bool],
                      species_results: Sequence[SpeciesResult]) -> list[str]:
        reasons: list[str] = []
        if not all(inside):
            reasons.append("outside_source_cell")
        if any(item.status == "partial" for item in species_results):
            reasons.append("pollen_incomplete")
        return reasons

    def _species_results(self, series: PollenSeries, windows: tuple[Window, ...],
                         departure: datetime,
                         inside: Sequence[bool]) -> tuple[SpeciesResult, ...]:
        # A leg outside the cell contributes nothing but still counts as walk
        # time in ``total_seconds``: its share must show up as unknown.
        covered = tuple(window for window, ok in zip(windows, inside) if ok)
        total = total_seconds(windows)
        results = []
        for index, species in enumerate(SPECIES):
            if covered:
                exposure = exposure_for_species(series, covered, departure, index)
                known, integral = exposure.known_seconds, \
                    exposure.exposure_grains_hours_m3
                mean, peak, peak_at = (exposure.mean_grains_m3,
                                       exposure.peak_grains_m3,
                                       exposure.peak_at)
            else:
                known = integral = 0.0
                mean = peak = peak_at = None
            results.append(SpeciesResult(
                code=species.code, name_ru=species.name_ru, unit=UNIT,
                status=exposure_status(known, total),
                known_seconds=round(known, 1), total_seconds=round(total, 1),
                known_share=round((known / total) if total > 0 else 0.0, 4),
                mean_grains_m3=None if mean is None else round(mean, 4),
                peak_grains_m3=None if peak is None else round(peak, 4),
                peak_at=peak_at.isoformat() if peak_at else None,
                exposure_grains_hours_m3=(None if mean is None
                                          else round(integral, 4))))
        return tuple(results)

    @staticmethod
    def _known_meters(series: PollenSeries, windows: tuple[Window, ...],
                      departure: datetime, lengths: Sequence[float],
                      inside: Sequence[bool]) -> float:
        """Meters of the walk where the WHOLE pollen panel has a value.

        Time is proportional to length inside a leg (native duration scaled by
        the length share), so a leg's known meters are its length times the
        panel-known share of its own walking window. A leg outside the source
        cell contributes no known meters at all.
        """
        panel = panel_known_seconds_per_window(series, windows, departure)
        known = 0.0
        for window, length, ok, panel_seconds in zip(windows, lengths, inside,
                                                    panel):
            if not ok or window.duration_seconds <= 0 or length <= 0:
                continue
            known += length * (panel_seconds / window.duration_seconds)
        return known

    def _leg_results(self, series: PollenSeries, windows: tuple[Window, ...],
                     departure: datetime, lengths: Sequence[float],
                     inside: Sequence[bool]) -> tuple[LegResult, ...]:
        panel = panel_known_seconds_per_window(series, windows, departure)
        results = []
        for index, (window, length, ok, panel_seconds) in enumerate(
                zip(windows, lengths, inside, panel)):
            known_meters = 0.0
            if ok and length > 0 and window.duration_seconds > 0:
                known_meters = length * (panel_seconds / window.duration_seconds)
            species: tuple[SpeciesResult, ...] = ()
            if ok:
                species = self._species_results(series, (window,), departure,
                                                (True,))
            results.append(LegResult(
                leg_index=index, length_m=round(length, 1),
                known_meters=round(known_meters, 1),
                unknown_meters=round(max(0.0, length - known_meters), 1),
                starts_at_seconds=round(window.start_seconds, 1),
                ends_at_seconds=round(window.end_seconds, 1),
                inside_source_cell=bool(ok), species=species))
        return tuple(results)

    @staticmethod
    def _diagnostics(inside: Sequence[bool],
                     species_results: Sequence[SpeciesResult], *,
                     withheld: int = 0) -> dict:
        """What the ENGINE found in the source — not what the response publishes.

        On the withholding path ``species`` in the payload reports every
        species as ``unknown`` while these counts still describe the computed
        outcome (how many species were fully/partly covered), and
        ``species_withheld`` states how many sets of numbers were withheld.
        """
        return {
            "legs": len(inside),
            "legs_outside_source_cell": sum(1 for ok in inside if not ok),
            "species_available": sum(1 for item in species_results
                                     if item.status == "available"),
            "species_partial": sum(1 for item in species_results
                                   if item.status == "partial"),
            "species_unknown": sum(1 for item in species_results
                                   if item.status == "unknown"),
            "species_withheld": withheld,
        }

    def _without_numbers(self, departure: datetime, period_end: datetime,
                         windows: tuple[Window, ...], lengths: Sequence[float],
                         elapsed: float, inside: Sequence[bool], *,
                         reasons: Sequence[str],
                         status: str = "unavailable",
                         species: tuple[SpeciesResult, ...] = (),
                         extra: Sequence[str] = ()) -> PollenAnalysisResult:
        """The no-numbers answer, and the ONLY way one is ever built.

        The contract for ``unavailable``/``outside_coverage`` forbids every
        number, per-species ones included (ТЗ C7.3 §6, C7.3.1), so this method
        enforces that itself: a computed species list handed over by the
        caller is published with its numbers withheld, never as-is.
        """
        merged = list(reasons)
        for code in extra:
            if code not in merged:
                merged.append(code)
        if species:
            published = tuple(_withhold_numbers(item) for item in species)
            # How many species the source really had values for and this
            # answer therefore REFUSES to publish — zero when the source had
            # nothing to withhold.
            withheld = sum(1 for item in species if item.known_seconds > 0)
        else:
            published = self._species_results_empty(windows)
            withheld = 0
        total_length = sum(lengths)
        return PollenAnalysisResult(
            status=status, method=METHOD, method_version=METHOD_VERSION,
            evaluated_period_start=departure.isoformat(),
            evaluated_period_end=period_end.isoformat(),
            total_length_m=total_length, known_meters=0.0,
            unknown_meters=total_length, coverage_share=0.0,
            walking_seconds=total_seconds(windows), elapsed_seconds=elapsed,
            confidence="unavailable", reasons=tuple(merged),
            limitations=BASE_LIMITATIONS, assumptions=BASE_ASSUMPTIONS,
            species=published, legs=(),
            diagnostics=self._diagnostics(inside, species, withheld=withheld))

    @staticmethod
    def _species_results_empty(
            windows: tuple[Window, ...]) -> tuple[SpeciesResult, ...]:
        total = round(total_seconds(windows), 1)
        return tuple(SpeciesResult(
            code=species.code, name_ru=species.name_ru, unit=UNIT,
            status="unknown", known_seconds=0.0, total_seconds=total,
            known_share=0.0, mean_grains_m3=None, peak_grains_m3=None,
            peak_at=None, exposure_grains_hours_m3=None) for species in SPECIES)
