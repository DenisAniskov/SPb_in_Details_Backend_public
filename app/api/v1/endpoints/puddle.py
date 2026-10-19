"""C7.2 puddle-risk endpoint — POST /environment/puddle/analyze.

WHAT THIS ENDPOINT DOES: it assesses an EXISTING walk (real MapKit pedestrian
geometry, native walking durations, POI visit durations) for the RELATIVE
risk of wet surface after rain, using the read-only OSM pilot dataset and a
weather provider that the operator must enable explicitly. It never
generates, chooses or silently replaces a route, and it issues zero routing
requests upstream.

WHAT IT REFUSES TO DO, ON PURPOSE:

* refuse a numeric estimate without usable data: no pilot dataset, no
  configured/uncached weather, stale weather, out-of-window departure or
  freezing conditions return ``status: "unavailable"`` with a reason —
  never invented numbers (ТЗ C7.2 §4);
* turn ``unknown`` into ``low``: unmatched, ambiguous or unmapped slices and
  slices outside the pilot box are reported as ``unknown_meters``;
* block the event loop: weather fetch and the CPU-bound analysis run under
  their own timeouts and a concurrency limit, and a failure here never
  touches ordinary navigation;
* forward the user's geometry to the weather provider: only one fixed pilot
  centroid is ever requested upstream.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.config import settings
from app.schemas.puddle import (
    PuddleAnalyzeRequest,
    PuddleAnalyzeResponse,
    PuddlePeriodOut,
    PuddleSegmentOut,
)
from app.services.puddle_engine.engine import (
    PuddleAnalysisInputError,
    PuddleLegInput,
    PuddleRiskEngine,
)
from app.services.puddle_engine.weather import (
    WeatherProvider,
    WeatherUnavailable,
    unavailable_provenance,
)
from app.services.puddle_engine.wiring import (
    default_puddle_weather_provider,
    default_puddle_ways_provider,
)
from app.services.shade_engine.geo import haversine_distance_m

router = APIRouter()
logger = logging.getLogger("smart_navigator.puddle")

#: Same coverage region as the C7.1 Shade Engine (min_lat, min_lon, max_lat,
#: max_lon): the feature is only offered inside the SPb metropolitan area.
#: Inside it, the PILOT dataset covers only its own box; slices outside the
#: box are honestly reported as unknown / outside coverage, never as dry.
SPB_COVERAGE_BBOX = (59.45, 29.05, 60.45, 31.15)

#: §document limits, identical to the shade analysis endpoint.
MAX_ROUTE_LENGTH_METERS = 25_000.0

_analysis_semaphore = asyncio.Semaphore(settings.PUDDLE_MAX_CONCURRENT_ANALYSES)


def get_puddle_engine() -> PuddleRiskEngine:
    """Production wiring: the read-only pilot dataset (never synthetic)."""
    return PuddleRiskEngine(ways_provider=default_puddle_ways_provider())


def get_puddle_weather_provider() -> WeatherProvider:
    """Disabled unless the operator opted in (see PUDDLE_WEATHER_ENABLED)."""
    return default_puddle_weather_provider()


def _route_length_meters(legs: list[PuddleLegInput]) -> float:
    total = 0.0
    for leg in legs:
        points = leg.geometry
        total += sum(haversine_distance_m(a[0], a[1], b[0], b[1])
                     for a, b in zip(points, points[1:]))
    return total


@router.post(
    "/puddle/analyze",
    response_model=PuddleAnalyzeResponse,
    summary="Оценка риска мокрых участков существующей прогулки (не генерирует маршруты)",
)
async def analyze_walk_puddles(
    payload: PuddleAnalyzeRequest,
    engine: PuddleRiskEngine = Depends(get_puddle_engine),
    weather_provider: WeatherProvider = Depends(get_puddle_weather_provider),
) -> PuddleAnalyzeResponse:
    """Оценка относительного риска мокрого покрытия УЖЕ СУЩЕСТВУЮЩЕЙ прогулки.

    Результат — модельная оценка по подтверждённым тегам OSM и модельным
    осадкам (см. limitations), а не вероятность лужи. Неизвестные участки
    остаются «неизвестно» и не считаются низким риском. Если проверенных
    данных нет — status unavailable без фиктивных чисел.
    """
    legs = [PuddleLegInput(
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
                detail=("Маршрут вне зоны анализа мокрых участков (Санкт-Петербург и "
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

    weather_series = None
    weather_reason = None
    try:
        async with asyncio.timeout(settings.PUDDLE_WEATHER_TIMEOUT_SECONDS):
            weather_series = await weather_provider.fetch()
    except WeatherUnavailable as exc:
        weather_reason = exc.reason
    except TimeoutError:
        weather_reason = "weather_unavailable"
    except Exception as exc:  # optional dependency: never break the walk
        logger.warning("Weather provider failure (%s)", type(exc).__name__)
        weather_reason = "weather_unavailable"

    try:
        async with _analysis_semaphore:
            async with asyncio.timeout(settings.PUDDLE_ANALYSIS_TIMEOUT_SECONDS):
                result = await asyncio.to_thread(
                    engine.analyze, legs, payload.departure_at, weather_series,
                    weather_reason, include_segments=payload.include_segments)
    except PuddleAnalysisInputError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=str(exc)) from None
    except TimeoutError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Расчёт мокрых участков занял слишком много времени и был остановлен.") from None

    # The response lengths are rounded to 0.1 m EXACTLY so that the sums the
    # client verifies (known+unknown=total, low+moderate+high=known,
    # coverage=known/total) stay consistent after rounding: the last bucket
    # absorbs the rounding remainder instead of drifting by a tenth.
    total_r = round(result.total_length_m, 1)
    known_r = round(result.known_meters, 1)
    unknown_r = round(total_r - known_r, 1)
    low_r = round(result.low_meters, 1)
    moderate_r = round(result.moderate_meters, 1)
    high_r = round(known_r - low_r - moderate_r, 1)
    if high_r < 0.0:
        moderate_r = round(max(0.0, known_r - low_r), 1)
        high_r = round(known_r - low_r - moderate_r, 1)
        if high_r < 0.0:  # defensive: never emit a negative length
            high_r = 0.0
            moderate_r = round(max(0.0, known_r - low_r - high_r), 1)
    coverage_r = (known_r / total_r) if total_r > 0 else 0.0

    segments_out = None
    if payload.include_segments and result.segments:
        segments_out = [
            PuddleSegmentOut(
                leg_index=segment.leg_index,
                geometry=[{"latitude": round(lat, 7), "longitude": round(lon, 7)}
                          for lat, lon in segment.geometry],
                length_meters=segment.length_m,
                state=segment.state,
                risk_index=segment.risk_index,
                starts_at_seconds=segment.starts_at_seconds,
                ends_at_seconds=segment.ends_at_seconds,
                reasons=list(segment.reasons))
            for segment in result.segments]

    return PuddleAnalyzeResponse(
        status=result.status,
        method=result.method,
        method_version=result.method_version,
        estimate=True,
        risk_level=result.risk_level,
        risk_index=(None if result.risk_index is None
                    else round(result.risk_index, 4)),
        route_length_meters=total_r,
        known_meters=known_r,
        unknown_meters=unknown_r,
        low_meters=low_r,
        moderate_meters=moderate_r,
        high_meters=high_r,
        coverage_share=coverage_r,
        confidence=result.confidence,
        evaluated_period=PuddlePeriodOut(
            start=result.evaluated_period_start,
            end=result.evaluated_period_end,
            timezone=str(payload.departure_at.tzinfo)),
        dataset_version=engine.dataset_version(),
        dataset_provenance=engine.provider_provenance(),
        weather_provenance=(
            weather_series.provenance() if weather_series is not None
            else unavailable_provenance(weather_reason or "weather_unavailable")),
        reasons=list(result.reasons),
        limitations=list(result.limitations),
        classified_segments=segments_out,
        sensitivity=result.sensitivity,
    )
