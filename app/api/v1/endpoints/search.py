from typing import Optional
from fastapi import APIRouter, Query, status
from app.schemas.search import (
    GeocodeResultResponse,
    ReverseGeocodeResponse,
    SuggestResponse,
)
from app.services.yandex_search_service import yandex_search_service

router = APIRouter()


@router.get(
    "/suggest",
    response_model=SuggestResponse,
    summary="Поисковые подсказки адресов и мест",
    description=(
        "Возвращает подсказки названий объектов и адресов через Яндекс Геосаджест API. "
        "Поиск приоритезирует объекты Санкт-Петербурга и агломерации."
    ),
)
async def get_suggestions(
    text: str = Query(..., min_length=1, max_length=150, description="Текст поискового запроса"),
    results: int = Query(default=7, ge=1, le=10, description="Количество подсказок (1-10)"),
) -> SuggestResponse:
    items = await yandex_search_service.get_suggestions(query=text, results_limit=results)
    return SuggestResponse(query=text, results=items)


@router.get(
    "/geocode",
    response_model=GeocodeResultResponse,
    summary="Прямое геокодирование объекта (получение координат)",
    description=(
        "Преобразует текстовый адрес или уникальный идентификатор URI Яндекса "
        "в координаты WGS84 и адрес через Яндекс Геокодер; точность зависит от найденного объекта."
    ),
)
async def geocode_point(
    text: Optional[str] = Query(default=None, max_length=250, description="Текст адреса или названия"),
    uri: Optional[str] = Query(default=None, max_length=500, description="Идентификатор объекта из suggest"),
) -> GeocodeResultResponse:
    return await yandex_search_service.geocode_object(text=text, uri=uri)


@router.get(
    "/reverse-geocode",
    response_model=ReverseGeocodeResponse,
    summary="Обратное геокодирование точки на карте",
    description="Определяет адрес и название объекта по географическим координатам (широта, долгота).",
)
async def reverse_geocode_point(
    lat: float = Query(..., ge=-90.0, le=90.0, description="Широта (latitude)"),
    lon: float = Query(..., ge=-180.0, le=180.0, description="Долгота (longitude)"),
) -> ReverseGeocodeResponse:
    return await yandex_search_service.reverse_geocode(latitude=lat, longitude=lon)
