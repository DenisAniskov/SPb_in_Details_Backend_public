"""Deterministic C7.2 v1 engine: relative wet-surface risk of an EXISTING walk.

The engine assesses the walk the user actually has (native MapKit geometry and
native durations) — it never creates, replaces or re-orders a route, and it
makes zero routing requests. Geometry time semantics are identical to the
C7.1 shade analysis: a leg is cut into ≈40 m pieces ALONG the real polyline,
each piece's crossing time is its share of the leg's native walking duration,
and a POI visit shifts every later leg.

Honesty rules this module enforces (ТЗ C7.2 §2–§3):

* ``unknown`` is a first-class outcome and is never folded into ``low``:
  a slice with no confirmed OSM way, an ambiguous parallel, an unmapped
  surface, a slice whose precipitation history is incomplete, or a slice
  outside the pilot box contributes ``unknown_meters``;
* a missing ``surface`` tag is never replaced by asphalt;
* a covered way lowers — never zeroes — the modelled precipitation input;
* only rain that has ALREADY fallen by the slice's crossing moment may
  contribute: an hour bin that is still in the future (or still falling)
  enters the index only with its elapsed share, so a forecast cannot raise
  the risk prematurely;
* snow/freezing makes the wet-surface model inapplicable, and stale or
  out-of-window weather yields ``unavailable`` — never numbers from other
  times;
* all aggregation is weighted by metres of real geometry, so MapKit vertex
  density cannot change the result.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Sequence

from app.services.shade_engine.geo import haversine_distance_m
from app.services.puddle_engine.matching import (
    MATCH_MAX_DISTANCE_M,
    expand_bbox,
    point_in_bbox,
    sample_along,
    slice_match,
)
from app.services.puddle_engine.model import (
    SENSITIVITY_VARIANTS,
    PuddleModelConfig,
    classify,
    wetness_index,
)
from app.services.puddle_engine.surfaces import SurfaceFacts
from app.services.puddle_engine.ways_store import BBox, PuddleWaysProvider
from app.services.puddle_engine.weather import WeatherSeries

METHOD = "experimental_precipitation_surface_index"
#: c7_2.4 — final audit corrections on top of c7_2.3's unbroken lookback:
#: (1) bins that start after the assessment moment cannot invalidate a
#: complete past window (a future forecast gap must not hide a numeric
#: estimate); (2) snow/freezing is checked for every bin OVERLAPPING the walk
#: period, including a bin that ends after the walk's last moment.
METHOD_VERSION = "c7_2.4"

#: Same piece length as the C7.1 time-aware analysis (documented 40 m step).
SLICE_LENGTH_M = 40.0
MAX_TOTAL_POINTS = 5000
MAX_SLICES = 1500

BASE_LIMITATIONS: tuple[str, ...] = (
    "Оценка относительная: это не вероятность конкретной лужи, не глубина "
    "подтопления и не гарантия сухих ног.",
    "Погодные данные — модельный прогноз и модельный архив (не показания "
    "локального дождемера); временной шаг 1 час, пространственное разрешение "
    "около 11 км.",
    "Теги OSM о покрытии не описывают дренаж, уклон, глубину неровностей и "
    "работу ливневой канализации.",
    "Отсутствующий тег покрытия не считается асфальтом — такие участки "
    "остаются «неизвестно».",
    "Покрытие подтверждается только на линии, совпадающей с дорожкой OSM в "
    "пилотной зоне; пересечения и параллельные дорожки характеристики не "
    "передают.",
    "Пороговые значения модели экспериментальные и не калиброваны на "
    "местности; блок sensitivity показывает их влияние на ответ.",
    "Крытый проход не гарантирует сухого покрытия: возможны занос, боковой "
    "дождь и сток.",
)


class PuddleAnalysisInputError(ValueError):
    """Raised for inputs the analysis cannot process honestly."""


@dataclass(frozen=True)
class PuddleLegInput:
    geometry: tuple[tuple[float, float], ...]  # (lat, lon) WGS84
    walking_duration_seconds: float
    visit_duration_seconds_after_leg: float = 0.0


@dataclass(frozen=True)
class ClassifiedSegment:
    leg_index: int
    geometry: tuple[tuple[float, float], ...]
    length_m: float
    starts_at_seconds: float
    ends_at_seconds: float
    state: str  # low | moderate | high | unknown
    risk_index: float | None
    osm_id: int | None
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class PuddleAnalysisResult:
    status: str  # available | partial | unavailable | outside_coverage
    method: str
    method_version: str
    evaluated_period_start: str
    evaluated_period_end: str
    total_length_m: float
    known_meters: float
    low_meters: float
    moderate_meters: float
    high_meters: float
    unknown_meters: float
    coverage_share: float
    risk_index: float | None
    risk_level: str  # low | moderate | high | unknown
    confidence: str  # low | unavailable
    reasons: tuple[str, ...]
    limitations: tuple[str, ...]
    sensitivity: dict
    segments: tuple[ClassifiedSegment, ...] = ()
    diagnostics: dict = field(default_factory=dict)


@dataclass(frozen=True)
class _Slice:
    leg_index: int
    geometry: tuple[tuple[float, float], ...]
    mid_lat: float
    mid_lon: float
    length_m: float
    t0: float
    t1: float
    bbox: BBox

    def bbox_expanded(self) -> BBox:
        return expand_bbox(self.bbox, MATCH_MAX_DISTANCE_M)


@dataclass(frozen=True)
class _SliceEvidence:
    """What the (expensive) matching phase produced for one slice."""

    slice: _Slice
    inside_pilot: bool
    facts: SurfaceFacts | None
    ambiguous: bool
    unmatched: bool
    history: tuple[tuple[float, float], ...]
    current_mm: float
    history_complete: bool
    osm_id: int | None


def _bbox_of(geometry: Sequence[tuple[float, float]]) -> BBox:
    lats = [point[0] for point in geometry]
    lons = [point[1] for point in geometry]
    return (min(lats), min(lons), max(lats), max(lons))


def _bbox_overlap(first: BBox, second: BBox) -> bool:
    return (first[0] <= second[2] and first[2] >= second[0]
            and first[1] <= second[3] and first[3] >= second[1])


def _diagnostics(evidence: Sequence[_SliceEvidence]) -> dict:
    return {
        "slices": len(evidence),
        "matched_slices": sum(
            item.inside_pilot and item.facts is not None
            and item.facts.surface_class is not None for item in evidence),
        "ambiguous_slices": sum(item.ambiguous for item in evidence),
        "unmatched_slices": sum(item.unmatched for item in evidence),
        "outside_pilot_slices": sum(not item.inside_pilot for item in evidence),
        # Slices that HAD a confirmed surface and were still excluded from the
        # numbers because their precipitation history is incomplete.
        "history_incomplete_slices": sum(
            item.inside_pilot and item.facts is not None
            and item.facts.surface_class is not None
            and not item.history_complete for item in evidence),
    }


def _build_slices(legs: Sequence[PuddleLegInput]) -> list[_Slice]:
    """Cut the legs into ≈40 m pieces along the real polyline (C7.1 semantics)."""
    slices: list[_Slice] = []
    clock = 0.0
    for leg_index, leg in enumerate(legs):
        points = leg.geometry
        lengths = [haversine_distance_m(a[0], a[1], b[0], b[1])
                   for a, b in zip(points, points[1:])]
        leg_length = sum(lengths)
        if leg_length <= 0:
            clock += leg.walking_duration_seconds + leg.visit_duration_seconds_after_leg
            continue
        piece_count = max(1, int(round(leg_length / SLICE_LENGTH_M)))
        target = leg_length / piece_count

        # Cumulative distances enable O(log n) position lookups, so a very
        # dense native polyline (up to 2000 points per leg) does not turn the
        # slicing into a quadratic scan. The cut points and kept vertices are
        # exactly those of a linear scan.
        cumulative = [0.0]
        for seg_len in lengths:
            cumulative.append(cumulative[-1] + seg_len)

        def point_at(distance: float) -> tuple[float, float]:
            index = bisect.bisect_left(cumulative, distance - 1e-9)
            if index <= 0:
                return points[0]
            if index >= len(cumulative):
                return points[-1]
            seg_start = cumulative[index - 1]
            seg_len = cumulative[index] - seg_start
            if seg_len <= 0:
                return points[index]
            frac = (distance - seg_start) / seg_len
            (lat_a, lon_a), (lat_b, lon_b) = points[index - 1], points[index]
            return (lat_a + (lat_b - lat_a) * frac,
                    lon_a + (lon_b - lon_a) * frac)

        def piece_geometry(d0: float, d1: float) -> list[tuple[float, float]]:
            first = bisect.bisect_right(cumulative, d0 + 1e-9)
            last = bisect.bisect_left(cumulative, d1 - 1e-9)
            distances = [d0, *cumulative[first:last], d1]
            return [point_at(distance) for distance in distances]

        piece_start = 0.0
        for index in range(piece_count):
            piece_end = leg_length if index == piece_count - 1 else min(
                leg_length, piece_start + target)
            geometry = tuple(piece_geometry(piece_start, piece_end))
            piece_len = piece_end - piece_start
            mid_lat, mid_lon = point_at(piece_start + piece_len / 2.0)
            frac0 = piece_start / leg_length
            frac1 = piece_end / leg_length
            t0 = clock + frac0 * leg.walking_duration_seconds
            t1 = clock + frac1 * leg.walking_duration_seconds
            slices.append(_Slice(leg_index, geometry, mid_lat, mid_lon,
                                 piece_len, t0, t1, _bbox_of(geometry)))
            piece_start = piece_end
        clock += leg.walking_duration_seconds + leg.visit_duration_seconds_after_leg
    return slices


def _bin_inputs(series: WeatherSeries, when: datetime) -> tuple[
        list[tuple[float, float]], float]:
    """(history[(mm, age_hours)], current_mm) at ``when``.

    Hour bins describe the interval ``(end-1h, end]`` (Open-Meteo semantics).
    ONLY the share that has already fallen by ``when`` may enter the
    assessment:

    * a bin whose interval has not started yet contributes nothing (a
      forecast for 12:00-13:00 must not be counted at 11:59);
    * a bin still falling at ``when`` contributes its elapsed share
      ``(when - start) / 1h``, clamped to [0, 1] — rain is assumed uniform
      inside the modelled hour, which is an explicit model assumption, not
      an observation;
    * a bin that ended before ``when`` is fully fallen: within the last hour
      it is undecayed "current" rain, older bins become history that the
      wetness model decays.
    """
    history: list[tuple[float, float]] = []
    current = 0.0
    for hour in series.hours:
        start = hour.ends_at - timedelta(hours=1)
        if start >= when:
            continue  # entirely in the future: nothing has fallen yet
        if when < hour.ends_at:  # still falling: only its elapsed share
            share = (when - start).total_seconds() / 3600.0
            current += hour.precipitation_mm * min(1.0, max(0.0, share))
            continue
        age = (when - hour.ends_at).total_seconds() / 3600.0
        if age >= 1.0:
            history.append((hour.precipitation_mm, age))
        else:
            current += hour.precipitation_mm
    return history, current


def _history_complete(series: WeatherSeries, when: datetime,
                      required_hours: float) -> bool:
    """Is ``[when - required_hours, when]`` COVERED by real hourly bins?

    Coverage is an unbroken chain of hourly records, not a record count: the
    whole window — including the bin the assessment moment falls into or lies
    exactly on the end of — must be spanned without gaps. A record count
    treats a missing current hour plus an extra older hour as "complete" and
    silently drops the most recent rain, which can UNDERSTATE the risk; any
    gap (missing current bin included) leaves the window uncovered and the
    affected slices stay ``unknown`` (review correction C7.2.1, method
    c7_2.3).

    Only bins that START BEFORE the assessment moment can cover the past
    window. A gap in the forecast AFTER ``when`` says nothing about the rain
    that already fell and must not turn a complete lookback into an unknown
    (final audit correction c7_2.4; the same cut ``_bin_inputs`` applies).
    """
    window_start = when - timedelta(hours=required_hours)
    chain = [hour for hour in series.hours
             if hour.ends_at > window_start
             and hour.ends_at - timedelta(hours=1) < when]
    if not chain:
        return False
    covered_end = chain[0].ends_at - timedelta(hours=1)
    if covered_end > window_start:
        return False  # the earliest available data starts inside the window
    for hour in chain:
        start = hour.ends_at - timedelta(hours=1)
        if start > covered_end:
            return False  # a break between the previous bin and this one
        if hour.ends_at > covered_end:
            covered_end = hour.ends_at
    return covered_end >= when


def _freezing_or_snow(series: WeatherSeries, first: datetime,
                      last: datetime) -> bool:
    """Any freezing/snow bin whose interval overlaps ``[first, last]``?

    A bin describes ``(end - 1h, end]``. Checking only bins whose END lies in
    the evaluation window misses a bin that covers the walk's last minutes but
    ends after them (e.g. a walk ending 12:15 and a freezing 12:00–13:00 bin).
    Checking interval overlap closes that hole; a bin that starts exactly at
    ``last`` is after the walk and does not count.
    """
    for hour in series.hours:
        start = hour.ends_at - timedelta(hours=1)
        if hour.ends_at >= first and start < last and (
                hour.snowfall_cm > 0 or hour.temperature_c <= 0):
            return True
    return False


class PuddleRiskEngine:
    def __init__(
        self,
        ways_provider: PuddleWaysProvider,
        config: PuddleModelConfig = PuddleModelConfig(),
        clock: Callable[[], datetime] | None = None,
        *,
        weather_max_age_minutes: float = 45.0,
        history_required_hours: float = 12.0,
    ) -> None:
        self._provider = ways_provider
        self._config = config
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._weather_max_age_minutes = weather_max_age_minutes
        self._history_required_hours = history_required_hours

    # ------------------------------------------------------------------ api
    def analyze(
        self,
        legs: Sequence[PuddleLegInput],
        departure: datetime,
        weather: WeatherSeries | None,
        weather_reason: str | None = None,
        *,
        include_segments: bool = False,
    ) -> PuddleAnalysisResult:
        if not legs:
            raise PuddleAnalysisInputError("at least one leg is required")
        if departure.tzinfo is None or departure.tzinfo.utcoffset(departure) is None:
            raise PuddleAnalysisInputError(
                "departure must be timezone-aware (naive datetimes are rejected)")
        for leg in legs:
            if len(leg.geometry) < 2:
                raise PuddleAnalysisInputError("every leg needs at least 2 route points")
            if not math.isfinite(leg.walking_duration_seconds) \
                    or leg.walking_duration_seconds <= 0:
                raise PuddleAnalysisInputError("walking_duration_seconds must be positive")
            if not math.isfinite(leg.visit_duration_seconds_after_leg) \
                    or leg.visit_duration_seconds_after_leg < 0:
                raise PuddleAnalysisInputError(
                    "visit_duration_seconds_after_leg must be >= 0")
        if sum(len(leg.geometry) for leg in legs) > MAX_TOTAL_POINTS:
            raise PuddleAnalysisInputError("too many route points for one analysis")

        slices = _build_slices(legs)
        if not slices:
            raise PuddleAnalysisInputError("the walk has no walkable length")
        if len(slices) > MAX_SLICES:
            raise PuddleAnalysisInputError(
                f"walk is too long for one analysis ({len(slices)} slices)")

        period_end = departure + timedelta(seconds=max(slice_.t1 for slice_ in slices))

        if not self._provider.available():
            return self._unavailable(slices, departure, period_end,
                                     ["dataset_unavailable"])

        weather_problem = self._weather_problem(weather, weather_reason,
                                                departure, period_end)
        if weather_problem is not None:
            return self._unavailable(slices, departure, period_end,
                                     [weather_problem])

        assert weather is not None  # _weather_problem returned None
        evidence = self._match_phase(slices, weather, departure)

        pilot = self._provider.pilot_bbox()
        outside_all = all(not item.inside_pilot for item in evidence) if pilot else True
        # Only a CONFIRMED way with a KNOWN surface class and a COMPLETE
        # precipitation history contributes numbers: a matched way with an
        # unmapped surface stays unknown, and so does a slice whose lookback
        # window is missing hours — without the full history the decayed
        # contribution of earlier rain cannot be bounded, and a number there
        # could understate the risk (review C7.2.1 #2). Both stay unknown,
        # never low.
        matched = [item for item in evidence
                   if item.inside_pilot and item.facts is not None
                   and item.facts.surface_class is not None
                   and item.history_complete]
        reasons = self._route_reasons(evidence)

        if not matched:
            status = "outside_coverage" if outside_all else "unavailable"
            return self._unavailable(slices, departure, period_end, reasons,
                                     status=status, evidence=evidence,
                                     include_segments=include_segments)

        baseline = self._aggregate(evidence, self._config, matched)
        sensitivity = self._sensitivity(evidence, matched, baseline)
        for code in ("model_precipitation_history", "experimental_uncalibrated"):
            if code not in reasons:
                reasons.append(code)

        segments: tuple[ClassifiedSegment, ...] = ()
        if include_segments:
            segments = self._segments(evidence, self._config)

        diagnostics = _diagnostics(evidence)
        return PuddleAnalysisResult(
            status="partial" if baseline["unknown_meters"] > 1e-6 else "available",
            method=METHOD,
            method_version=METHOD_VERSION,
            evaluated_period_start=departure.isoformat(),
            evaluated_period_end=period_end.isoformat(),
            total_length_m=baseline["total"],
            known_meters=baseline["known"],
            low_meters=baseline["low"],
            moderate_meters=baseline["moderate"],
            high_meters=baseline["high"],
            unknown_meters=baseline["unknown_meters"],
            coverage_share=baseline["coverage_share"],
            risk_index=baseline["risk_index"],
            risk_level=baseline["risk_level"],
            confidence="low",
            reasons=tuple(reasons),
            limitations=self._limitations(baseline),
            sensitivity=sensitivity,
            segments=segments,
            diagnostics=diagnostics,
        )

    # ------------------------------------------------------------- internals
    def _weather_problem(self, weather: WeatherSeries | None,
                         weather_reason: str | None,
                         departure: datetime,
                         period_end: datetime) -> str | None:
        """The reason code that forbids a numeric estimate, or None."""
        if weather is None:
            return weather_reason or "weather_unavailable"
        now = self._clock().astimezone(timezone.utc)
        age = now - weather.retrieved_at.astimezone(timezone.utc)
        if age > timedelta(minutes=self._weather_max_age_minutes):
            return "weather_stale"
        if not weather.hours:
            return "weather_unavailable"
        first_start = weather.hours[0].ends_at - timedelta(hours=1)
        last_end = weather.hours[-1].ends_at
        if departure < first_start or period_end > last_end:
            return "weather_outside_window"
        check_start = departure - timedelta(hours=self._history_required_hours)
        if _freezing_or_snow(weather, check_start, period_end):
            return "weather_snow_or_freezing"
        return None

    def _match_phase(self, slices: Sequence[_Slice], weather: WeatherSeries,
                     departure: datetime) -> list[_SliceEvidence]:
        route_bbox = _bbox_of([point for slice_ in slices
                               for point in slice_.geometry])
        candidates = self._provider.ways_in_bbox(
            expand_bbox(route_bbox, MATCH_MAX_DISTANCE_M))
        pilot = self._provider.pilot_bbox()
        evidence: list[_SliceEvidence] = []
        for slice_ in slices:
            inside = bool(pilot) and point_in_bbox(slice_.mid_lat, slice_.mid_lon,
                                                   pilot)
            # The piece is evaluated at its crossing midpoint (documented).
            when = departure + timedelta(seconds=(slice_.t0 + slice_.t1) / 2.0)
            if not inside:
                evidence.append(_SliceEvidence(
                    slice_, False, None, False, False, (), 0.0, True, None))
                continue
            local = [way for way in candidates
                     if _bbox_overlap(way.bbox, slice_.bbox_expanded())]
            match = slice_match(sample_along(slice_.geometry), local)
            history, current = _bin_inputs(weather, when)
            complete = _history_complete(weather, when, self._history_required_hours)
            evidence.append(_SliceEvidence(
                slice_, True, match.facts, match.ambiguous,
                match.way is None and not match.ambiguous,
                tuple(history), current, complete,
                match.way.osm_id if match.way is not None else None))
        return evidence

    def _aggregate(self, evidence: Sequence[_SliceEvidence],
                   config: PuddleModelConfig,
                   matched: Sequence[_SliceEvidence]) -> dict:
        total = sum(item.slice.length_m for item in evidence)
        known = low = moderate = high = weighted = 0.0
        for item in matched:
            index = wetness_index(
                item.facts.surface_class, history=list(item.history),
                current_mm=item.current_mm, covered=item.facts.covered,
                config=config)
            length = item.slice.length_m
            known += length
            weighted += index * length
            state = classify(index, config)
            if state == "low":
                low += length
            elif state == "moderate":
                moderate += length
            else:
                high += length
        return {
            "total": total,
            "known": known,
            "low": low,
            "moderate": moderate,
            "high": high,
            "unknown_meters": total - known,
            "coverage_share": (known / total) if total > 0 else 0.0,
            "risk_index": (weighted / known) if known > 0 else None,
            "risk_level": classify(weighted / known, config) if known > 0 else "unknown",
        }

    def _sensitivity(self, evidence: Sequence[_SliceEvidence],
                     matched: Sequence[_SliceEvidence], baseline: dict) -> dict:
        if baseline["risk_index"] is None:
            return {}
        report: dict = {"baseline": baseline["risk_level"]}
        for name, changes in SENSITIVITY_VARIANTS:
            variant = self._aggregate(evidence, self._config.variant(**changes),
                                      matched)
            report[name] = variant["risk_level"]
            report[f"{name}_risk_index"] = round(variant["risk_index"], 4)
        report["note"] = (
            "Параметры экспериментальные и не калиброваны; расхождение вариантов "
            "показывает, насколько ответ зависит от выбора параметров, а не от данных.")
        return report

    def _segments(self, evidence: Sequence[_SliceEvidence],
                  config: PuddleModelConfig) -> tuple[ClassifiedSegment, ...]:
        segments = []
        for item in evidence:
            slice_ = item.slice
            index: float | None = None
            if not item.inside_pilot:
                state, reasons = "unknown", ("outside_pilot",)
            elif item.ambiguous:
                state, reasons = "unknown", ("ambiguous_osm_way",)
            elif item.facts is None:
                state, reasons = "unknown", ("unmatched_osm_way",)
            elif item.facts.surface_class is None:
                code = ("surface_not_supported" if item.facts.surface_tag
                        else "surface_unknown")
                state, reasons = "unknown", (code,)
            elif not item.history_complete:
                # No bounded precipitation history: no number for this slice.
                state, reasons = "unknown", ("weather_incomplete",)
            else:
                index = wetness_index(item.facts.surface_class,
                                      history=list(item.history),
                                      current_mm=item.current_mm,
                                      covered=item.facts.covered, config=config)
                state = classify(index, config)
                reasons = (("covered_not_waterproof",) if item.facts.covered else ())
            segments.append(ClassifiedSegment(
                leg_index=slice_.leg_index, geometry=slice_.geometry,
                length_m=round(slice_.length_m, 1),
                starts_at_seconds=round(slice_.t0, 1),
                ends_at_seconds=round(slice_.t1, 1),
                state=state,
                risk_index=None if index is None else round(index, 4),
                osm_id=item.osm_id, reasons=reasons))
        return tuple(segments)

    def _route_reasons(self, evidence: Sequence[_SliceEvidence]) -> list[str]:
        reasons: list[str] = []
        for item in evidence:
            if not item.inside_pilot:
                code = "outside_pilot"
            elif item.ambiguous:
                code = "ambiguous_osm_way"
            elif item.facts is None:
                code = "unmatched_osm_way"
            elif item.facts.surface_class is None:
                code = ("surface_not_supported" if item.facts.surface_tag
                        else "surface_unknown")
            elif not item.history_complete:
                code = "weather_incomplete"
            else:
                code = "covered_not_waterproof" if item.facts.covered else None
            if code and code not in reasons:
                reasons.append(code)
        return reasons

    def _limitations(self, baseline: dict) -> tuple[str, ...]:
        limitations = list(BASE_LIMITATIONS)
        if baseline["unknown_meters"] > 1e-6:
            limitations.append(
                "Для неизвестных участков оценка не даётся: неизвестность не "
                "считается низким риском.")
        return tuple(limitations)

    def _unavailable(self, slices: Sequence[_Slice],
                     departure: datetime, period_end: datetime,
                     reasons: Sequence[str], *,
                     status: str = "unavailable",
                     evidence: Sequence[_SliceEvidence] | None = None,
                     include_segments: bool = False) -> PuddleAnalysisResult:
        total = sum(slice_.length_m for slice_ in slices)
        segments: tuple[ClassifiedSegment, ...] = ()
        if include_segments and evidence is not None:
            segments = self._segments(evidence, self._config)
        return PuddleAnalysisResult(
            status=status,
            method=METHOD,
            method_version=METHOD_VERSION,
            evaluated_period_start=departure.isoformat(),
            evaluated_period_end=period_end.isoformat(),
            total_length_m=total,
            known_meters=0.0,
            low_meters=0.0,
            moderate_meters=0.0,
            high_meters=0.0,
            unknown_meters=total,
            coverage_share=0.0,
            risk_index=None,
            risk_level="unknown",
            confidence="unavailable",
            reasons=tuple(reasons),
            limitations=BASE_LIMITATIONS,
            sensitivity={},
            segments=segments,
            diagnostics=_diagnostics(evidence) if evidence is not None else {},
        )

    # ------------------------------------------------------------ provenance
    def provider_provenance(self) -> dict:
        return self._provider.provenance()

    def dataset_version(self) -> str | None:
        return self._provider.dataset_version()
