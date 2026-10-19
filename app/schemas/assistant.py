"""Phase C13 private AI assistant request/response contract.

The Flutter client may only send text, bounded prior turns and an optional
catalog place id. It cannot select a model, send system/developer messages,
upload files/URLs or pass coordinates. Confirmed place references are built
server-side from the retrieval set, never trusted from the model.
"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.enums import PlaceCategory


class AssistantContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    place_id: int | None = Field(
        default=None, ge=1, le=10_000_000,
        description="Optional catalog place opened by the user before asking",
    )
    walk_context_enabled: bool = Field(
        default=False,
        description="The client explicitly enabled a walk-related conversation; no GPS is sent",
    )


class AssistantHistoryMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=800)

    @field_validator("content")
    @classmethod
    def _text_only(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("history content must not be blank")
        return value


class AssistantChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=800)
    history: list[AssistantHistoryMessage] = Field(default_factory=list, max_length=8)
    context: AssistantContext = Field(default_factory=AssistantContext)

    @field_validator("message")
    @classmethod
    def _message_only(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("message must not be blank")
        return value

    @model_validator(mode="after")
    def _bounded_dialogue(self):
        total = len(self.message) + sum(len(turn.content) for turn in self.history)
        if total > 4000:
            raise ValueError("conversation context is too large")
        return self


class AssistantPlaceReference(BaseModel):
    """A real catalog row selected by the server, never by the model."""

    id: int
    catalog_key: str | None = None
    title: str
    category: PlaceCategory
    is_unusual: bool
    short_description: str


class AssistantAction(BaseModel):
    type: Literal["open_smart_walk"]
    label: str


class AssistantUsage(BaseModel):
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    cost_verified: bool = False


class AssistantChatResponse(BaseModel):
    status: Literal["ok", "insufficient_data", "unavailable"]
    answer: str = Field(max_length=8000)
    model: str | None = None
    places: list[AssistantPlaceReference] = Field(default_factory=list)
    actions: list[AssistantAction] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    usage: AssistantUsage = Field(default_factory=AssistantUsage)


class AssistantQuotaStatus(BaseModel):
    global_minute_used: int
    global_minute_limit: int
    global_day_used: int
    global_day_limit: int
    source_minute_limit: int
    source_day_limit: int
    max_concurrent: int


class AssistantCapabilitiesResponse(BaseModel):
    """Minimal public availability data; no key, budget or operator settings."""

    assistant_available: bool
    consent_required: bool = True
    privacy_notice_version: str
    global_quota_exhausted: bool = False
    retry_after_seconds: int | None = Field(default=None, ge=1)


class AssistantStatusResponse(BaseModel):
    enabled: bool
    access_mode: Literal["private_test", "guest_limited", "disabled"]
    key_configured: bool
    primary_model: str
    reserve_model: str
    reserve_enabled: bool
    data_collection: Literal["allow", "deny"]
    free_only: bool
    model_check: Literal["ok", "not_checked", "failed"]
    model_check_reason: str | None = None
    key_status_reason: str | None = None
    key_limit_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    key_usage_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    key_remaining_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    key_free_tier: bool | None = None
    kill_switch_tripped: bool = False
    quota: AssistantQuotaStatus
    backup_available: bool = False
    backup_sha256: str | None = None
    backup_bytes: int | None = Field(default=None, ge=0)
