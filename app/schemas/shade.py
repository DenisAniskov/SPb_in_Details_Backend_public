"""API schemas for the C7.1 Shade Engine (§11).

The request carries ONLY what the client legitimately knows: the walk's real
native pedestrian geometry per leg, the native walking durations, the POI
visit durations and the departure instant. Building geometry NEVER travels
from the client — it comes from the internal read-only dataset.

Every shade number is explicitly an ESTIMATE (``estimate`` flag +
``limitations`` + ``confidence``); unknown ground is reported as ``unknown``,
never as sun (§9).
"""

from datetime import datetime
from typing import List, Literal, Optional, Tuple

from pydantic import BaseModel, Field, field_validator

from app.schemas.route import Coordinates

#: §11.2 status vocabulary.
ShadeStatus = Literal[
    "available", "partial", "unavailable", "outside_coverage", "sun_below_horizon"]
Confidence = Literal["high", "medium", "low", "unavailable"]
SegmentState = Literal["estimated_shaded", "estimated_exposed", "unknown", "mixed"]


class WeatherContextInput(BaseModel):
    """Optional cloud-cover context. Influences RELEVANCE only — never geometry."""

    cloud_cover_fraction: Optional[float] = Field(
        default=None, ge=0.0, le=1.0,
        description="Доля облачности 0..1 (None — неизвестна). Не влияет на геометрию теней.")
    source: Optional[str] = Field(default=None, description="Источник данных о погоде (справочно).")


class ShadeWalkLegIn(BaseModel):
    """One real walking leg: native geometry + native walking duration."""

    geometry: List[Coordinates] = Field(
        ..., min_length=2, max_length=2000,
        description="Полилиния этапа (реальная пешеходная геометрия Yandex MapKit), WGS84.")
    walking_duration_seconds: float = Field(
        ..., gt=0, le=6 * 3600, allow_inf_nan=False,
        description="Нативная длительность ходьбы по этому этапу, секунды.")
    visit_duration_seconds_after_leg: float = Field(
        default=0.0, ge=0, le=24 * 3600, allow_inf_nan=False,
        description="Длительность осмотра POI ПОСЛЕ этого этапа, секунды (0 для последнего).")


class ShadeAnalyzeRequest(BaseModel):
    """Запрос анализа тени для СУЩЕСТВУЮЩЕЙ прогулки (не генерация маршрута)."""

    departure_at: datetime = Field(
        ..., description="Момент начала прогулки, ISO-8601 ОБЯЗАТЕЛЬНО с часовым поясом.")
    legs: List[ShadeWalkLegIn] = Field(
        ..., min_length=1, max_length=24,
        description="Этапы прогулки: геометрия + нативное время ходьбы + время осмотра после этапа.")
    include_segments: bool = Field(
        default=False, description="Вернуть классифицированные участки (тень/солнце/неизвестно).")
    weather: Optional[WeatherContextInput] = Field(
        default=None, description="Опциональный контекст облачности (влияет только на применимость).")

    @field_validator("departure_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("departure_at must include a timezone offset (timezone-aware ISO-8601)")
        return value


class EvaluatedPeriodOut(BaseModel):
    start: str = Field(..., description="Начало прогулки (ISO-8601)")
    end: str = Field(..., description="Расчётное завершение прогулки (ISO-8601)")
    timezone: str = Field(..., description="Часовой пояс расчёта (из departure_at)")


class SolarSummaryOut(BaseModel):
    min_elevation_deg: float = Field(..., description="Минимальная высота Солнца за прогулку, °")
    max_elevation_deg: float = Field(..., description="Максимальная высота Солнца за прогулку, °")
    min_azimuth_deg: float = Field(..., description="Минимальный азимут Солнца, ° от севера")
    max_azimuth_deg: float = Field(..., description="Максимальный азимут Солнца, ° от севера")
    bucket_count: int = Field(..., description="Число интервалов положения Солнца (шаг 10 минут)")


class CoverageMetricsOut(BaseModel):
    dataset_zones_touched: List[str] = Field(default_factory=list)
    outside_coverage_meters: float = Field(default=0.0, ge=0.0)
    coverage_share: float = Field(default=0.0, ge=0.0, le=1.0)


class HeightProvenanceMetricsOut(BaseModel):
    externally_verified: int = Field(default=0, description="Высота подтверждена внешним источником")
    osm_declared: int = Field(default=0, description="Высота из тега OSM height=*")
    levels_estimate: int = Field(default=0, description="Оценка по building:levels × 3.3 м")
    unavailable: int = Field(default=0, description="Высота неизвестна (тень не выдумывается)")
    buildings_considered: int = Field(default=0)


class ClassifiedSegmentOut(BaseModel):
    """Один участок прогулки (≈40 м вдоль реальной геометрии).

    ``geometry`` — РЕАЛЬНАЯ полилиния участка (со всеми внутренними вершинами
    исходного маршрута): клиент рисует её как есть и не имеет возможности
    заменить поворот прямой линией.

    ``shaded_spans`` / ``unknown_spans`` — ИЗМЕРЕННЫЕ интервалы (метры вдоль
    участка от его начала), где начинается и заканчивается теневая полоса.
    Их сумма равна ``shaded_meters`` / ``unknown_meters``; остаток участка —
    ``estimated_exposed``. Без этих интервалов клиент не знает, где именно
    находится тень, и любая её отрисовка была бы выдуманной границей (§14.4).
    """

    leg_index: int = Field(..., ge=0)
    geometry: List[Coordinates] = Field(
        ..., min_length=2,
        description="Полилиния участка (реальная геометрия этапа, WGS84), включая повороты.")
    length_meters: float = Field(..., ge=0.0, description="Длина участка вдоль полилинии, м")
    shaded_meters: float = Field(..., ge=0.0)
    unknown_meters: float = Field(..., ge=0.0)
    state: SegmentState = Field(
        ...,
        description=("estimated_shaded — ВЕСЬ участок в тени; estimated_exposed — весь на солнце; "
                     "unknown — весь участок неизвестен; mixed — внутри участка несколько состояний, "
                     "их границы заданы интервалами."))
    starts_at_seconds: float = Field(..., ge=0.0, description="Смещение от начала прогулки")
    ends_at_seconds: float = Field(..., ge=0.0, description="Окончание участка от начала прогулки")
    shaded_spans: List[Tuple[float, float]] = Field(
        default_factory=list,
        description="[начало, конец] тени в метрах вдоль участка; непересекающиеся, в [0, length_meters]")
    unknown_spans: List[Tuple[float, float]] = Field(
        default_factory=list,
        description="[начало, конец] участков «неизвестно» в метрах вдоль участка; тень имеет приоритет")


class ShadeAnalyzeResponse(BaseModel):
    """Ответ C7.1. Длины «оценка» присутствуют только при status available/partial."""

    status: ShadeStatus = Field(
        ..., description="available | partial | unavailable | outside_coverage | sun_below_horizon")
    method: str = Field(..., description="Идентификатор метода")
    method_version: str = Field(..., description="Версия метода")
    estimate: bool = Field(default=True, description="Всегда true: это оценка, а не измерение")
    evaluated_period: EvaluatedPeriodOut
    dataset_version: Optional[str] = Field(default=None, description="Версия датасета геометрии зданий")
    dataset_provenance: str = Field(..., description="Происхождение датасета (лицензия, снимок)")
    solar_summary: Optional[SolarSummaryOut] = Field(default=None)
    route_length_meters: float = Field(..., ge=0.0, description="Геодезическая длина прогулки, м")
    estimated_shaded_meters: Optional[float] = Field(default=None, ge=0.0)
    estimated_exposed_meters: Optional[float] = Field(default=None, ge=0.0)
    unknown_meters: Optional[float] = Field(default=None, ge=0.0)
    shade_fraction: Optional[float] = Field(
        default=None, ge=0.0, le=1.0,
        description="ОЦЕНКА доли длины в тени; None, если расчёт недоступен")
    confidence: Confidence = Field(..., description="high | medium | low | unavailable")
    confidence_reason: Optional[str] = Field(default=None)
    coverage_metrics: CoverageMetricsOut = Field(default_factory=CoverageMetricsOut)
    height_provenance_metrics: HeightProvenanceMetricsOut = Field(
        default_factory=HeightProvenanceMetricsOut)
    weather_relevance: Optional[str] = Field(default=None)
    weather_relevance_explanation: Optional[str] = Field(default=None)
    limitations: List[str] = Field(default_factory=list)
    classified_segments: Optional[List[ClassifiedSegmentOut]] = Field(
        default=None, description="Только при include_segments=true и наличии расчёта")
    reason: Optional[str] = Field(default=None, description="Честное объяснение, если расчёт недоступен")
