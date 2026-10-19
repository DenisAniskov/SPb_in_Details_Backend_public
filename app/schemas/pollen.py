"""C7.3 v1 accepts existing native geometry, never client pollen or health data.

There are deliberately NO threshold, risk-level or medical fields here: the
source publishes concentrations in grains/m³, and inventing danger categories
for them would require a verified source per species (ТЗ C7.3 §4). The
response therefore reports per-species concentrations, coverage, and a
concentration-time integral whose units (grains·h/m³) state exactly what it
is: not an inhaled dose and not a symptom forecast.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.route import Coordinates

Status = Literal["available", "partial", "unavailable", "outside_coverage"]
SpeciesState = Literal["available", "partial", "unknown"]


class PollenLegIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    geometry: list[Coordinates] = Field(min_length=2, max_length=2000)
    walking_duration_seconds: float = Field(gt=0, le=6 * 3600, allow_inf_nan=False)
    visit_duration_seconds_after_leg: float = Field(default=0, ge=0, le=24 * 3600,
                                                   allow_inf_nan=False)


class PollenAnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    departure_at: datetime
    legs: list[PollenLegIn] = Field(min_length=1, max_length=24)
    include_legs: bool = False

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


class PollenPeriodOut(BaseModel):
    start: str
    end: str
    timezone: str


class PollenSpeciesOut(BaseModel):
    """One species over the assessed walk.

    On an ``unavailable``/``outside_coverage`` response the contract forbids
    numbers, so every species is published as ``unknown`` with all four number
    fields ``null`` (ТЗ C7.3.1). ``known_seconds``/``known_share`` still
    describe the SOURCE's own coverage of that species: that is how a client
    tells "the source had nothing to report" (share 0) from "the source had
    values, and no hour of the walk carried the complete panel, so the numbers
    are withheld" (share > 0, reason ``pollen_no_complete_panel``).
    """

    code: str
    name: str
    unit: str
    status: SpeciesState
    known_seconds: float
    total_seconds: float
    known_share: float
    mean_concentration: float | None = None
    peak_concentration: float | None = None
    peak_at: str | None = None
    exposure_grains_hours_per_m3: float | None = None


class PollenLegOut(BaseModel):
    leg_index: int
    length_meters: float
    known_meters: float
    unknown_meters: float
    starts_at_seconds: float
    ends_at_seconds: float
    inside_source_cell: bool
    species: list[PollenSpeciesOut]


class PollenAnalyzeResponse(BaseModel):
    status: Status
    method: str = "experimental_pollen_exposure"
    method_version: str = "c7_3.1"
    estimate: bool = True
    confidence: Literal["low", "unavailable"]
    evaluated_period: PollenPeriodOut
    route_length_meters: float
    known_meters: float
    unknown_meters: float
    coverage_share: float
    walking_duration_seconds: float
    elapsed_duration_seconds: float
    concentration_unit: str = "grains/m³"
    species: list[PollenSpeciesOut]
    legs: list[PollenLegOut] | None = None
    reasons: list[str]
    limitations: list[str]
    assumptions: list[str]
    source_provenance: dict[str, Any]
    #: What the ENGINE found in the source — not what the response publishes.
    #: On the withholding path ``species`` reports every species as unknown
    #: while these counts keep the computed outcome, and ``species_withheld``
    #: states how many species had values whose numbers were not published.
    diagnostics: dict[str, Any]
