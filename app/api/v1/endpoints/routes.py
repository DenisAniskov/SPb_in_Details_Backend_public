from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.session import get_db
from app.schemas.route import (
    MultiPointRouteRequest,
    RouteAssessmentOverview,
    AutoWalkProposalRequest,
    AutoWalkProposalResponse,
)
from app.services.place_service import PlaceService

router = APIRouter()


@router.post("/evaluate", response_model=RouteAssessmentOverview, summary="Будущий контракт оценки (не реализовано)")
async def evaluate_route(
    payload: MultiPointRouteRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Архитектурная точка входа для будущей комплексной оценки пешеходных маршрутов
    по факторам тени, тишины и пыльцы.
    
    Честно информирует о статусе готовности и требуемых геопространственных слоях,
    не выдавая случайные числа за измерения.
    """
    dest = await PlaceService.get_place_by_id(db=db, place_id=payload.destination_place_id)
    if not dest:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Целевое место с ID {payload.destination_place_id} не найдено",
        )
        
    return RouteAssessmentOverview()


from app.services.smart_walk_service import SmartWalkService


@router.post("/propose-walk", response_model=AutoWalkProposalResponse, summary="Автоматический подбор туристической прогулки")
async def propose_walk(
    payload: AutoWalkProposalRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Детерминированный подбор туристической прогулки по СПб.
    Учитывает стартовую точку, лимит времени, категории интересов и предпочтение необычных мест.
    """
    all_places = await PlaceService.get_places(db=db, limit=None)
    if not all_places and not await PlaceService.has_stored_places(db):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="В каталоге отсутствуют туристические места",
        )

    result = SmartWalkService.propose_walk(
        start_lat=payload.start_point.latitude,
        start_lon=payload.start_point.longitude,
        time_budget_minutes=payload.time_budget_minutes,
        all_places=all_places,
        categories=payload.categories,
        prefer_unusual=payload.prefer_unusual,
        max_places=payload.max_places,
    )
    return AutoWalkProposalResponse(**result)
