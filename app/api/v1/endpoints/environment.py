"""Environment endpoints — Phase C7.1: time-aware shade analysis (§11).

WHAT THIS ENDPOINT DOES: it analyses an EXISTING walk (real MapKit pedestrian
geometry, native walking durations, POI visit durations) for shade at the
departure time, using the read-only OSM-derived pilot building dataset. It
never generates, chooses or silently replaces a route.

WHAT IT REFUSES TO DO, ON PURPOSE:

* report an invented shade percentage: without a usable dataset (or with an
  empty building answer) it returns ``status: "unavailable"`` and a reason;
* report unknown ground as sun: ground outside the pilot coverage, or inside
  the shadow envelope of buildings whose height is unknown, is returned as
  ``unknown_meters`` (§9);
* block the event loop: the GEOS work runs in a worker thread under a
  timeout and a concurrency limit;
* accept client-supplied building geometry: buildings come only from the
  internal dataset (§26 — no public upload of geometry).
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.config import settings
from app.schemas.shade import (
    ClassifiedSegmentOut,
    CoverageMetricsOut,
    EvaluatedPeriodOut,
    HeightProvenanceMetricsOut,
    ShadeAnalyzeRequest,
    ShadeAnalyzeResponse,
    SolarSummaryOut,
)
from app.services.shade_engine.geo import haversine_distance_m
from app.services.shade_engine.time_aware import (
    ShadeAnalysisInputError,
    TimeAwareShadeEngine,
    WalkLegInput,
)
from app.services.shade_engine.weather import WeatherContext, shade_estimate_relevance
from app.services.shade_engine.wiring import default_building_provider

router = APIRouter()

# Coverage region: generous bounding box of the Saint Petersburg metropolitan
# area. Inside it, the PILOT DATASET covers only the mapped zones; the analysis
# reports the uncovered part honestly instead of pretending to know it.
SPB_COVERAGE_BBOX = (59.45, 29.05, 60.45, 31.15)  # (min_lat, min_lon, max_lat, max_lon)

#: §11.3 document-level limits (in addition to the Pydantic field bounds).
MAX_ROUTE_LENGTH_METERS = 25_000.0
MAX_TOTAL_POINTS = 5000

_analysis_semaphore = asyncio.Semaphore(settings.SHADE_MAX_CONCURRENT_ANALYSES)


def get_shade_engine() -> TimeAwareShadeEngine:
    """Production wiring: the read-only pilot dataset (never synthetic)."""
    return TimeAwareShadeEngine(building_provider=default_building_provider())


def _route_length_meters(legs: list[WalkLegInput]) -> float:
    total = 0.0
    for leg in legs:
        points = leg.geometry
        total += sum(haversine_distance_m(a[0], a[1], b[0], b[1])
                     for a, b in zip(points, points[1:]))
    return total


@router.post(
    "/shade/analyze",
    response_model=ShadeAnalyzeResponse,
    summary="Оценка тени существующей прогулки с учётом времени (не генерирует маршруты)",
)
async def analyze_walk_shade(
    payload: ShadeAnalyzeRequest,
    engine: TimeAwareShadeEngine = Depends(get_shade_engine),
) -> ShadeAnalyzeResponse:
    """Оценка затенённости УЖЕ СУЩЕСТВУЮЩЕЙ прогулки от момента выхода.

    Результат — оценка по доступной геометрии зданий (см. limitations), а не
    измерение. Участки, для которых данных недостаточно, возвращаются как
    «неизвестно», а не как солнце. Если проверенных данных нет — status
    unavailable без фиктивных процентов.
    """
    legs = [WalkLegInput(
        geometry=tuple((p.latitude, p.longitude) for p in leg.geometry),
        walking_duration_seconds=leg.walking_duration_seconds,
        visit_duration_seconds_after_leg=leg.visit_duration_seconds_after_leg,
    ) for leg in payload.legs]

    total_points = sum(len(leg.geometry) for leg in legs)
    if total_points > MAX_TOTAL_POINTS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Слишком много точек маршрута для одного анализа (>{MAX_TOTAL_POINTS}).")
    for lat, lon in (point for leg in legs for point in leg.geometry):
        if not (SPB_COVERAGE_BBOX[0] <= lat <= SPB_COVERAGE_BBOX[2]
                and SPB_COVERAGE_BBOX[1] <= lon <= SPB_COVERAGE_BBOX[3]):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=("Маршрут вне зоны Shade Engine (Санкт-Петербург и окрестности, "
                        f"широта {SPB_COVERAGE_BBOX[0]}..{SPB_COVERAGE_BBOX[2]}, "
                        f"долгота {SPB_COVERAGE_BBOX[1]}..{SPB_COVERAGE_BBOX[3]})."))
    route_length = _route_length_meters(legs)
    if route_length <= 0.0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Маршрут вырожден: нулевая длина, оценка доли тени невозможна.")
    if route_length > MAX_ROUTE_LENGTH_METERS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Маршрут длиннее лимита анализа ({MAX_ROUTE_LENGTH_METERS:.0f} м).")

    try:
        async with _analysis_semaphore:
            async with asyncio.timeout(settings.SHADE_ANALYSIS_TIMEOUT_SECONDS):
                result = await asyncio.to_thread(
                    engine.analyze, legs, payload.departure_at,
                    include_slices=payload.include_segments)
    except ShadeAnalysisInputError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=str(exc)) from None
    except TimeoutError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Расчёт тени занял слишком много времени и был остановлен.") from None

    weather = None
    if payload.weather is not None:
        weather = WeatherContext(
            cloud_cover_fraction=payload.weather.cloud_cover_fraction,
            source=payload.weather.source)
    sun_above = bool(result.solar and result.solar.max_elevation_deg > 0)
    relevance, relevance_note = shade_estimate_relevance(weather, sun_above)

    def r1(value: float | None) -> float | None:
        return None if value is None else round(value, 1)

    segments_out = None
    if payload.include_segments and result.slices:
        segments_out = [
            ClassifiedSegmentOut(
                leg_index=slice_.leg_index,
                geometry=[{"latitude": round(lat, 7), "longitude": round(lon, 7)}
                          for lat, lon in slice_.geometry],
                length_meters=round(slice_.length_m, 1),
                shaded_meters=round(slice_.shaded_m, 1),
                unknown_meters=round(slice_.unknown_m, 1),
                state=slice_.state,
                starts_at_seconds=round(slice_.starts_at_seconds, 1),
                ends_at_seconds=round(slice_.ends_at_seconds, 1),
                shaded_spans=[(round(a, 1), round(b, 1))
                              for a, b in slice_.shaded_spans_m],
                unknown_spans=[(round(a, 1), round(b, 1))
                               for a, b in slice_.unknown_spans_m])
            for slice_ in result.slices]

    return ShadeAnalyzeResponse(
        status=result.status,
        method=result.method,
        method_version=result.method_version,
        estimate=True,
        evaluated_period=EvaluatedPeriodOut(
            start=result.evaluated_period_start,
            end=result.evaluated_period_end,
            timezone=str(payload.departure_at.tzinfo)),
        dataset_version=engine.dataset_version(),
        dataset_provenance=engine.provider_provenance(),
        solar_summary=(SolarSummaryOut(**result.solar.to_map()) if result.solar else None),
        route_length_meters=r1(route_length) or 0.0,
        estimated_shaded_meters=r1(result.shaded_length_m),
        estimated_exposed_meters=r1(result.exposed_length_m),
        unknown_meters=r1(result.unknown_length_m),
        shade_fraction=result.shade_fraction,
        confidence=result.confidence,
        confidence_reason=None,
        coverage_metrics=CoverageMetricsOut(**{
            "dataset_zones_touched": result.coverage.get("dataset_zones_touched", []),
            "outside_coverage_meters": result.coverage.get("outside_coverage_meters", 0.0),
            "coverage_share": result.coverage.get("coverage_share", 0.0),
        }),
        height_provenance_metrics=(HeightProvenanceMetricsOut(**result.height_provenance)
                                   if result.height_provenance
                                   else HeightProvenanceMetricsOut()),
        weather_relevance=relevance.value,
        weather_relevance_explanation=relevance_note,
        limitations=list(result.limitations),
        classified_segments=segments_out,
        reason=result.reason,
    )
