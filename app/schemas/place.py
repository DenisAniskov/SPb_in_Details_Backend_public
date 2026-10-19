import math
import re
from typing import Optional, List
from pydantic import BaseModel, ConfigDict, Field, field_validator
from app.models.enums import PlaceCategory


class CategoryInfo(BaseModel):
    id: PlaceCategory
    name: str
    description: str
    count: int = 0


class PlaceBase(BaseModel):
    title: str = Field(..., max_length=200, description="Название места")
    short_description: str = Field(..., max_length=500, description="Краткое описание для карточки")
    category: PlaceCategory = Field(..., description="Категория места")
    is_unusual: bool = Field(default=False, description="Признак «необычное место»")
    latitude: float = Field(..., ge=-90.0, le=90.0, allow_inf_nan=False, description="Широта (WGS84)")
    longitude: float = Field(..., ge=-180.0, le=180.0, allow_inf_nan=False, description="Долгота (WGS84)")
    address: str = Field(..., max_length=300, description="Адрес")
    photo_url: Optional[str] = Field(default=None, description="Ссылка на фотографию")
    rating: Optional[float] = Field(default=None, ge=0.0, le=5.0, description="Рейтинг места, только при наличии источника")
    recommended_visit_minutes: int = Field(default=30, ge=5, le=180, description="Приблизительная редакторская оценка SmartNavigator, не официальные часы/данные Яндекса")

    @field_validator("is_unusual", mode="before")
    @classmethod
    def _only_real_booleans_are_unusual(cls, v):
        # Hostile storage must never mint unusual=true from arbitrary truthy
        # text/bytes: anything that is not a boolean (or 0/1) is honest "false".
        if isinstance(v, bool):
            return v
        if isinstance(v, int) and v in (0, 1):
            return bool(v)
        return False

    @field_validator("recommended_visit_minutes", mode="before")
    @classmethod
    def _coerce_recommended_visit(cls, v):
        if v is None:
            return 30
        return v

    @field_validator("photo_url", mode="before")
    @classmethod
    def _coerce_photo_url(cls, v):
        return v if isinstance(v, str) else None

    @field_validator("rating", mode="before")
    @classmethod
    def _drop_unsupported_rating(cls, v):
        # Рейтинг в каталоге не публикуется: значение вне допустимого диапазона
        # или нечисловое мусорное значение из БД честнее спрятать (None),
        # чем уронить выдачу или показать недействительное число.
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        return v if math.isfinite(v) and 0.0 <= v <= 5.0 else None


class PlaceRead(PlaceBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    catalog_key: Optional[str] = Field(
        default=None, max_length=80, pattern=r"^[a-z][a-z0-9-]{0,79}$",
        description="Стабильный ключ каталога, независимый от ID базы; null для места без ключа",
    )
    tags: Optional[str] = None
    source_url: Optional[str] = None
    coordinate_kind: str = "unverified"
    address_status: str = "unverified"
    why_visit: Optional[str] = Field(default=None, description="Почему стоит посетить")
    # Phase C3: display point is the published representative coordinate;
    # routing fields are filled only with a verified public entrance/approach.
    display_latitude: float = Field(..., ge=-90.0, le=90.0, allow_inf_nan=False, description="Точка отображения объекта на карте")
    display_longitude: float = Field(..., ge=-180.0, le=180.0, allow_inf_nan=False, description="Точка отображения объекта на карте")
    routing_latitude: Optional[float] = Field(default=None, description="Проверенная точка пешеходного подхода, если существует")
    routing_longitude: Optional[float] = Field(default=None, description="Проверенная точка пешеходного подхода, если существует")
    routing_point_source: Optional[str] = Field(default=None, description="Источник проверенной точки подхода")
    routing_point_verified_on: Optional[str] = Field(default=None, description="Дата проверки точки подхода")
    routing_access_status: str = Field(default="representative_point_only", description="Статус доступности пешеходного подхода")

    @field_validator("catalog_key", mode="before")
    @classmethod
    def _only_valid_catalog_keys(cls, v):
        # The seed catalogue already enforces this immutable identity format.
        # Malformed legacy storage must not mint an identity or break the list.
        return v if isinstance(v, str) and re.fullmatch(r"[a-z][a-z0-9-]{0,79}", v) else None

    @field_validator("routing_latitude", "routing_longitude", mode="before")
    @classmethod
    def _drop_unusable_routing_coordinate(cls, v):
        # Половичные/нечисловые точки подхода честнее спрятать (None),
        # чем уронить выдачу или предложить недействительный маршрут.
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        return v if math.isfinite(v) else None

    @field_validator("title", "short_description", "address", mode="before")
    @classmethod
    def _unavailable_required_text(cls, v):
        return v if isinstance(v, str) else "Данные недоступны"

    @field_validator("recommended_visit_minutes", mode="before")
    @classmethod
    def _unavailable_stored_visit(cls, v):
        # Non-numeric SQLite storage is unavailable. Numeric values still undergo
        # the strict 5..180 schema boundary; startup repairs invalid legacy values.
        return v if type(v) in (int, float) else 30

    @field_validator("tags", mode="before")
    @classmethod
    def _coerce_tags(cls, v):
        return v if isinstance(v, str) else None


class PlaceDetail(PlaceRead):
    model_config = ConfigDict(from_attributes=True)
    full_description: str = Field(..., description="Подробное описание места")
    working_hours: Optional[str] = Field(default=None, description="Часы работы")
    architectural_style: Optional[str] = Field(default=None, description="Архитектурный стиль")
    year_built: Optional[str] = Field(default=None, description="Год постройки")
    visit_note: Optional[str] = Field(default=None, description="Совет и контекст посещения")
    official_website: Optional[str] = Field(default=None, description="Официальный сайт (из OSM)")
    wikipedia_url: Optional[str] = Field(default=None, description="Ссылка на Википедию (из OSM)")

    @field_validator("full_description", mode="before")
    @classmethod
    def _unavailable_description(cls, v):
        return v if isinstance(v, str) else "Описание недоступно"

    @field_validator("working_hours", "architectural_style", "year_built", mode="before")
    @classmethod
    def _unavailable_optional_text(cls, v):
        return v if isinstance(v, str) else None


class PlaceCreate(PlaceBase):
    full_description: str
    working_hours: Optional[str] = None
    tags: Optional[str] = None
    architectural_style: Optional[str] = None
    year_built: Optional[str] = None


class PlaceListResponse(BaseModel):
    total: int
    items: List[PlaceRead]
