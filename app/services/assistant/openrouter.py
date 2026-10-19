"""Strict free-only OpenRouter client for the C13 assistant.

Every call is preceded by a server-side model allowlist check and a live model
metadata check: the model id must end in `:free`, prompt and completion prices
must be explicitly zero, and every other present price category must be zero.
The request also carries `provider.max_price` zeros, disables model/provider
fallbacks and never sends a model id chosen by the client. The response
`model` field must exactly match the requested model id; any other actual
model is rejected instead of being reported as if it were the requested one.

No prompt, answer, API key or provider error body is logged or included in an
exception returned to the app. Only short error codes and safe numeric usage
statistics leave this module.
"""
from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.core.config import settings

FREE_SUFFIX = ":free"
MAX_MODEL_CATALOG_BYTES = 4 * 1024 * 1024
MAX_ERROR_BODY_BYTES = 2048
PRICE_KEYS = {
    "prompt", "completion", "request", "image", "web_search",
    "internal_reasoning", "input_cache_read", "input_cache_write",
}


class OpenRouterError(Exception):
    """Safe failure with a stable code; no upstream text is retained."""

    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
        status_code: int | None = None,
        retry_after: float | None = None,
        cost_usd: float | None = None,
    ):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.status_code = status_code
        self.retry_after = retry_after
        self.cost_usd = cost_usd


@dataclass(frozen=True)
class FreeModelInfo:
    model_id: str
    prompt_price: Decimal
    completion_price: Decimal
    checked_at: float


@dataclass(frozen=True)
class ChatResult:
    content: str
    model_id: str
    prompt_tokens: int | None
    completion_tokens: int | None
    cost_usd: float | None
    cost_verified: bool
    finish_reason: str | None
    truncated: bool


@dataclass(frozen=True)
class StreamDelta:
    """One incremental upstream text fragment.

    It is provisional: the answer may only be confirmed after the stream
    completes with a matching model and verified zero cost.
    """

    text: str
    model_id: str


@dataclass(frozen=True)
class StreamSummary:
    """Final, verified upstream metadata for a completed SSE stream."""

    model_id: str
    prompt_tokens: int | None
    completion_tokens: int | None
    cost_usd: float | None
    cost_verified: bool
    finish_reason: str | None
    truncated: bool


def _zero_price(value: Any, field: str) -> Decimal:
    if value is None:
        raise OpenRouterError("model_price_missing")
    if isinstance(value, bool):
        raise OpenRouterError("model_price_invalid")
    try:
        price = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise OpenRouterError("model_price_invalid") from None
    if not price.is_finite() or price < 0:
        raise OpenRouterError("model_price_invalid")
    if field in {"prompt", "completion"} and price != 0:
        raise OpenRouterError("model_is_paid")
    if price != 0:
        raise OpenRouterError("model_is_paid")
    return price


def validate_free_pricing(pricing: Any) -> tuple[Decimal, Decimal]:
    if not isinstance(pricing, dict):
        raise OpenRouterError("model_price_missing")
    # Unknown positive categories are treated as paid as well; a provider can
    # add a new billable category without warning.
    prompt = _zero_price(pricing.get("prompt"), "prompt")
    completion = _zero_price(pricing.get("completion"), "completion")
    for key, value in pricing.items():
        if key in {"prompt", "completion"}:
            continue
        if value is None:
            continue
        _zero_price(value, key if isinstance(key, str) else "unknown")
    return prompt, completion


def _cut_to_sentence(content: str) -> str:
    """Avoid showing a half-word answer when the provider hit its token cap."""
    cut = max(content.rfind(mark) for mark in ".!?\n")
    if cut >= 80 and cut >= len(content) // 3:
        return content[: cut + 1].strip()
    return content.strip()


class OpenRouterClient:
    def __init__(
        self,
        config: Any | None = None,
        *,
        clock: Callable[[], float] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._settings = config or settings
        self._clock = clock or time.time
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()
        self._model_lock = asyncio.Lock()
        self._models: dict[str, tuple[FreeModelInfo, float]] = {}
        self._closing = False

    async def _get_client(self) -> httpx.AsyncClient:
        if self._closing:
            raise OpenRouterError("shutting_down")
        if self._client is None or self._client.is_closed:
            async with self._client_lock:
                if self._closing:
                    raise OpenRouterError("shutting_down")
                if self._client is None or self._client.is_closed:
                    self._client = httpx.AsyncClient(
                        timeout=httpx.Timeout(
                            float(self._settings.AI_ASSISTANT_TIMEOUT_SECONDS),
                            connect=float(self._settings.AI_ASSISTANT_CONNECT_TIMEOUT_SECONDS),
                        ),
                        limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
                        trust_env=False,
                        follow_redirects=False,
                        transport=self._transport,
                    )
        return self._client

    async def close(self) -> None:
        self._closing = True
        async with self._client_lock:
            client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    @staticmethod
    async def _read_limited(response: httpx.Response, limit: int) -> bytes:
        data = bytearray()
        async for chunk in response.aiter_bytes():
            if len(data) + len(chunk) > limit:
                raise OpenRouterError("response_too_large")
            data.extend(chunk)
        return bytes(data)

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        raw = response.headers.get("retry-after")
        if not raw:
            return None
        try:
            value = float(raw.strip())
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value) or value < 0:
            return None
        return min(value, 120.0)

    @staticmethod
    def _http_error(status: int, response: httpx.Response) -> OpenRouterError:
        retry_after = OpenRouterClient._retry_after(response)
        if status == 401 or status == 403:
            return OpenRouterError("auth_error", status_code=status)
        if status == 402:
            return OpenRouterError("payment_required", status_code=status)
        if status == 404:
            return OpenRouterError("model_unavailable", status_code=status)
        if status == 429:
            return OpenRouterError(
                "rate_limited", retryable=True, status_code=status,
                retry_after=retry_after)
        if status in (408, 504):
            return OpenRouterError(
                "timeout", retryable=True, status_code=status,
                retry_after=retry_after)
        if status in (502, 503):
            return OpenRouterError(
                "provider_error", retryable=True, status_code=status,
                retry_after=retry_after)
        if status == 400:
            return OpenRouterError("bad_request", status_code=status)
        if 400 <= status < 500:
            return OpenRouterError("bad_request", status_code=status)
        return OpenRouterError("provider_error", status_code=status)

    async def _fetch_models(self) -> list[dict[str, Any]]:
        api_key = (getattr(self._settings, "OPENROUTER_API_KEY", None) or "").strip()
        if not api_key:
            raise OpenRouterError("not_configured")
        client = await self._get_client()
        try:
            async with client.stream(
                "GET",
                self._settings.AI_ASSISTANT_OPENROUTER_MODELS_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Accept": "application/json",
                    "X-Title": "SmartNavigator SPb",
                },
            ) as response:
                if response.status_code != 200:
                    raise self._http_error(response.status_code, response)
                body = await self._read_limited(response, MAX_MODEL_CATALOG_BYTES)
        except httpx.TimeoutException:
            raise OpenRouterError("timeout", retryable=True) from None
        except httpx.RequestError:
            raise OpenRouterError("network", retryable=True) from None
        try:
            payload = json.loads(body)
        except (TypeError, ValueError):
            raise OpenRouterError("model_metadata_invalid") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise OpenRouterError("model_metadata_invalid")
        return payload["data"]

    async def verify_model(self, model_id: str) -> FreeModelInfo:
        """Return verified zero-price metadata or fail closed before any call."""
        if not isinstance(model_id, str) or not model_id.endswith(FREE_SUFFIX):
            raise OpenRouterError("model_not_free")
        now = self._clock()
        cached = self._models.get(model_id)
        cache_seconds = int(self._settings.AI_ASSISTANT_MODEL_CACHE_SECONDS)
        if cached is not None and now - cached[1] < cache_seconds:
            return cached[0]
        async with self._model_lock:
            cached = self._models.get(model_id)
            if cached is not None and self._clock() - cached[1] < cache_seconds:
                return cached[0]
            models = await self._fetch_models()
            found = next(
                (m for m in models if isinstance(m, dict) and m.get("id") == model_id),
                None,
            )
            if found is None:
                raise OpenRouterError("model_not_available")
            prompt, completion = validate_free_pricing(found.get("pricing"))
            info = FreeModelInfo(
                model_id=model_id,
                prompt_price=prompt,
                completion_price=completion,
                checked_at=self._clock(),
            )
            self._models[model_id] = (info, self._clock())
            return info

    def _api_key(self) -> str:
        api_key = (getattr(self._settings, "OPENROUTER_API_KEY", None) or "").strip()
        if not api_key:
            raise OpenRouterError("not_configured")
        return api_key

    @staticmethod
    def _money(value: Any) -> float | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(parsed) or parsed < 0:
            return None
        return parsed

    async def key_status(self) -> dict[str, Any]:
        """Safe key budget metadata; never returns the key or its label."""
        api_key = self._api_key()
        client = await self._get_client()
        try:
            async with client.stream(
                "GET",
                self._settings.AI_ASSISTANT_OPENROUTER_KEY_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Accept": "application/json",
                    "X-Title": "SmartNavigator SPb",
                },
            ) as response:
                if response.status_code != 200:
                    raise self._http_error(response.status_code, response)
                body = await self._read_limited(response, 4096)
        except httpx.TimeoutException:
            raise OpenRouterError("timeout", retryable=True) from None
        except httpx.RequestError:
            raise OpenRouterError("network", retryable=True) from None
        try:
            payload = json.loads(body)
        except (TypeError, ValueError):
            raise OpenRouterError("key_status_invalid") from None
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise OpenRouterError("key_status_invalid")
        free_tier = data.get("is_free_tier")
        return {
            "limit_usd": self._money(data.get("limit")),
            "usage_usd": self._money(data.get("usage")),
            "remaining_usd": self._money(data.get("limit_remaining")),
            "is_free_tier": free_tier if isinstance(free_tier, bool) else None,
        }

    def _request_body(
        self,
        model_id: str,
        messages: list[dict[str, str]],
        *,
        stream: bool = False,
    ) -> dict[str, Any]:
        data_collection = self._settings.AI_ASSISTANT_DATA_COLLECTION
        if data_collection not in ("allow", "deny"):
            raise OpenRouterError("bad_configuration")
        return {
            "model": model_id,
            "messages": messages,
            "max_tokens": int(self._settings.AI_ASSISTANT_MAX_OUTPUT_TOKENS),
            "temperature": 0.3,
            "stream": stream,
            # OpenRouter reports token usage in the final SSE data frame when
            # requested. Cost is checked before any answer is confirmed.
            "usage": {"include": True},
            "provider": {
                # Never let OpenRouter substitute another model or a paid
                # provider. max_price zero is the second, independent guard.
                "allow_fallbacks": False,
                "require_parameters": True,
                "data_collection": data_collection,
                "max_price": {"prompt": 0, "completion": 0},
            },
        }

    async def chat(
        self,
        *,
        model_id: str,
        messages: list[dict[str, str]],
    ) -> ChatResult:
        api_key = self._api_key()
        client = await self._get_client()
        body = self._request_body(model_id, messages)
        try:
            async with client.stream(
                "POST",
                self._settings.AI_ASSISTANT_OPENROUTER_CHAT_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "X-Title": "SmartNavigator SPb",
                },
                json=body,
            ) as response:
                status = response.status_code
                if status != 200:
                    # Drain only a bounded prefix; never parse or log it.
                    try:
                        await self._read_limited(response, MAX_ERROR_BODY_BYTES)
                    except (OpenRouterError, httpx.HTTPError):
                        pass
                    raise self._http_error(status, response)
                raw = await self._read_limited(
                    response, int(self._settings.AI_ASSISTANT_MAX_RESPONSE_BYTES))
        except OpenRouterError:
            raise
        except httpx.TimeoutException:
            raise OpenRouterError("timeout", retryable=True) from None
        except httpx.RequestError:
            raise OpenRouterError("network", retryable=True) from None

        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            raise OpenRouterError("bad_response") from None
        if not isinstance(payload, dict):
            raise OpenRouterError("bad_response")
        if payload.get("error") is not None:
            raise OpenRouterError("provider_error")
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise OpenRouterError("bad_response")
        first = choices[0]
        if not isinstance(first, dict) or not isinstance(first.get("message"), dict):
            raise OpenRouterError("bad_response")
        content = first["message"].get("content")
        if not isinstance(content, str):
            raise OpenRouterError("bad_response")
        content = content.strip()
        if not content:
            raise OpenRouterError("empty_response")
        max_chars = int(self._settings.AI_ASSISTANT_MAX_RESPONSE_CHARS)
        finish_reason = first.get("finish_reason")
        hit_token_limit = finish_reason == "length"
        truncated = len(content) > max_chars or hit_token_limit
        if len(content) > max_chars:
            content = content[:max_chars].rstrip()
        elif hit_token_limit:
            content = _cut_to_sentence(content)

        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        prompt_tokens = self._token_count(usage.get("prompt_tokens"))
        completion_tokens = self._token_count(usage.get("completion_tokens"))
        cost_raw = usage.get("cost")
        if cost_raw is None:
            cost_raw = usage.get("total_cost")
        cost: float | None = None
        cost_verified = False
        if cost_raw is not None and not isinstance(cost_raw, bool):
            try:
                parsed = float(cost_raw)
                if math.isfinite(parsed) and parsed >= 0:
                    cost = parsed
                    cost_verified = True
            except (TypeError, ValueError):
                cost = None
        if cost is not None and cost > 0:
            raise OpenRouterError("paid_usage", cost_usd=cost, retryable=False)
        # The requested id is only a routing request; OpenRouter reports the
        # model that actually answered. Reject anything else so technical
        # statistics never label a response with a model that did not run.
        actual_model = payload.get("model")
        if not isinstance(actual_model, str) or not actual_model.strip():
            raise OpenRouterError("bad_response")
        if actual_model != model_id:
            raise OpenRouterError("model_mismatch", retryable=False)
        return ChatResult(
            content=content,
            model_id=actual_model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost,
            cost_verified=cost_verified,
            finish_reason=finish_reason if isinstance(finish_reason, str) else None,
            truncated=truncated,
        )

    async def stream_chat(
        self,
        *,
        model_id: str,
        messages: list[dict[str, str]],
    ) -> AsyncIterator[StreamDelta | StreamSummary]:
        """Yield guarded SSE fragments and one final verified summary.

        The request is identical to :meth:`chat` except for `stream: true`.
        Every data frame that names a model is checked against the requested
        free model. A paid usage frame, a model mismatch, a mid-stream error
        frame or a truncated/incomplete stream raises before the caller can
        confirm the provisional text. `[DONE]` and `:` keepalive comments are
        handled explicitly; per-event and total byte bounds are enforced.
        """
        api_key = self._api_key()
        client = await self._get_client()
        body = self._request_body(model_id, messages, stream=True)
        max_total = int(self._settings.AI_ASSISTANT_MAX_RESPONSE_BYTES)
        max_event = int(self._settings.AI_ASSISTANT_STREAM_MAX_EVENT_BYTES)
        max_chars = int(self._settings.AI_ASSISTANT_MAX_RESPONSE_CHARS)
        actual_model: str | None = None
        prompt_tokens: int | None = None
        completion_tokens: int | None = None
        cost: float | None = None
        cost_verified = False
        finish_reason: str | None = None
        saw_text = False
        accumulated_chars = 0
        total_bytes = 0

        try:
            async with client.stream(
                "POST",
                self._settings.AI_ASSISTANT_OPENROUTER_CHAT_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Accept": "text/event-stream",
                    "Content-Type": "application/json",
                    "X-Title": "SmartNavigator SPb",
                },
                json=body,
            ) as response:
                if response.status_code != 200:
                    try:
                        await self._read_limited(response, MAX_ERROR_BODY_BYTES)
                    except (OpenRouterError, httpx.HTTPError):
                        pass
                    raise self._http_error(response.status_code, response)

                buffer = bytearray()
                data_lines: list[str] = []
                done = False

                async def handle_event(payload: str) -> (
                        AsyncIterator[StreamDelta | StreamSummary]):
                    nonlocal actual_model, prompt_tokens, completion_tokens
                    nonlocal cost, cost_verified, finish_reason
                    nonlocal saw_text, accumulated_chars, done
                    event = payload.strip()
                    if not event:
                        return
                    if event == "[DONE]":
                        if actual_model is None:
                            raise OpenRouterError("bad_response")
                        if not saw_text:
                            raise OpenRouterError("empty_response")
                        done = True
                        yield StreamSummary(
                            model_id=actual_model,
                            prompt_tokens=prompt_tokens,
                            completion_tokens=completion_tokens,
                            cost_usd=cost,
                            cost_verified=cost_verified,
                            finish_reason=finish_reason,
                            truncated=finish_reason == "length",
                        )
                        return
                    try:
                        frame = json.loads(event)
                    except (TypeError, ValueError):
                        raise OpenRouterError("bad_response") from None
                    if not isinstance(frame, dict):
                        raise OpenRouterError("bad_response")
                    if frame.get("error") is not None:
                        # A mid-stream provider error must never be mistaken
                        # for the end of a normal answer.
                        raise OpenRouterError("provider_error")
                    frame_model = frame.get("model")
                    if frame_model is not None:
                        if not isinstance(frame_model, str) or not frame_model.strip():
                            raise OpenRouterError("bad_response")
                        if frame_model != model_id:
                            raise OpenRouterError("model_mismatch")
                        actual_model = frame_model

                    usage = frame.get("usage")
                    if isinstance(usage, dict):
                        prompt_tokens = self._token_count(usage.get("prompt_tokens"))
                        completion_tokens = self._token_count(
                            usage.get("completion_tokens"))
                        cost_raw = usage.get("cost")
                        if cost_raw is None:
                            cost_raw = usage.get("total_cost")
                        if cost_raw is not None and not isinstance(cost_raw, bool):
                            try:
                                parsed = float(cost_raw)
                                if math.isfinite(parsed) and parsed >= 0:
                                    cost = parsed
                                    cost_verified = True
                            except (TypeError, ValueError):
                                cost = None
                        if cost is not None and cost > 0:
                            raise OpenRouterError(
                                "paid_usage", cost_usd=cost, retryable=False)

                    choices = frame.get("choices")
                    if not isinstance(choices, list) or not choices:
                        return
                    first = choices[0]
                    if not isinstance(first, dict):
                        raise OpenRouterError("bad_response")
                    reason = first.get("finish_reason")
                    if isinstance(reason, str) and reason:
                        finish_reason = reason
                    delta = first.get("delta")
                    if not isinstance(delta, dict):
                        return
                    content = delta.get("content")
                    if content is None:
                        return
                    if not isinstance(content, str):
                        raise OpenRouterError("bad_response")
                    if not content:
                        return
                    accumulated_chars += len(content)
                    if accumulated_chars > max_chars + 512:
                        raise OpenRouterError("response_too_large")
                    saw_text = True
                    yield StreamDelta(
                        text=content,
                        model_id=actual_model or "",
                    )

                async for raw in response.aiter_bytes():
                    if not raw:
                        continue
                    total_bytes += len(raw)
                    if total_bytes > max_total:
                        raise OpenRouterError("response_too_large")
                    buffer.extend(raw)
                    while True:
                        newline = buffer.find(b"\n")
                        if newline < 0:
                            if len(buffer) > max_event:
                                raise OpenRouterError("response_too_large")
                            break
                        line_bytes = bytes(buffer[:newline])
                        del buffer[: newline + 1]
                        if line_bytes.endswith(b"\r"):
                            line_bytes = line_bytes[:-1]
                        if len(line_bytes) > max_event:
                            raise OpenRouterError("response_too_large")
                        try:
                            line = line_bytes.decode("utf-8")
                        except UnicodeDecodeError:
                            raise OpenRouterError("bad_response") from None
                        if line == "":
                            if data_lines:
                                async for item in handle_event(
                                        "\n".join(data_lines)):
                                    yield item
                                data_lines.clear()
                            if done:
                                return
                            continue
                        if line.startswith(":"):
                            # SSE keepalive comment such as
                            # ": OPENROUTER PROCESSING".
                            continue
                        if line.startswith("data:"):
                            value = line[5:]
                            if value.startswith(" "):
                                value = value[1:]
                            data_lines.append(value)
                # A stream that ends without [DONE] is incomplete and its
                # provisional text is never confirmed.
                raise OpenRouterError("bad_response")
        except OpenRouterError:
            raise
        except httpx.TimeoutException:
            raise OpenRouterError("timeout", retryable=True) from None
        except httpx.RequestError:
            raise OpenRouterError("network", retryable=True) from None

    @staticmethod
    def _token_count(value: Any) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    async def model_status(self, model_id: str) -> tuple[str, str | None]:
        """Safe status for the private operational endpoint."""
        if not (getattr(self._settings, "OPENROUTER_API_KEY", None) or "").strip():
            return "not_checked", "key_not_configured"
        try:
            await self.verify_model(model_id)
            return "ok", None
        except OpenRouterError as error:
            return "failed", error.code
        except Exception:
            return "failed", "unexpected"
