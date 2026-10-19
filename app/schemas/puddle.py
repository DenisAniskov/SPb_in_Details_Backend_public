"""C7.2 v1 accepts existing native geometry, never client weather/OSM data."""
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.route import Coordinates

RiskState = Literal["low", "moderate", "high", "unknown"]


class PuddleLegIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    geometry: list[Coordinates] = Field(min_length=2, max_length=2000)
    walking_duration_seconds: float = Field(gt=0, le=6 * 3600, allow_inf_nan=False)
    visit_duration_seconds_after_leg: float = Field(default=0, ge=0, le=24 * 3600,
                                                   allow_inf_nan=False)


class PuddleAnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    departure_at: datetime
    legs: list[PuddleLegIn] = Field(min_length=1, max_length=24)
    include_segments: bool = False

    @field_validator("departure_at")
    @classmethod
    def aware_time(cls, value):
        if value.tzinfo is None or value.utcoffset() is None or not 2000 <= value.year <= 2100:
            raise ValueError("departure_at requires a timezone and a supported year")
        return value

    @model_validator(mode="after")
    def bounded_walk(self):
        if sum(len(leg.geometry) for leg in self.legs) > 5000:
            raise ValueError("too many geometry points")
        # Final POI visiting does not advance a segment that does not exist.
        elapsed = sum(leg.walking_duration_seconds for leg in self.legs)
        elapsed += sum(leg.visit_duration_seconds_after_leg for leg in self.legs[:-1])
        if elapsed > 24 * 3600:
            raise ValueError("walk elapsed time exceeds 24 hours")
        return self


class PuddlePeriodOut(BaseModel):
    start: str
    end: str
    timezone: str


class PuddleSegmentOut(BaseModel):
    leg_index: int
    geometry: list[Coordinates]
    length_meters: float
    state: RiskState
    risk_index: float | None = None
    starts_at_seconds: float
    ends_at_seconds: float
    reasons: list[str]


class PuddleAnalyzeResponse(BaseModel):
    status: Literal["available", "partial", "unavailable", "outside_coverage"]
    method: str = "experimental_precipitation_surface_index"
    method_version: str = "c7_2.4"
    estimate: bool = True
    risk_level: RiskState
    risk_index: float | None = None
    route_length_meters: float
    known_meters: float
    unknown_meters: float
    low_meters: float
    moderate_meters: float
    high_meters: float
    coverage_share: float
    confidence: Literal["low", "unavailable"]
    evaluated_period: PuddlePeriodOut
    dataset_version: str | None = None
    dataset_provenance: dict[str, Any]
    weather_provenance: dict[str, Any]
    reasons: list[str]
    limitations: list[str]
    classified_segments: list[PuddleSegmentOut] | None = None
    sensitivity: dict[str, Any]
