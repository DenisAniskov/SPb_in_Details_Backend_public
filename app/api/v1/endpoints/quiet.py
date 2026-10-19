"""C11 quiet-road endpoint — POST /environment/quiet/analyze.

Read-only analysis of an EXISTING walk. It never builds, reorders or changes a
route, and it issues zero MapKit requests. The dataset is a pinned OSM snapshot
inside the image; no runtime download and no client-supplied coefficients.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import math

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.config import settings
from app.schemas.quiet import (
    QuietAnalyzeRequest,
    QuietAnalyzeResponse,
    QuietLegOut,
)
from app.services.quiet_engine.engine import (
    QuietAnalysisInputError,
    QuietEngine,
    QuietLegInput,
)
from app.services.quiet_engine.model import ASSUMPTIONS, LIMITATIONS
from app.services.quiet_engine.source import QuietDataset
from app.services.quiet_engine.wiring import default_quiet_dataset
from app.services.shade_engine.geo import haversine_distance_m

router = APIRouter()
logger = logging.getLogger("smart_navigator.quiet")

#: The feature is offered only inside the SPb metropolitan area. Inside this
#: outer box, the PILOT's own bbox decides known vs unknown.
SPB_COVERAGE_BBOX = (59.45, 29.05, 60.45, 31.15)  # min_lat, min_lon, max_lat, max_lon
MAX_ROUTE_LENGTH_METERS = 25_000.0
MAX_TOTAL_POINTS = 5000

_analysis_semaphore = asyncio.Semaphore(settings.QUIET_MAX_CONCURRENT_ANALYSES)


def get_quiet_dataset() -> QuietDataset:
    """Production wiring: disabled unless QUIET_ENGINE_ENABLED is true."""
    return default_quiet_dataset()


def get_quiet_engine(
    dataset: QuietDataset = Depends(get_quiet_dataset),
) -> QuietEngine:
    """One engine over the verified dataset; never client-supplied rules."""
    return QuietEngine(dataset)


def _route_length_meters(legs: list[QuietLegInput]) -> float:
    return sum(haversine_distance_m(a[0], a[1], b[0], b[1])
               for leg in legs for a, b in zip(leg.geometry, leg.geometry[1:]))


def _round(value: float | None, digits: int) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, digits)


@router.post(
    "/quiet/analyze",
    response_model=QuietAnalyzeResponse,
    summary="Относительный прокси дорожного шума существующей прогулки (не генерирует маршруты)",
)
async def analyze_walk_quiet(
    payload: QuietAnalyzeRequest,
    dataset: QuietDataset = Depends(get_quiet_dataset),
    engine: QuietEngine = Depends(get_quiet_engine),
) -> QuietAnalyzeResponse:
    """Оценка близости УЖЕ СУЩЕСТВУЮЩЕГО пешеходного маршрута к дорогам OSM.

    Результат — безразмерный относительный индекс и честное покрытие. Это не
    дБ, не акустическое измерение и не медицинская оценка. Участки вне
    проверенного пилота остаются неизвестными, а не «тихими».
    """
    legs = [QuietLegInput(
        geometry=tuple((point.latitude, point.longitude)
                       for point in leg.geometry),
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
                detail=("Маршрут вне зоны оценки тишины (Санкт-Петербург и "
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

    evaluated_at = payload.assessed_at or datetime.now(timezone.utc)
    try:
        async with _analysis_semaphore:
            async with asyncio.timeout(settings.QUIET_ANALYSIS_TIMEOUT_SECONDS):
                result = await asyncio.to_thread(
                    engine.analyze, legs)
    except QuietAnalysisInputError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=str(exc)) from None
    except TimeoutError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Расчёт относительной оценки тишины занял слишком много "
                   "времени и был остановлен.") from None
    except Exception as exc:
        logger.warning("Quiet engine failure (%s)", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Оценка тишины временно недоступна. Пешеходный маршрут сохранён.",
        ) from None

    total_r = round(result.route_length_m, 1)
    known_r = round(result.known_m, 1)
    unknown_r = round(max(0.0, total_r - known_r), 1)
    if unknown_r < 0.0:  # defensive; max() above already prevents this
        unknown_r = 0.0
        known_r = total_r
    coverage_r = (known_r / total_r) if total_r > 0 else 0.0
    estimate = result.status in ("available", "partial")
    index_value = _round(result.route_noise_exposure_index, 4) if estimate else None
    if index_value is not None:
        index_value = min(1.0, max(0.0, index_value))
    low_share = _round(result.low_exposure_share, 4) if estimate else None
    major_share = _round(result.major_road_adjacent_share, 4) if estimate else None

    legs_out = None
    if payload.include_legs:
        legs_out = [
            QuietLegOut(
                leg_index=leg.leg_index,
                length_meters=round(leg.length_meters, 1),
                known_meters=round(leg.known_meters, 1),
                unknown_meters=round(leg.unknown_meters, 1),
                road_noise_exposure_index=(
                    _round(leg.road_noise_exposure_index, 4)
                    if estimate else None),
                major_road_adjacent_meters=(
                    _round(leg.major_road_adjacent_meters, 1)
                    if estimate else None),
                low_exposure_meters=(
                    _round(leg.low_exposure_meters, 1)
                    if estimate else None),
            )
            for leg in result.legs
        ]

    return QuietAnalyzeResponse(
        status=result.status,
        estimate=estimate,
        confidence="low" if estimate else "unavailable",
        dataset_version=dataset.dataset_version,
        dataset_sha256=dataset.dataset_sha256,
        evaluated_at=evaluated_at.isoformat(),
        route_length_meters=total_r,
        known_meters=known_r,
        unknown_meters=unknown_r,
        coverage_share=coverage_r,
        road_noise_exposure_index=index_value,
        low_exposure_share=low_share,
        major_road_adjacent_share=major_share,
        mean_nearest_road_distance_m=(
            _round(result.mean_nearest_road_distance_m, 1) if estimate else None),
        max_road_class_nearby=(
            result.max_road_class_nearby if estimate else None),
        legs=legs_out,
        reasons=list(result.reasons),
        limitations=list(LIMITATIONS),
        assumptions=list(ASSUMPTIONS),
        source_provenance=dict(dataset.provenance),
        diagnostics=dict(result.diagnostics),
    )
