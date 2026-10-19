from typing import List, Optional
from pydantic import BaseModel, Field


class SuggestItemResponse(BaseModel):
    title: str = Field(..., description="Название найденного объекта")
    subtitle: Optional[str] = Field(None, description="Адрес или дополнительное описание объекта")
    tags: List[str] = Field(default_factory=list, description="Теги категории объекта (например, metro, biz)")
    uri: Optional[str] = Field(None, description="Уникальный идентификатор/URI Яндекса для последующего геокодирования")
    distance_text: Optional[str] = Field(None, description="Текстовое расстояние до объекта (если передана геопозиция)")


class SuggestResponse(BaseModel):
    query: str = Field(..., description="Исходный поисковый запрос")
    results: List[SuggestItemResponse] = Field(default_factory=list, description="Список подсказок Яндекса")


class GeocodeResultResponse(BaseModel):
    title: str = Field(..., description="Название объекта")
    subtitle: Optional[str] = Field(None, description="Описание или район")
    address: Optional[str] = Field(None, description="Полный нормализованный адрес")
    latitude: float = Field(..., ge=-90, le=90, allow_inf_nan=False, description="Широта (WGS84)")
    longitude: float = Field(..., ge=-180, le=180, allow_inf_nan=False, description="Долгота (WGS84)")
    uri: Optional[str] = Field(None, description="Идентификатор URI Яндекса")
    kind: Optional[str] = Field(None, description="Тип топонима (house, street, metro, station, biz)")


class ReverseGeocodeResponse(BaseModel):
    title: str = Field(..., description="Короткое наименование (дом или улица)")
    address: str = Field(..., description="Полный адрес")
    subtitle: Optional[str] = Field(None, description="Дополнительное описание (город, район)")
    latitude: float = Field(..., ge=-90, le=90, allow_inf_nan=False, description="Широта (WGS84)")
    longitude: float = Field(..., ge=-180, le=180, allow_inf_nan=False, description="Долгота (WGS84)")
