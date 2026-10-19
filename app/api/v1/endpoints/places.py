from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.session import get_db
from app.models.enums import PlaceCategory
from app.schemas.place import PlaceRead, PlaceDetail, PlaceListResponse, CategoryInfo
from app.schemas.place_history import PlaceHistoryResponse
from app.services.place_service import PlaceService
from app.db.place_history import history_for

router = APIRouter()


@router.get("/categories", response_model=List[CategoryInfo], summary="Список категорий интересов")
async def get_categories(db: AsyncSession = Depends(get_db)):
    """Возвращает список всех категорий интересов на русском языке с количеством доступных мест."""
    return await PlaceService.get_categories_info(db)


@router.get("", response_model=PlaceListResponse, summary="Получить список мест с фильтрацией")
async def list_places(
    category: Optional[List[PlaceCategory]] = Query(
        None,
        description="Фильтр по одной или нескольким категориям"
    ),
    is_unusual: Optional[bool] = Query(
        None,
        description="Фильтр по признаку «необычное место»"
    ),
    q: Optional[str] = Query(
        None,
        max_length=200,
        description="Поисковый запрос по названию, адресу, описанию и тегам"
    ),
    limit: int = Query(100, ge=1, le=200, description="Количество записей"),
    offset: int = Query(0, ge=0, description="Смещение пагинации"),
    db: AsyncSession = Depends(get_db),
):
    """
    Возвращает список мест Санкт-Петербурга с поддержкой фильтрации по категориям,
    признаку «необычное место» и текстовому поиску.
    """
    items = await PlaceService.get_places(
        db=db,
        categories=category,
        is_unusual=is_unusual,
        search_query=q,
        limit=limit,
        offset=offset,
    )
    total = await PlaceService.get_total_count(
        db=db,
        categories=category,
        is_unusual=is_unusual,
        search_query=q,
    )
    return PlaceListResponse(total=total, items=items)


@router.get("/{place_id}", response_model=PlaceDetail, summary="Подробная информация о месте")
async def get_place_detail(
    place_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Возвращает полную карточку места по его уникальному ID."""
    place = await PlaceService.get_place_by_id(db=db, place_id=place_id)
    if not place:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Место с идентификатором {place_id} не найдено",
        )
    return place


@router.get("/{place_id}/history", response_model=PlaceHistoryResponse, summary="Проверенная историческая справка о месте")
async def get_place_history(
    place_id: int,
    db: AsyncSession = Depends(get_db),
):
    """
    Phase C6: возвращает редакторскую историческую карточку места по её
    стабильному catalog_key. Карточка может отсутствовать (history: null) —
    исторический слой опционален и никогда не ломает основной каталог.
    Файл набора читается из кеша, сетевые обращения не выполняются.
    """
    place = await PlaceService.get_place_by_id(db=db, place_id=place_id)
    if not place:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Место с идентификатором {place_id} не найдено",
        )
    return PlaceHistoryResponse(
        placeId=place.id,
        catalogKey=place.catalog_key,
        history=history_for(place.catalog_key),
    )
