import re
from ipaddress import ip_network
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    PROJECT_NAME: str = "SmartNavigator SPb API"
    VERSION: str = "1.0.0"
    API_V1_STR: str = "/api/v1"
    BACKEND_ENV: Literal["development", "staging", "production"] = "development"
    HOST: str = "0.0.0.0"
    PORT: int = Field(default=8000, ge=1, le=65535)
    # Catalog synchronization is a single-writer startup operation.
    WORKERS: int = Field(default=1, ge=1, le=1)
    RELOAD: bool = False
    LOG_LEVEL: Literal["critical", "error", "warning", "info", "debug"] = "info"
    TRUSTED_PROXY_IPS: str = ""
    SERVER_CONCURRENCY_LIMIT: int = Field(default=100, ge=1, le=10000)
    REQUEST_MAX_BYTES: int = Field(default=16384, ge=1024, le=1048576)
    REQUEST_BODY_TIMEOUT_SECONDS: float = Field(default=10, gt=0, le=60, allow_inf_nan=False)
    QUERY_MAX_BYTES: int = Field(default=2048, ge=256, le=16384)
    SEARCH_CLIENT_RATE_LIMIT: int = Field(default=30, ge=1, le=10000)
    SEARCH_CLIENT_WINDOW_SECONDS: int = Field(default=60, ge=1, le=86400)
    SEARCH_CLIENT_MAX_ENTRIES: int = Field(default=4096, ge=1, le=100000)
    READINESS_TIMEOUT_SECONDS: float = Field(default=2, gt=0, le=30, allow_inf_nan=False)
    DATABASE_URL: str = Field(default="sqlite+aiosqlite:///./smart_navigator_spb.db", repr=False)
    # Android does not need browser CORS; opt in to exact web origins.
    ALLOWED_ORIGINS: list[str] = Field(default_factory=list)
    MAPKIT_API_KEY: str | None = Field(default=None, repr=False)  # Legacy, unused.
    GEOCODER_API_KEY: str | None = Field(default=None, repr=False)
    SUGGEST_API_KEY: str | None = Field(default=None, repr=False)
    YANDEX_SUGGEST_URL: str = "https://suggest-maps.yandex.ru/v1/suggest"
    YANDEX_HTTP_REFERER: str = "http://ru.smartnavigator.spb.ymapapp"
    YANDEX_GEOCODER_URL: str = "https://geocode-maps.yandex.ru/v1/"
    YANDEX_TIMEOUT_SECONDS: float = Field(default=5, gt=0, le=60, allow_inf_nan=False)
    YANDEX_CONNECT_TIMEOUT_SECONDS: float = Field(default=3, gt=0, le=60, allow_inf_nan=False)
    YANDEX_MAX_CONCURRENT: int = Field(default=4, ge=1, le=64)
    YANDEX_SERVER_RATE_LIMIT: int = Field(default=60, ge=1, le=10000)
    YANDEX_RATE_WINDOW_SECONDS: int = Field(default=60, ge=1, le=86400)
    YANDEX_SUGGEST_DAILY_BUDGET: int = Field(default=800, ge=0, le=100000)
    YANDEX_GEOCODER_DAILY_BUDGET: int = Field(default=800, ge=0, le=100000)
    SPB_CENTER_LON: float = Field(default=30.3146, ge=-180, le=180, allow_inf_nan=False)
    SPB_CENTER_LAT: float = Field(default=59.9398, ge=-90, le=90, allow_inf_nan=False)
    SPB_SPN_LON: float = Field(default=0.9, gt=0, le=360, allow_inf_nan=False)
    SPB_SPN_LAT: float = Field(default=0.7, gt=0, le=180, allow_inf_nan=False)
    # Phase C7.1 Shade Engine: read-only pilot building dataset (SQLite).
    # Relative paths resolve against the backend root; an empty value disables
    # the dataset so the engine honestly reports "unavailable" instead of guessing.
    SHADE_BUILDING_DATASET: str = "data/shade_buildings_spb_pilot.sqlite"
    SHADE_MAX_CONCURRENT_ANALYSES: int = Field(default=2, ge=1, le=16)
    SHADE_ANALYSIS_TIMEOUT_SECONDS: float = Field(default=10, gt=0, le=60, allow_inf_nan=False)
    # Phase C7.2 Puddle Risk: read-only pilot OSM ways snapshot (JSON, ODbL).
    # Relative paths resolve against the backend root; an empty value disables
    # the dataset so the engine honestly reports "unavailable" instead of guessing.
    PUDDLE_WAYS_DATASET: str = "data/puddle_ways_spb_pilot.json"
    PUDDLE_MAX_CONCURRENT_ANALYSES: int = Field(default=2, ge=1, le=16)
    PUDDLE_ANALYSIS_TIMEOUT_SECONDS: float = Field(default=10, gt=0, le=60, allow_inf_nan=False)
    # Modeled weather (Open-Meteo) is a separate, explicit operator opt-in:
    # free-tier, non-commercial conditions apply (see PHASE_C7_2_DATA_PROVENANCE.md).
    PUDDLE_WEATHER_ENABLED: bool = False
    PUDDLE_WEATHER_TIMEOUT_SECONDS: float = Field(default=6, gt=0, le=30, allow_inf_nan=False)
    # One fixed pilot centroid; the user's route is NEVER sent upstream, so the
    # provider stays spatially independent of where the user actually walks.
    PUDDLE_WEATHER_LATITUDE: float = Field(default=59.9405, ge=-90, le=90, allow_inf_nan=False)
    PUDDLE_WEATHER_LONGITUDE: float = Field(default=30.3165, ge=-180, le=180, allow_inf_nan=False)
    # Phase C7.3 Pollen Exposure: modeled CAMS pollen via Open-Meteo Air Quality.
    # The free tier is non-commercial and requires attribution (see
    # PHASE_C7_3_SOURCE_FEASIBILITY.md), so the source is an explicit opt-in.
    POLLEN_MAX_CONCURRENT_ANALYSES: int = Field(default=2, ge=1, le=16)
    POLLEN_ANALYSIS_TIMEOUT_SECONDS: float = Field(default=10, gt=0, le=60, allow_inf_nan=False)
    POLLEN_SOURCE_ENABLED: bool = False
    POLLEN_SOURCE_TIMEOUT_SECONDS: float = Field(default=10, gt=0, le=30, allow_inf_nan=False)
    # One fixed pilot centroid; the user's route is NEVER sent upstream. The
    # fetched value describes exactly ONE 0.1° CAMS cell around this point.
    POLLEN_SOURCE_LATITUDE: float = Field(default=59.9405, ge=-90, le=90, allow_inf_nan=False)
    POLLEN_SOURCE_LONGITUDE: float = Field(default=30.3165, ge=-180, le=180, allow_inf_nan=False)
    # Phase C11 Quiet Engine: read-only central-SPb OSM road snapshot (JSON.gz)
    # plus a SHA-256 manifest. Both are packaged with the image; no runtime
    # download. The endpoint reports an honest unavailable status until the
    # operator explicitly enables the verified dataset.
    QUIET_ENGINE_ENABLED: bool = False
    QUIET_DATASET: str = "data/quiet_spb_pilot.json.gz"
    QUIET_DATASET_MANIFEST: str = "data/quiet_spb_pilot.manifest.json"
    QUIET_MAX_CONCURRENT_ANALYSES: int = Field(default=2, ge=1, le=16)
    QUIET_ANALYSIS_TIMEOUT_SECONDS: float = Field(default=10, gt=0, le=60, allow_inf_nan=False)
    # Phase C13: private, free-only OpenRouter text assistant. Phase C13.1 adds
    # `guest_limited`: no access code on the chat path, but the same durable
    # quota ledger, free-only guards and a server-side API key. Guest mode is
    # never the default; an operator must select it explicitly.
    OPENROUTER_API_KEY: str | None = Field(default=None, repr=False)
    AI_ASSISTANT_ENABLED: bool = False
    AI_ASSISTANT_ACCESS_MODE: Literal[
        "private_test", "guest_limited", "disabled"] = "private_test"
    AI_ASSISTANT_ACCESS_CODE_HASH: str | None = Field(default=None, repr=False)
    # Stable, opaque salt for per-source quota hashes. Keeping it separate from
    # the operator code lets the code rotate without resetting source quotas.
    AI_ASSISTANT_SOURCE_SALT: str | None = Field(default=None, repr=False)
    AI_ASSISTANT_PRIVACY_NOTICE_VERSION: str = "smartnavigator.ai_consent.v1"
    AI_ASSISTANT_MODEL_PRIMARY: str = "nvidia/nemotron-3-super-120b-a12b:free"
    AI_ASSISTANT_MODEL_RESERVE: str = "nvidia/nemotron-3.5-super-vl:free"
    AI_ASSISTANT_RESERVE_ENABLED: bool = False
    AI_ASSISTANT_FREE_ONLY: bool = True
    AI_ASSISTANT_DATA_COLLECTION: Literal["allow", "deny"] = "deny"
    AI_ASSISTANT_DATA_COLLECTION_ACK: bool = False
    AI_ASSISTANT_OPENROUTER_CHAT_URL: str = "https://openrouter.ai/api/v1/chat/completions"
    AI_ASSISTANT_OPENROUTER_MODELS_URL: str = "https://openrouter.ai/api/v1/models"
    AI_ASSISTANT_OPENROUTER_KEY_URL: str = "https://openrouter.ai/api/v1/key"
    AI_ASSISTANT_TIMEOUT_SECONDS: float = Field(default=25, gt=0, le=60, allow_inf_nan=False)
    AI_ASSISTANT_CONNECT_TIMEOUT_SECONDS: float = Field(
        default=5, gt=0, le=15, allow_inf_nan=False)
    AI_ASSISTANT_MODEL_CACHE_SECONDS: int = Field(default=600, ge=30, le=86400)
    AI_ASSISTANT_MAX_OUTPUT_TOKENS: int = Field(default=450, ge=64, le=1024)
    AI_ASSISTANT_MAX_MESSAGE_CHARS: int = Field(default=800, ge=50, le=2000)
    AI_ASSISTANT_MAX_HISTORY_MESSAGES: int = Field(default=8, ge=0, le=20)
    AI_ASSISTANT_MAX_HISTORY_CHARS: int = Field(default=4000, ge=0, le=20000)
    AI_ASSISTANT_MAX_CONTEXT_CHARS: int = Field(default=6000, ge=1000, le=20000)
    AI_ASSISTANT_MAX_RESPONSE_CHARS: int = Field(default=8000, ge=500, le=20000)
    AI_ASSISTANT_MAX_RESPONSE_BYTES: int = Field(default=262144, ge=4096, le=1048576)
    # C13.2 SSE endpoint. Disabling it leaves the non-streaming `/chat` intact.
    AI_ASSISTANT_STREAMING_ENABLED: bool = True
    AI_ASSISTANT_STREAM_MAX_EVENT_BYTES: int = Field(
        default=65536, ge=4096, le=262144)
    AI_ASSISTANT_GLOBAL_PER_MINUTE: int = Field(default=8, ge=1, le=100)
    AI_ASSISTANT_GLOBAL_PER_DAY: int = Field(default=100, ge=1, le=1000)
    AI_ASSISTANT_SOURCE_PER_MINUTE: int = Field(default=3, ge=1, le=100)
    AI_ASSISTANT_SOURCE_PER_DAY: int = Field(default=20, ge=1, le=1000)
    AI_ASSISTANT_MAX_CONCURRENT: int = Field(default=2, ge=1, le=8)
    AI_ASSISTANT_QUOTA_RETENTION_SECONDS: int = Field(
        default=8 * 24 * 60 * 60, ge=24 * 60 * 60, le=90 * 24 * 60 * 60)
    # Cheap in-process pre-limit in front of the catalog and OpenRouter. It is
    # intentionally below the durable ledger's role: a restart resets these
    # counters, while every upstream attempt remains durably counted.
    AI_ASSISTANT_INBOUND_CHAT_PER_MINUTE: int = Field(default=10, ge=1, le=100)
    AI_ASSISTANT_INBOUND_CHAT_GLOBAL_PER_MINUTE: int = Field(
        default=60, ge=1, le=1000)
    AI_ASSISTANT_INBOUND_CAPABILITIES_PER_MINUTE: int = Field(
        default=30, ge=1, le=300)
    AI_ASSISTANT_INBOUND_CAPABILITIES_GLOBAL_PER_MINUTE: int = Field(
        default=120, ge=1, le=2000)
    AI_ASSISTANT_PRIVATE_TEST_MAX_FAILURES: int = Field(default=10, ge=1, le=100)
    AI_ASSISTANT_PRIVATE_TEST_FAILURE_WINDOW_SECONDS: int = Field(
        default=600, ge=60, le=3600)
    # Railway replaces client-supplied X-Forwarded-For with exactly
    # "<client>, <railway-edge>". The edge address rotates per request, so a
    # one-hop key would collapse or randomize source quotas. Count two hops
    # from the right and use the client address; a client cannot spoof it
    # because the edge replaces the header. The hard cap remains global.
    AI_ASSISTANT_TRUSTED_PROXY_HOPS: int = Field(default=2, ge=0, le=2)
    # Railway SSH/SFTP is not always reachable from the operator workstation.
    # A startup online backup runs BEFORE any C13 schema is created; this is
    # the same tested stdlib script used by the C11 deployment.
    AI_ASSISTANT_BACKUP_ON_START: bool = False
    AI_ASSISTANT_BACKUP_LABEL: str = "predeploy-c13"
    # Temporary, token-gated export of the verified backup. Disabled after the
    # operator has downloaded and independently verified the file.
    AI_ASSISTANT_EXPORT_ENABLED: bool = False
    AI_ASSISTANT_EXPORT_TOKEN_HASH: str | None = Field(default=None, repr=False)

    model_config = SettingsConfigDict(
        case_sensitive=True, env_file=".env", env_file_encoding="utf-8",
        extra="ignore", hide_input_in_errors=True,
    )

    @field_validator("GEOCODER_API_KEY", "SUGGEST_API_KEY", "MAPKIT_API_KEY",
                     "OPENROUTER_API_KEY")
    @classmethod
    def optional_key(cls, value):
        value = (value.strip() or None) if value else None
        if value and value.upper().startswith(("YOUR_", "REPLACE_", "PLACEHOLDER")):
            return None
        return value

    @field_validator("DATABASE_URL")
    @classmethod
    def sqlite_url(cls, value):
        try:
            url = make_url(value)
        except Exception:
            raise ValueError("DATABASE_URL must be a SQLite aiosqlite URL") from None
        if (url.drivername != "sqlite+aiosqlite" or not url.database or url.query
                or url.username or url.password or url.host or url.port):
            raise ValueError("DATABASE_URL must be a SQLite aiosqlite URL without query options")
        return value

    @field_validator("TRUSTED_PROXY_IPS")
    @classmethod
    def trusted_proxies(cls, value):
        entries = [entry.strip() for entry in value.split(",") if entry.strip()]
        try:
            for entry in entries:
                network = ip_network(entry, strict=False)
                if network.prefixlen == 0:
                    raise ValueError()
        except ValueError:
            raise ValueError("TRUSTED_PROXY_IPS requires explicit IPs or bounded CIDRs") from None
        return ",".join(entries)

    @field_validator("ALLOWED_ORIGINS")
    @classmethod
    def origins(cls, values):
        for value in values:
            parts = urlsplit(value)
            if (parts.scheme not in ("http", "https") or not parts.netloc
                    or parts.username or parts.password or parts.query or parts.fragment
                    or parts.path not in ("", "/") or "*" in value):
                raise ValueError("ALLOWED_ORIGINS requires exact HTTP(S) origins")
        return [value.rstrip("/") for value in values]

    @field_validator("YANDEX_SUGGEST_URL", "YANDEX_GEOCODER_URL")
    @classmethod
    def upstream_url(cls, value, info):
        expected = {
            "YANDEX_SUGGEST_URL": ("suggest-maps.yandex.ru", "/v1/suggest"),
            "YANDEX_GEOCODER_URL": ("geocode-maps.yandex.ru", "/v1/"),
        }[info.field_name]
        parts = urlsplit(value)
        if (parts.scheme != "https" or (parts.hostname, parts.path) != expected
                or parts.port not in (None, 443) or parts.username or parts.password
                or parts.query or parts.fragment):
            raise ValueError("Yandex URL must use the supported official HTTPS endpoint")
        return value

    @field_validator("AI_ASSISTANT_ACCESS_CODE_HASH",
                     "AI_ASSISTANT_SOURCE_SALT",
                     "AI_ASSISTANT_EXPORT_TOKEN_HASH")
    @classmethod
    def assistant_sha256_hash(cls, value):
        value = (value.strip().lower() or None) if value else None
        if value is not None and not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("AI assistant secrets must be stored as lowercase SHA-256 hashes")
        return value

    @field_validator("AI_ASSISTANT_PRIVACY_NOTICE_VERSION")
    @classmethod
    def assistant_notice_version(cls, value):
        value = value.strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{7,63}", value):
            raise ValueError("AI assistant privacy notice version must be a short lowercase id")
        return value

    @field_validator("AI_ASSISTANT_MODEL_PRIMARY", "AI_ASSISTANT_MODEL_RESERVE")
    @classmethod
    def assistant_free_model(cls, value):
        value = value.strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*/[A-Za-z0-9._:-]+:free", value):
            raise ValueError("AI assistant models must be explicit OpenRouter IDs ending in :free")
        return value

    @field_validator("AI_ASSISTANT_OPENROUTER_CHAT_URL",
                     "AI_ASSISTANT_OPENROUTER_MODELS_URL",
                     "AI_ASSISTANT_OPENROUTER_KEY_URL")
    @classmethod
    def assistant_openrouter_url(cls, value, info):
        expected = {
            "AI_ASSISTANT_OPENROUTER_CHAT_URL": "/api/v1/chat/completions",
            "AI_ASSISTANT_OPENROUTER_MODELS_URL": "/api/v1/models",
            "AI_ASSISTANT_OPENROUTER_KEY_URL": "/api/v1/key",
        }[info.field_name]
        parts = urlsplit(value)
        if (parts.scheme != "https" or parts.hostname != "openrouter.ai"
                or parts.path != expected or parts.port not in (None, 443)
                or parts.username or parts.password
                or parts.query or parts.fragment):
            raise ValueError("AI assistant must use the official OpenRouter HTTPS endpoint")
        return value

    @field_validator("AI_ASSISTANT_BACKUP_LABEL")
    @classmethod
    def assistant_backup_label(cls, value):
        value = value.strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,40}", value):
            raise ValueError("AI assistant backup label must be a short lowercase slug")
        return value

    @model_validator(mode="after")
    def deployment_mode(self):
        if self.BACKEND_ENV != "development":
            if self.RELOAD:
                raise ValueError("RELOAD is allowed only in development")
            if make_url(self.DATABASE_URL).database == ":memory:":
                raise ValueError("Deployment requires persistent file SQLite storage")
        return self

    @model_validator(mode="after")
    def assistant_guardrails(self):
        # Paid routing is never an option, in any environment.
        if not self.AI_ASSISTANT_FREE_ONLY:
            raise ValueError("AI_ASSISTANT_FREE_ONLY cannot be disabled")
        if (self.AI_ASSISTANT_DATA_COLLECTION == "allow"
                and not self.AI_ASSISTANT_DATA_COLLECTION_ACK):
            raise ValueError(
                "AI_ASSISTANT_DATA_COLLECTION=allow requires "
                "AI_ASSISTANT_DATA_COLLECTION_ACK=true")
        if self.AI_ASSISTANT_ENABLED and self.AI_ASSISTANT_ACCESS_MODE == "disabled":
            raise ValueError("AI_ASSISTANT_ENABLED requires an active access mode")
        if self.AI_ASSISTANT_ENABLED and not self.AI_ASSISTANT_ACCESS_CODE_HASH:
            # The operator status stays protected in every mode, so the code
            # hash is required even when guests chat without it.
            raise ValueError("Enabled assistant requires an operator access code hash")
        if (self.AI_ASSISTANT_ENABLED
                and self.AI_ASSISTANT_ACCESS_MODE == "guest_limited"):
            if self.AI_ASSISTANT_DATA_COLLECTION != "allow":
                raise ValueError(
                    "guest_limited requires AI_ASSISTANT_DATA_COLLECTION=allow")
            if not self.AI_ASSISTANT_SOURCE_SALT:
                raise ValueError(
                    "guest_limited requires AI_ASSISTANT_SOURCE_SALT")
        if self.AI_ASSISTANT_EXPORT_ENABLED and not self.AI_ASSISTANT_EXPORT_TOKEN_HASH:
            raise ValueError("Backup export requires an export token hash")
        return self


settings = Settings()
