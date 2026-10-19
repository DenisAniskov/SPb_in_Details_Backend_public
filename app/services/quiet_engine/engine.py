"""C11 relative road-noise exposure proxy for an EXISTING pedestrian walk.

WHAT IT DOES: it measures how close the real MapKit geometry runs to mapped
motor roads, weights each road by functional class and distance, and reports a
bounded dimensionless index plus coverage. It never chooses or changes a route.

WHAT IT REFUSES TO DO: report dB, a measured noise level, a health/sanitary
verdict, an hourly traffic forecast, or zero exposure for ground the pilot does
not cover. Ground outside the pilot is ``unknown``; a route with no significant
road within 120 m inside the pilot is a real (valid) zero of this proxy.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

from app.services.quiet_engine.model import (
    LOW_EXPOSURE_THRESHOLD,
    MAJOR_ADJACENT_THRESHOLD,
    MAX_INFLUENCE_METERS,
    MODEL_VERSION,
    SAMPLE_STEP_METERS,
)
from app.services.quiet_engine.source import QuietDataset
from app.services.shade_engine.geo import haversine_distance_m

STATUS_AVAILABLE = "available"
STATUS_PARTIAL = "partial"
STATUS_UNAVAILABLE = "unavailable"
STATUS_OUTSIDE = "outside_coverage"


class QuietAnalysisInputError(ValueError):
    pass


@dataclass(frozen=True)
class QuietLegInput:
    geometry: tuple[tuple[float, float], ...]  # (lat, lon)


@dataclass(frozen=True)
class QuietLegResult:
    leg_index: int
    length_meters: float
    known_meters: float
    unknown_meters: float
    road_noise_exposure_index: float | None
    major_road_adjacent_meters: float | None
    low_exposure_meters: float | None


@dataclass(frozen=True)
class QuietAnalysisResult:
    status: str
    route_length_m: float
    known_m: float
    unknown_m: float
    route_noise_exposure_index: float | None
    low_exposure_share: float | None
    major_road_adjacent_share: float | None
    mean_nearest_road_distance_m: float | None
    max_road_class_nearby: str | None
    legs: tuple[QuietLegResult, ...]
    reasons: tuple[str, ...]
    diagnostics: dict[str, int | float | str]


def _interpolate(a: tuple[float, float], b: tuple[float, float],
                 fraction: float) -> tuple[float, float]:
    return (a[0] + (b[0] - a[0]) * fraction,
            a[1] + (b[1] - a[1]) * fraction)


class QuietEngine:
    def __init__(self, dataset: QuietDataset) -> None:
        self._dataset = dataset

    def analyze(self, legs: list[QuietLegInput]) -> QuietAnalysisResult:
        if not legs:
            raise QuietAnalysisInputError("route has no legs")
        total_length = 0.0
        for leg in legs:
            if len(leg.geometry) < 2:
                raise QuietAnalysisInputError("each leg needs at least two points")
        for leg in legs:
            for lat, lon in leg.geometry:
                if not (math.isfinite(lat) and math.isfinite(lon)
                        and -90 <= lat <= 90 and -180 <= lon <= 180):
                    raise QuietAnalysisInputError("invalid route coordinate")
            total_length += sum(
                haversine_distance_m(a[0], a[1], b[0], b[1])
                for a, b in zip(leg.geometry, leg.geometry[1:]))
        if not math.isfinite(total_length) or total_length <= 0:
            raise QuietAnalysisInputError("route has zero length")

        if not self._dataset.available:
            return QuietAnalysisResult(
                status=STATUS_UNAVAILABLE, route_length_m=total_length,
                known_m=0.0, unknown_m=total_length,
                route_noise_exposure_index=None, low_exposure_share=None,
                major_road_adjacent_share=None,
                mean_nearest_road_distance_m=None,
                max_road_class_nearby=None, legs=(),
                reasons=(self._dataset.reason or "quiet_dataset_unavailable",),
                diagnostics={"road_count": 0, "known_chunks": 0,
                             "unknown_chunks": 0,
                             "sample_step_meters": SAMPLE_STEP_METERS,
                             "max_influence_meters": MAX_INFLUENCE_METERS,
                             "projection": "local_equirectangular"})

        known = 0.0
        unknown = 0.0
        weighted = 0.0
        low = 0.0
        major = 0.0
        distance_sum = 0.0
        distance_count = 0
        max_weight = 0.0
        max_class: str | None = None
        known_chunks = 0
        unknown_chunks = 0
        leg_results: list[QuietLegResult] = []

        for index, leg in enumerate(legs):
            leg_length = leg_known = leg_unknown = 0.0
            leg_weighted = leg_low = leg_major = 0.0
            for start, end in zip(leg.geometry, leg.geometry[1:]):
                segment_length = haversine_distance_m(
                    start[0], start[1], end[0], end[1])
                if segment_length <= 0 or not math.isfinite(segment_length):
                    continue
                chunks = max(1, math.ceil(segment_length / SAMPLE_STEP_METERS))
                for chunk in range(chunks):
                    first = _interpolate(start, end, chunk / chunks)
                    second = _interpolate(start, end, (chunk + 1) / chunks)
                    chunk_length = haversine_distance_m(
                        first[0], first[1], second[0], second[1])
                    if not math.isfinite(chunk_length) or chunk_length <= 0:
                        continue
                    leg_length += chunk_length
                    # Conservative boundary rule: a chunk with either endpoint
                    # outside the pilot is unknown. The value is never guessed
                    # from the inside part of the same chunk.
                    if not (self._dataset.contains(first[1], first[0])
                            and self._dataset.contains(second[1], second[0])):
                        leg_unknown += chunk_length
                        unknown_chunks += 1
                        continue
                    known_chunks += 1
                    leg_known += chunk_length
                    mid_lat = (first[0] + second[0]) / 2.0
                    mid_lon = (first[1] + second[1]) / 2.0
                    nearest = self._dataset.nearest(mid_lon, mid_lat)
                    if nearest is not None:
                        distance_sum += nearest[1]
                        distance_count += 1
                    exposure = 0.0
                    for road, distance in self._dataset.nearby(
                            mid_lon, mid_lat, MAX_INFLUENCE_METERS):
                        decay = (1.0 - distance / MAX_INFLUENCE_METERS) ** 2
                        exposure = max(exposure, min(1.0, road.weight * decay))
                        if road.weight > max_weight:
                            max_weight = road.weight
                            max_class = road.highway
                    leg_weighted += exposure * chunk_length
                    if exposure < LOW_EXPOSURE_THRESHOLD:
                        leg_low += chunk_length
                    if exposure >= MAJOR_ADJACENT_THRESHOLD:
                        leg_major += chunk_length
            if leg_known > 0:
                leg_results.append(QuietLegResult(
                    leg_index=index, length_meters=leg_length,
                    known_meters=leg_known, unknown_meters=leg_unknown,
                    road_noise_exposure_index=leg_weighted / leg_known,
                    major_road_adjacent_meters=leg_major,
                    low_exposure_meters=leg_low,
                ))
            else:
                leg_results.append(QuietLegResult(
                    leg_index=index, length_meters=leg_length,
                    known_meters=0.0, unknown_meters=leg_unknown,
                    road_noise_exposure_index=None,
                    major_road_adjacent_meters=None, low_exposure_meters=None,
                ))
            known += leg_known
            unknown += leg_unknown
            weighted += leg_weighted
            low += leg_low
            major += leg_major

        if known <= 0:
            status = STATUS_OUTSIDE
            reasons = ("route_outside_pilot",)
        elif unknown > 0:
            status = STATUS_PARTIAL
            reasons = ("route_partially_outside_pilot",)
        else:
            status = STATUS_AVAILABLE
            reasons = ()

        index_value = weighted / known if known > 0 else None
        low_share = low / known if known > 0 else None
        major_share = major / known if known > 0 else None
        mean_distance = (distance_sum / distance_count
                         if distance_count > 0 else None)
        return QuietAnalysisResult(
            status=status, route_length_m=total_length, known_m=known,
            unknown_m=unknown,
            route_noise_exposure_index=index_value,
            low_exposure_share=low_share,
            major_road_adjacent_share=major_share,
            mean_nearest_road_distance_m=mean_distance,
            max_road_class_nearby=max_class, legs=tuple(leg_results),
            reasons=reasons,
            diagnostics={
                "road_count": len(self._dataset.roads),
                "known_chunks": known_chunks,
                "unknown_chunks": unknown_chunks,
                "sample_step_meters": SAMPLE_STEP_METERS,
                "max_influence_meters": MAX_INFLUENCE_METERS,
                "projection": "local_equirectangular",
                "model_version": MODEL_VERSION,
            },
        )
