"""C7.3 pollen-exposure endpoint — POST /environment/pollen/analyze.

WHAT THIS ENDPOINT DOES: it reports modelled pollen concentrations for an
EXISTING walk (real MapKit pedestrian geometry, native walking durations, POI
visit durations) from a source the operator must enable explicitly. It never
generates, chooses or silently replaces a route, and it issues zero routing
requests upstream.

WHAT IT REFUSES TO DO, ON PURPOSE:

* refuse numbers without usable data: no configured source, an unreachable or
  malformed response, a stale series or a walk outside the source's interval
  all return ``status: "unavailable"`` with a reason — never invented,
  zero-filled or extrapolated concentrations;
* turn ``unknown`` into ``low``: there is no "low" here at all. Hours past the
  four-day horizon, and legs outside the fetched CAMS cell, are reported as
  unknown metres rather than as clean air;
* claim street-level resolution: the source is one ~11 km model cell, so every
  leg of a walk is described by the same cell and only the hour of traversal
  may differ;
* forward the user's geometry upstream: only one fixed pilot centroid is ever
  requested;
* offer medical advice: no thresholds, no danger categories, no "allergy
  risk" — concentrations, coverage and their units only.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.config import settings
from app.schemas.pollen import (
    PollenAnalyzeRequest,
    PollenAnalyzeResponse,
    PollenLegOut,
    PollenPeriodOut,
    PollenSpeciesOut,
)
from app.services.pollen_engine.engine import (
    PollenAnalysisInputError,
    PollenEngine,
    PollenLegInput,
    SpeciesResult,
)
from app.services.pollen_engine.source import (
    PollenProvider,
    PollenUnavailable,
    SPECIES,
    UNIT,
    unavailable_provenance,
)
from app.services.pollen_engine.wiring import default_pollen_provider
from app.services.shade_engine.geo import haversine_distance_m

router = APIRouter()
logger = logging.getLogger("smart_navigator.pollen")

#: Same outer coverage region as the C7.1/C7.2 endpoints (min_lat, min_lon,
#: max_lat, max_lon): the feature is offered only inside the SPb metropolitan
#: area. Inside it, the SOURCE's own cell decides coverage; legs outside the
#: fetched cell are reported as unknown, never as clean air.
SPB_COVERAGE_BBOX = (59.45, 29.05, 60.45, 31.15)

#: Same document limit as the shade and puddle analysis endpoints.
MAX_ROUTE_LENGTH_METERS = 25_000.0

_analysis_semaphore = asyncio.Semaphore(settings.POLLEN_MAX_CONCURRENT_ANALYSES)


def get_pollen_engine() -> PollenEngine:
    """Production wiring: the C7.3 engine, no client-supplied coefficients."""
    return PollenEngine()


def get_pollen_source() -> PollenProvider:
    """Disabled unless the operator opted in (see POLLEN_SOURCE_ENABLED)."""
    return default_pollen_provider()


def _route_length_meters(legs: list[PollenLegInput]) -> float:
    total = 0.0
    for leg in legs:
        points = leg.geometry
        total += sum(haversine_distance_m(a[0], a[1], b[0], b[1])
                     for a, b in zip(points, points[1:]))
    return total


def _species_out(item: SpeciesResult) -> PollenSpeciesOut:
    return PollenSpeciesOut(
        code=item.code, name=item.name_ru, unit=item.unit, status=item.status,
        known_seconds=item.known_seconds, total_seconds=item.total_seconds,
        known_share=item.known_share, mean_concentration=item.mean_grains_m3,
        peak_concentration=item.peak_grains_m3, peak_at=item.peak_at,
        exposure_grains_hours_per_m3=item.exposure_grains_hours_m3)


@router.post(
    "/pollen/analyze",
    response_model=PollenAnalyzeResponse,
    summary="Концентрации пыльцы на существующей прогулке (не генерирует маршруты)",
)
async def analyze_walk_pollen(
    payload: PollenAnalyzeRequest,
    engine: PollenEngine = Depends(get_pollen_engine),
    source: PollenProvider = Depends(get_pollen_source),
) -> PollenAnalyzeResponse:
    """Концентрации пыльцы (модель CAMS) на УЖЕ СУЩЕСТВУЮЩЕЙ прогулке.

    Результат — модельная оценка по отдельным видам пыльцы с честным
    покрытием: часы за горизонтом прогноза и этапы вне ячейки источника
    остаются «неизвестно» и не считаются чистыми. Порогов опасности и
    медицинских выводов здесь нет. Если проверенных данных нет — status
    unavailable без фиктивных чисел.
    """
    legs = [PollenLegInput(
        geometry=tuple((point.latitude, point.longitude)
                       for point in leg.geometry),
        walking_duration_seconds=leg.walking_duration_seconds,
        visit_duration_seconds_after_leg=leg.visit_duration_seconds_after_leg,
    ) for leg in payload.legs]

    for lat, lon in (point for leg in legs for point in leg.geometry):
        if not (SPB_COVERAGE_BBOX[0] <= lat <= SPB_COVERAGE_BBOX[2]
                and SPB_COVERAGE_BBOX[1] <= lon <= SPB_COVERAGE_BBOX[3]):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=("Маршрут вне зоны оценки пыльцы (Санкт-Петербург и "
                        f"окрестности, широта {SPB_COVERAGE_BBOX[0]}..{SPB_COVERAGE_BBOX[2]}, "
                        f"долгота {SPB_COVERAGE_BBOX[1]}..{SPB_COVERAGE_BBOX[3]})."))
    route_length = _route_length_meters(legs)
    if route_length <= 0.0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Маршрут вырожден: нулевая длина, оценка невозможна.")
    if route_length > MAX_ROUTE_LENGTH_METERS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Маршрут длиннее лимита анализа ({MAX_ROUTE_LENGTH_METERS:.0f} м).")

    series = None
    source_reason = None
    try:
        async with asyncio.timeout(settings.POLLEN_SOURCE_TIMEOUT_SECONDS):
            series = await source.fetch()
    except PollenUnavailable as exc:
        source_reason = exc.reason
    except TimeoutError:
        source_reason = "pollen_unavailable"
    except Exception as exc:  # optional dependency: never break the walk
        logger.warning("Pollen source failure (%s)", type(exc).__name__)
        source_reason = "pollen_unavailable"

    try:
        async with _analysis_semaphore:
            async with asyncio.timeout(settings.POLLEN_ANALYSIS_TIMEOUT_SECONDS):
                result = await asyncio.to_thread(
                    engine.analyze, legs, payload.departure_at, series,
                    source_reason, include_legs=payload.include_legs)
    except PollenAnalysisInputError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=str(exc)) from None
    except TimeoutError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Расчёт пыльцевой нагрузки занял слишком много времени "
                   "и был остановлен.") from None

    # Lengths are rounded to 0.1 m EXACTLY so the sums the client verifies
    # (known+unknown=total, coverage=known/total) stay consistent: the unknown
    # bucket absorbs the rounding remainder instead of drifting by a tenth.
    total_r = round(result.total_length_m, 1)
    known_r = round(result.known_meters, 1)
    unknown_r = round(total_r - known_r, 1)
    if unknown_r < 0.0:  # defensive: never emit a negative length
        unknown_r = 0.0
        known_r = total_r
    coverage_r = (known_r / total_r) if total_r > 0 else 0.0

    legs_out = None
    if payload.include_legs and result.legs:
        legs_out = [
            PollenLegOut(
                leg_index=leg.leg_index, length_meters=leg.length_m,
                known_meters=leg.known_meters,
                unknown_meters=leg.unknown_meters,
                starts_at_seconds=leg.starts_at_seconds,
                ends_at_seconds=leg.ends_at_seconds,
                inside_source_cell=leg.inside_source_cell,
                species=[_species_out(item) for item in leg.species])
            for leg in result.legs]

    return PollenAnalyzeResponse(
        status=result.status,
        method=result.method,
        method_version=result.method_version,
        estimate=True,
        confidence=result.confidence,
        evaluated_period=PollenPeriodOut(
            start=result.evaluated_period_start,
            end=result.evaluated_period_end,
            timezone=str(payload.departure_at.tzinfo)),
        route_length_meters=total_r,
        known_meters=known_r,
        unknown_meters=unknown_r,
        coverage_share=coverage_r,
        walking_duration_seconds=round(result.walking_seconds, 1),
        elapsed_duration_seconds=round(result.elapsed_seconds, 1),
        concentration_unit=UNIT,
        species=[_species_out(item) for item in result.species],
        legs=legs_out,
        reasons=list(result.reasons),
        limitations=list(result.limitations),
        assumptions=list(result.assumptions),
        source_provenance=(
            series.provenance() if series is not None
            else unavailable_provenance(source_reason or "pollen_unavailable")),
        diagnostics=dict(result.diagnostics),
    )
