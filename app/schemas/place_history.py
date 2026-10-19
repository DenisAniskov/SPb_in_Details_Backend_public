"""Phase C6: structured historical content schemas.

The historical dataset is a versioned editorial store (backend/app/db/place_history.json)
keyed by the stable ``catalog_key`` of the starter catalogue. These models are the
single validation boundary between the editorial file and the API: nothing reaches
a client before passing every constraint below. The historical layer is optional —
a missing card never breaks the place catalogue — but an INVALID editorial file is
rejected loudly at load time, not served half-parsed.
"""
from typing import Optional, List, Literal
from urllib.parse import urlparse
from datetime import date
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Allowed link schemes for editorial sources. Anything else (javascript:,
# data:, file:, ftp: …) is a hostile or broken reference and is rejected at
# validation time — the client only ever opens verified http(s) pages.
ALLOWED_URL_SCHEMES = {"http", "https"}

# §3.2 editorial fact categories (stable keys, Russian labels live in the UI).
FACT_CATEGORIES = {
    "architecture",
    "creation_history",
    "historical_events",
    "notable_people",
    "cultural_significance",
    "interesting_details",
    "reconstructions",
}

# §3.3 dating discipline: an imprecise date label must never be stored as an
# exact one. ``sortKey`` is optional and free-form (year/period strings).
DATE_PRECISIONS = {"exact", "year", "period", "circa"}

# §3.4 person roles.
PERSON_ROLES = {
    "architect",
    "engineer",
    "sculptor",
    "founder",
    "patron",
    "historical_figure",
    "other",
}

# §3.5 source types (§5.1 source-quality priority lives in the docs).
SOURCE_TYPES = {
    "official_museum",
    "government_heritage",
    "academic",
    "official_reference",
    "encyclopedia",
    "other",
}

EDITORIAL_STATUSES = {"verified", "needs_review", "draft"}

_LOCALE_PATTERN = re.compile(r"^[a-z]{2}(-[A-Za-z]{2})?$")


def _validate_https_url(value: str, field: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in ALLOWED_URL_SCHEMES or not parsed.netloc:
        raise ValueError(
            f"{field}: source URL must be an absolute http(s) link, got {value!r}"
        )
    return value


def _validate_iso_date(value: str, field: str) -> str:
    try:
        date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field}: expected ISO-8601 date (YYYY-MM-DD), got {value!r}") from exc
    return value


def _validate_not_future_date(value: str, field: str) -> str:
    """§6 editorial discipline: verification dates cannot be in the future.

    A future ``lastVerifiedOn``/``accessedOn``/``generatedOn`` is a data-entry
    error, not a stylistic issue, so it is rejected at load time (fail-fast),
    not merely warned about in the coverage script.
    """
    parsed = date.fromisoformat(value)
    if parsed > date.today():
        raise ValueError(
            f"{field}: editorial date must not be in the future, got {value!r}"
        )
    return value


class HistoricalSource(BaseModel):
    """§3.5: a source backs specific facts, not the card as a whole."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, max_length=80)
    title: str = Field(..., min_length=1, max_length=300)
    url: str
    publisher: Optional[str] = Field(default=None, max_length=200)
    sourceType: str
    accessedOn: str
    license: Optional[str] = Field(default=None, max_length=120)
    notes: Optional[str] = Field(default=None, max_length=500)

    @field_validator("url")
    @classmethod
    def _url_is_safe(cls, v: str) -> str:
        return _validate_https_url(v, "sources.url")

    @field_validator("accessedOn")
    @classmethod
    def _accessed_on_is_iso_date(cls, v: str) -> str:
        _validate_iso_date(v, "sources.accessedOn")
        return _validate_not_future_date(v, "sources.accessedOn")

    @field_validator("sourceType")
    @classmethod
    def _known_source_type(cls, v: str) -> str:
        if v not in SOURCE_TYPES:
            raise ValueError(f"sources.sourceType: unknown type {v!r}")
        return v

    @field_validator("id", "title", "publisher", "license", "notes", mode="before")
    @classmethod
    def _strings_only(cls, v):
        if v is not None and not isinstance(v, str):
            raise ValueError("source text fields must be strings")
        return v


class HistoricalFact(BaseModel):
    """§3.2: one verifiable fact with an explicit source link."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, max_length=80)
    title: str = Field(..., min_length=1, max_length=200)
    text: str = Field(..., min_length=1, max_length=1200)
    category: str
    sourceIds: List[str] = Field(..., min_length=1)

    @field_validator("category")
    @classmethod
    def _known_category(cls, v: str) -> str:
        if v not in FACT_CATEGORIES:
            raise ValueError(f"facts.category: unknown category {v!r}")
        return v


class HistoricalTimelineEvent(BaseModel):
    """§3.3: a dated event; precision is stored next to the label."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, max_length=80)
    dateLabel: str = Field(..., min_length=1, max_length=120)
    datePrecision: str
    sortKey: Optional[str] = Field(default=None, max_length=40)
    title: str = Field(..., min_length=1, max_length=200)
    description: str = Field(..., min_length=1, max_length=1200)
    sourceIds: List[str] = Field(..., min_length=1)

    @field_validator("datePrecision")
    @classmethod
    def _known_precision(cls, v: str) -> str:
        if v not in DATE_PRECISIONS:
            raise ValueError(f"timeline.datePrecision: unknown precision {v!r}")
        return v


class HistoricalPerson(BaseModel):
    """§3.4: an attributed person — never an unconfirmed authorship."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=200)
    role: str
    description: Optional[str] = Field(default=None, max_length=800)
    sourceIds: List[str] = Field(..., min_length=1)

    @field_validator("role")
    @classmethod
    def _known_role(cls, v: str) -> str:
        if v not in PERSON_ROLES:
            raise ValueError(f"people.role: unknown role {v!r}")
        return v


class HistoricalPlaceContent(BaseModel):
    """§3.1: the historical card of one catalog object."""

    model_config = ConfigDict(extra="forbid")

    catalogKey: str = Field(..., min_length=1, max_length=80)
    locale: str = "ru"
    shortHistoricalSummary: str = Field(..., min_length=1, max_length=1200)
    detailedHistoricalText: str = Field(..., min_length=1, max_length=6000)
    period: Optional[str] = Field(default=None, max_length=120)
    timeline: List[HistoricalTimelineEvent] = Field(default_factory=list)
    people: List[HistoricalPerson] = Field(default_factory=list)
    facts: List[HistoricalFact] = Field(default_factory=list)
    sources: List[HistoricalSource] = Field(..., min_length=1)
    editorialStatus: str
    lastVerifiedOn: str
    contentVersion: int = Field(..., ge=1)

    @field_validator("catalogKey")
    @classmethod
    def _catalog_key_shape(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,79}", v):
            raise ValueError(f"catalogKey: invalid stable identity {v!r}")
        return v

    @field_validator("locale")
    @classmethod
    def _locale_shape(cls, v: str) -> str:
        if not _LOCALE_PATTERN.fullmatch(v):
            raise ValueError(f"locale: unexpected value {v!r}")
        return v

    @field_validator("editorialStatus")
    @classmethod
    def _known_status(cls, v: str) -> str:
        if v not in EDITORIAL_STATUSES:
            raise ValueError(f"editorialStatus: unknown status {v!r}")
        return v

    @field_validator("lastVerifiedOn")
    @classmethod
    def _verified_on_is_iso_date(cls, v: str) -> str:
        _validate_iso_date(v, "lastVerifiedOn")
        return _validate_not_future_date(v, "lastVerifiedOn")

    @field_validator(
        "shortHistoricalSummary", "detailedHistoricalText", "period", mode="before"
    )
    @classmethod
    def _strings_only(cls, v):
        if v is not None and not isinstance(v, str):
            raise ValueError("editorial text fields must be strings")
        return v

    @model_validator(mode="after")
    def _referential_integrity(self) -> "HistoricalPlaceContent":
        """§14: every fact/event/person must point at a source of THIS card."""
        source_ids = {source.id for source in self.sources}
        if len(source_ids) != len(self.sources):
            raise ValueError("sources: duplicate source ids inside one card")
        used: List[str] = []
        for fact in self.facts:
            used.extend(fact.sourceIds)
        for event in self.timeline:
            used.extend(event.sourceIds)
        for person in self.people:
            used.extend(person.sourceIds)
        unknown = sorted({sid for sid in used} - source_ids)
        if unknown:
            raise ValueError(
                f"referential integrity: facts/timeline/people reference unknown sources {unknown}"
            )
        unused = sorted(source_ids - set(used))
        if unused:
            raise ValueError(
                f"referential integrity: sources {unused} are not referenced by any fact/timeline/person"
            )
        return self


class HistoricalDatasetMeta(BaseModel):
    """Envelope metadata of the editorial dataset file."""

    model_config = ConfigDict(extra="forbid")

    version: str = Field(..., min_length=1, max_length=40)
    locale: str = "ru"
    generatedOn: str
    license: Optional[str] = Field(default=None, max_length=200)
    attribution: Optional[str] = Field(default=None, max_length=500)

    @field_validator("generatedOn")
    @classmethod
    def _generated_on_is_iso_date(cls, v: str) -> str:
        _validate_iso_date(v, "generatedOn")
        return _validate_not_future_date(v, "generatedOn")


class HistoricalDataset(BaseModel):
    """The whole place_history.json document."""

    model_config = ConfigDict(extra="forbid")

    meta: HistoricalDatasetMeta
    entries: List[HistoricalPlaceContent]


class PlaceHistoryResponse(BaseModel):
    """API response for ``GET /places/{place_id}/history``.

    ``history`` is null for places without an editorial historical card —
    the endpoint stays truthful without inventing content.
    """

    placeId: int
    catalogKey: Optional[str] = None
    history: Optional[HistoricalPlaceContent] = None
