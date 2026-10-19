"""C11 quiet-road contract: relative proxy, coverage, provenance, no dB."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.route import Coordinates

Status = Literal["available", "partial", "unavailable", "outside_coverage"]


class QuietLegIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    geometry: list[Coordinates] = Field(min_length=2, max_length=2000)
    # Kept for symmetry with the existing environmental contracts; the static
    # C11 proxy does not use walking time to model traffic.
    walking_duration_seconds: float | None = Field(
        default=None, gt=0, le=6 * 3600, allow_inf_nan=False)


class QuietAnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assessed_at: datetime | None = None
    legs: list[QuietLegIn] = Field(min_length=1, max_length=24)
    include_legs: bool = False

    @field_validator("assessed_at")
    @classmethod
    def aware_time(cls, value):
        if value is None:
            return value
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("assessed_at requires a timezone")
        if not 2000 <= value.year <= 2100:
            raise ValueError("assessed_at year is unsupported")
        return value

    @model_validator(mode="after")
    def bounded_geometry(self):
        if sum(len(leg.geometry) for leg in self.legs) > 5000:
            raise ValueError("too many geometry points")
        return self


class QuietLegOut(BaseModel):
    leg_index: int
    length_meters: float
    known_meters: float
    unknown_meters: float
    road_noise_exposure_index: float | None = None
    major_road_adjacent_meters: float | None = None
    low_exposure_meters: float | None = None


class QuietAnalyzeResponse(BaseModel):
    status: Status
    estimate: bool
    confidence: Literal["low", "unavailable"]
    method: str = "relative_road_noise_exposure_proxy"
    method_version: str = "c11_1"
    dataset_version: str | None = None
    dataset_sha256: str | None = None
    evaluated_at: str
    temporal_model: str = "static_osm_network"
    traffic_time_modeled: bool = False
    time_context_note: str = (
        "Время используется только для согласования отчёта; статическая "
        "модель дорожной сети не прогнозирует транспортный поток по часам.")
    route_length_meters: float
    known_meters: float
    unknown_meters: float
    coverage_share: float
    road_noise_exposure_index: float | None = None
    index_scale: str = (
        "Безразмерный относительный прокси 0..1: выше — ближе к более крупным "
        "автомобильным дорогам. Не дБ и не измерение шума.")
    low_exposure_share: float | None = None
    major_road_adjacent_share: float | None = None
    mean_nearest_road_distance_m: float | None = None
    max_road_class_nearby: str | None = None
    legs: list[QuietLegOut] | None = None
    reasons: list[str]
    limitations: list[str]
    assumptions: list[str]
    source_provenance: dict[str, Any]
    diagnostics: dict[str, Any]
