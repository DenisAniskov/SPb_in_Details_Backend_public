from typing import List, Optional
from pydantic import BaseModel, Field, field_validator


class Coordinates(BaseModel):
    latitude: float = Field(..., ge=-90.0, le=90.0, allow_inf_nan=False, description="Широта (WGS84)")
    longitude: float = Field(..., ge=-180.0, le=180.0, allow_inf_nan=False, description="Долгота (WGS84)")

    @field_validator("latitude", "longitude", mode="before")
    @classmethod
    def reject_non_numeric_coordinates(cls, value):
        if type(value) not in (int, float):
            raise ValueError("Coordinates must be numbers, not text or booleans")
        return value


class MultiPointRouteRequest(BaseModel):
    start_point: Coordinates = Field(..., description="Координаты стартовой точки")
    destination_place_id: int = Field(..., description="ID целевого места")
    via_place_ids: Optional[List[int]] = Field(default_factory=list, max_length=6, description="Промежуточные места для прогулки")


class EnvironmentalFactorStatus(BaseModel):
    factor_name: str
    status: str = "planned_integration"
    data_source_requirement: str
    description: str


class RouteAssessmentOverview(BaseModel):
    """
    Архитектурная схема для будущей оценки пешеходных маршрутов по тени, тишине и пыльце.
    Не выдаёт случайные заглушки за реальные измерения:
    честно фиксирует требуемые источники данных.
    """
    status: str = "not_implemented"
    message: str = "Это будущий контракт API. Маршрут и факторы среды не рассчитываются."
    route_distance_meters: Optional[float] = None
    estimated_walking_duration_seconds: Optional[int] = None
    shade_assessment: EnvironmentalFactorStatus = Field(
        default_factory=lambda: EnvironmentalFactorStatus(
            factor_name="Тень (солнечная освещенность)",
            status="requires_3d_spatial_data",
            data_source_requirement="Положение Солнца + 3D-модели зданий OpenStreetMap/CityGML + карта крон деревьев",
            description="Расчёт положения Солнца и затенения не реализован. Требуются проверенные геоданные и отдельный алгоритм."
        )
    )
    quietness_assessment: EnvironmentalFactorStatus = Field(
        default_factory=lambda: EnvironmentalFactorStatus(
            factor_name="Тишина (относительный дорожный прокси)",
            status="implemented_read_only",
            data_source_requirement="Проверенный OSM-снимок дорожной сети Санкт-Петербурга; отдельный read-only endpoint",
            description=(
                "Комплексная оценка маршрута по-прежнему не объединена в этом "
                "устаревшем контракте. Относительная оценка близости к дорогам "
                "доступна отдельно: POST /api/v1/environment/quiet/analyze. "
                "Это не измерение шума и не дБ."
            )
        )
    )
    pollen_assessment: EnvironmentalFactorStatus = Field(
        default_factory=lambda: EnvironmentalFactorStatus(
            factor_name="Пыльца (аллергобезопасность)",
            status="requires_botanical_pollen_monitoring",
            data_source_requirement="Календарь пыления берёзы/ольхи/злаков СПб + данные мониторинга пыльцы",
            description="Расчёт пыльцы не реализован; данные мониторинга не подключены."
        )
    )


from app.schemas.place import PlaceRead
from app.models.enums import PlaceCategory


class ProposedWalkItem(BaseModel):
    place: PlaceRead
    visit_duration_minutes: int
    reasons: List[str] = Field(default_factory=list, description="Понятные человеку причины выбора места")


class AutoWalkProposalRequest(BaseModel):
    """
    Запрос на автоматическое составление пешеходной прогулки.
    Предусматривает параметры предпочтений: точку старта, лимит мест,
    категории интересов, предпочтение необычных мест и лимит времени прогулки.
    """
    start_point: Coordinates = Field(..., description="Координаты начальной точки")
    time_budget_minutes: int = Field(default=120, ge=30, le=360, strict=True, description="Доступное время прогулки в минутах (30..360)")
    categories: Optional[List[PlaceCategory]] = Field(default=None, max_length=6, description="Выбранные категории: строгий фильтр; пусто — любые")
    prefer_unusual: bool = Field(default=False, strict=True, description="Приоритет необычным атмосферным местам")
    max_places: int = Field(default=6, ge=1, le=6, strict=True, description="Желаемое количество мест (максимум 6)")

    @field_validator("categories", mode="after")
    @classmethod
    def deduplicate_categories(cls, v: Optional[List[PlaceCategory]]) -> Optional[List[PlaceCategory]]:
        if v is not None:
            return list(dict.fromkeys(v))
        return v


class AutoWalkAlternative(BaseModel):
    """
    Результат автоматического составления туристической прогулки.
    """
    time_budget_minutes: int = Field(..., description="Запрошенный бюджет времени")
    estimated_walking_duration_seconds: int = Field(..., description="Эвристика Haversine / 75 м в минуту; не время MapKit")
    estimated_visit_duration_minutes: int = Field(..., description="Суммарное рекомендованное время осмотра в минутах")
    estimated_total_duration_minutes: int = Field(..., description="Общая продолжительность прогулки (ходьба + осмотр)")
    estimated_total_distance_meters: float = Field(..., description="Геометрическая Haversine-оценка в метрах, не длина пешеходного маршрута")
    suggested_places_sequence: List[int] = Field(default_factory=list, description="Рекомендованная последовательность ID мест")
    items: List[ProposedWalkItem] = Field(default_factory=list, description="Детализация выбранных мест с объяснением")


class AutoWalkProposalResponse(AutoWalkAlternative):
    status: str = "success"
    message: str = "Маршрут успешно сформирован"
    reason_code: Optional[str] = Field(default=None, description="Честная причина пустого результата; null при успехе")
    alternatives: List[AutoWalkAlternative] = Field(default_factory=list, max_length=2,
        description="До двух предварительно допустимых альтернатив с другими наборами POI; MapKit ещё не проверен")
