"""C13.1 assistant API: private operator mode and account-free guest mode.

`guest_limited` accepts chat requests without a client access code. It is not
client authentication: protection comes from durable server-side quotas, a
free-only model guard, a closed OpenRouter key budget and fail-closed behavior.
The operator status endpoint keeps its own SHA-256 code in every mode; that code
is never bundled into the APK. All model names, quota limits and privacy routing
stay server-side.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import math
import time
from collections import OrderedDict, deque
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_db
from app.schemas.assistant import (
    AssistantCapabilitiesResponse,
    AssistantChatRequest,
    AssistantChatResponse,
    AssistantStatusResponse,
)
from app.services.assistant.backup import backup_export_file
from app.services.assistant.context import AssistantContextService
from app.services.assistant.openrouter import OpenRouterClient
from app.services.assistant.quota import AssistantQuotaLedger
from app.services.assistant.service import AssistantService, AssistantUnavailable

router = APIRouter()

UNAVAILABLE = ("ИИ-помощник временно недоступен. "
               "Карта, поиск и навигация продолжают работать.")
TOO_MANY = "Слишком много запросов к ИИ-помощнику. Повторите позже."
PLACE_NOT_FOUND = "Место не найдено в каталоге. Откройте карточку заново."


class _AuthFailures:
    """Small in-memory brute-force brake; no raw address is ever logged."""

    def __init__(self):
        self._entries: OrderedDict[str, deque[float]] = OrderedDict()

    def _prune(self, values: deque[float], now: float) -> None:
        window = settings.AI_ASSISTANT_PRIVATE_TEST_FAILURE_WINDOW_SECONDS
        while values and values[0] <= now - window:
            values.popleft()

    def blocked(self, source: str) -> int | None:
        now = time.monotonic()
        values = self._entries.get(source)
        if values is None:
            return None
        self._prune(values, now)
        if not values:
            self._entries.pop(source, None)
            return None
        if len(values) >= settings.AI_ASSISTANT_PRIVATE_TEST_MAX_FAILURES:
            return max(1, int(values[0] + settings.AI_ASSISTANT_PRIVATE_TEST_FAILURE_WINDOW_SECONDS - now))
        return None

    def fail(self, source: str) -> None:
        now = time.monotonic()
        values = self._entries.get(source)
        if values is None:
            if len(self._entries) >= 4096:
                self._entries.popitem(last=False)
            values = deque()
            self._entries[source] = values
        self._prune(values, now)
        values.append(now)

    def succeed(self, source: str) -> None:
        self._entries.pop(source, None)


class _InboundLimiter:
    """Very cheap in-process pre-limit before the catalog and OpenRouter.

    Counters live only in this worker and reset on restart. They are a first
    line of defence, not the authoritative quota: every upstream attempt is
    still reserved in SQLite by `AssistantQuotaLedger`.
    """

    def __init__(self):
        self._global: deque[float] = deque()
        self._entries: OrderedDict[str, deque[float]] = OrderedDict()

    @staticmethod
    def _prune(values: deque[float], now: float, window: float) -> None:
        while values and values[0] <= now - window:
            values.popleft()

    def check(
        self,
        key: str,
        *,
        per_minute: int,
        global_per_minute: int,
        window: float = 60.0,
    ) -> int | None:
        now = time.monotonic()
        self._prune(self._global, now, window)
        if len(self._global) >= global_per_minute:
            return max(1, math.ceil(self._global[0] + window - now))
        values = self._entries.get(key)
        if values is None:
            if len(self._entries) >= 4096:
                self._entries.popitem(last=False)
            values = deque()
            self._entries[key] = values
        else:
            self._entries.move_to_end(key)
        self._prune(values, now, window)
        if len(values) >= per_minute:
            return max(1, math.ceil(values[0] + window - now))
        self._global.append(now)
        values.append(now)
        return None

    def reset(self) -> None:
        self._global.clear()
        self._entries.clear()


_failures = _AuthFailures()
_chat_inbound = _InboundLimiter()
_capabilities_inbound = _InboundLimiter()
_service: AssistantService | None = None


def get_assistant_service() -> AssistantService:
    global _service
    if _service is None:
        _service = AssistantService(
            client=OpenRouterClient(),
            quota=AssistantQuotaLedger(),
            context=AssistantContextService(),
        )
    return _service


async def close_assistant_service() -> None:
    global _service
    if _service is not None:
        service, _service = _service, None
        await service.close()


def _client_ip(request: Request) -> str:
    peer = request.client.host if request.client and request.client.host else "unknown"
    hops = int(settings.AI_ASSISTANT_TRUSTED_PROXY_HOPS)
    if hops <= 0:
        return peer
    forwarded = request.headers.get("x-forwarded-for")
    if not forwarded:
        return peer
    parts = [part.strip() for part in forwarded.split(",") if part.strip()]
    if len(parts) < hops:
        return peer
    # Railway's rightmost entry is its rotating edge, not the visitor. With
    # two trusted hops the selected entry is the client address the edge
    # recorded; client-supplied left-hand entries are never selected.
    candidate = parts[-hops]
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return peer
    return candidate


def _source_hash(ip: str) -> str:
    # The raw address is not stored. A dedicated stable salt lets the operator
    # code rotate without resetting per-source quota continuity.
    salt = (settings.AI_ASSISTANT_SOURCE_SALT
            or settings.AI_ASSISTANT_ACCESS_CODE_HASH
            or "no-code")
    return hashlib.sha256(f"{ip}|{salt}".encode("utf-8")).hexdigest()[:32]


def _rate_key(ip: str) -> str:
    return hashlib.sha256(f"inbound|{ip}".encode("utf-8")).hexdigest()[:32]


def _check_private_code(request: Request, ip: str) -> None:
    """Constant-time operator code with a small failure brake."""
    blocked = _failures.blocked(ip)
    if blocked is not None:
        raise HTTPException(429, TOO_MANY, headers={"Retry-After": str(blocked)})
    expected = settings.AI_ASSISTANT_ACCESS_CODE_HASH
    if not expected:
        raise HTTPException(503, UNAVAILABLE)
    code = request.headers.get("x-assistant-access-code")
    if not code:
        _failures.fail(ip)
        raise HTTPException(401, "Требуется код доступа оператора.")
    code = code.strip()
    if not 8 <= len(code) <= 256:
        _failures.fail(ip)
        raise HTTPException(401, "Требуется код доступа оператора.")
    digest = hashlib.sha256(code.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(digest, expected):
        _failures.fail(ip)
        raise HTTPException(401, "Требуется код доступа оператора.")
    _failures.succeed(ip)


async def _require_chat_access(request: Request) -> str:
    mode = settings.AI_ASSISTANT_ACCESS_MODE
    if mode not in ("private_test", "guest_limited"):
        raise HTTPException(503, UNAVAILABLE)
    ip = _client_ip(request)
    if mode == "private_test":
        _check_private_code(request, ip)
    retry_after = _chat_inbound.check(
        _rate_key(ip),
        per_minute=int(settings.AI_ASSISTANT_INBOUND_CHAT_PER_MINUTE),
        global_per_minute=int(
            settings.AI_ASSISTANT_INBOUND_CHAT_GLOBAL_PER_MINUTE),
    )
    if retry_after is not None:
        raise HTTPException(
            429, TOO_MANY, headers={"Retry-After": str(retry_after)})
    return ip


async def _require_operator_access(request: Request) -> str:
    """The diagnostics endpoint stays code-protected in every active mode."""
    if settings.AI_ASSISTANT_ACCESS_MODE not in (
            "private_test", "guest_limited"):
        raise HTTPException(503, UNAVAILABLE)
    ip = _client_ip(request)
    _check_private_code(request, ip)
    return ip


async def _capabilities_guard(request: Request) -> None:
    retry_after = _capabilities_inbound.check(
        _rate_key(_client_ip(request)),
        per_minute=int(settings.AI_ASSISTANT_INBOUND_CAPABILITIES_PER_MINUTE),
        global_per_minute=int(
            settings.AI_ASSISTANT_INBOUND_CAPABILITIES_GLOBAL_PER_MINUTE),
    )
    if retry_after is not None:
        raise HTTPException(
            429, TOO_MANY, headers={"Retry-After": str(retry_after)})


def _as_http(error: AssistantUnavailable) -> HTTPException:
    headers = {"Retry-After": str(error.retry_after)} if error.retry_after else None
    if error.code == "invalid_place":
        return HTTPException(422, PLACE_NOT_FOUND)
    if error.code == "history_too_long":
        return HTTPException(422, "Диалог слишком длинный. Очистите беседу и попробуйте снова.")
    if error.code in ("busy", "quota_exceeded"):
        return HTTPException(429, TOO_MANY, headers=headers)
    return HTTPException(
        max(400, min(error.status_code, 503)), UNAVAILABLE, headers=headers)


@router.post(
    "/chat",
    response_model=AssistantChatResponse,
    summary="Встроенный ИИ-помощник (guest_limited или private_test)",
)
async def assistant_chat(
    payload: AssistantChatRequest,
    source_ip: str = Depends(_require_chat_access),
    db: AsyncSession = Depends(get_db),
):
    service = get_assistant_service()
    try:
        return await service.chat(db, payload, source_hash=_source_hash(source_ip))
    except AssistantUnavailable as error:
        raise _as_http(error) from None


@router.post(
    "/chat/stream",
    summary="Потоковый встроенный ИИ-помощник (SSE)",
    response_class=StreamingResponse,
)
async def assistant_chat_stream(
    payload: AssistantChatRequest,
    source_ip: str = Depends(_require_chat_access),
    db: AsyncSession = Depends(get_db),
):
    """Typed SSE: `started`, `delta`, `completed` or `error`.

    All validation, grounding, model verification and the durable reservation
    happen before the 200 response starts, so quota and access failures keep
    their normal HTTP status codes. Text fragments are provisional until the
    final `completed` event confirms the model, zero cost and completion.
    """
    service = get_assistant_service()
    try:
        lease = await service.prepare_stream(
            db, payload, source_hash=_source_hash(source_ip))
    except AssistantUnavailable as error:
        raise _as_http(error) from None

    async def body() -> AsyncIterator[bytes]:
        try:
            async for item in service.stream(lease):
                event = item["event"]
                data = json.dumps(
                    item["data"], ensure_ascii=False, separators=(",", ":"))
                yield f"event: {event}\ndata: {data}\n\n".encode("utf-8")
        finally:
            # Idempotent: settles a cancelled reservation and releases the
            # concurrency slot if the client disconnected mid-stream.
            await service.abandon_stream(lease)

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "X-Content-Type-Options": "nosniff",
            "Connection": "keep-alive",
        },
    )


@router.get(
    "/capabilities",
    response_model=AssistantCapabilitiesResponse,
    summary="Публичная доступность гостевого ИИ-помощника",
)
async def assistant_capabilities(
    response: Response,
    _guard: None = Depends(_capabilities_guard),
):
    service = get_assistant_service()
    try:
        result = await service.capabilities()
    except AssistantUnavailable as error:
        raise _as_http(error) from None
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get(
    "/status",
    response_model=AssistantStatusResponse,
    summary="Безопасный операторский статус ИИ-помощника",
)
async def assistant_status(_source_ip: str = Depends(_require_operator_access)):
    service = get_assistant_service()
    try:
        return await service.status()
    except AssistantUnavailable as error:
        raise _as_http(error) from None


async def _require_export_token(request: Request) -> None:
    """Temporary operator-only export; disabled after independent verification."""
    if not settings.AI_ASSISTANT_EXPORT_ENABLED:
        raise HTTPException(404, "Not found")
    expected = settings.AI_ASSISTANT_EXPORT_TOKEN_HASH
    if not expected:
        raise HTTPException(404, "Not found")
    token = request.headers.get("x-backup-token")
    if not token:
        raise HTTPException(401, "Authorization required")
    digest = hashlib.sha256(token.strip().encode("utf-8")).hexdigest()
    if not hmac.compare_digest(digest, expected):
        raise HTTPException(401, "Authorization required")


@router.get(
    "/backup/export",
    include_in_schema=False,
    summary="Temporary verified backup export",
)
async def export_backup(_token: None = Depends(_require_export_token)):
    item = backup_export_file()
    if item is None:
        raise HTTPException(404, "Backup is not available")
    path, evidence = item
    return FileResponse(
        path,
        media_type="application/vnd.sqlite3",
        filename=evidence["file"],
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'",
        },
    )
