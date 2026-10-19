"""C13.2 backend: guarded SSE streaming and the provisional-answer contract."""
from __future__ import annotations

import asyncio
import hashlib
import json
from decimal import Decimal

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.api.v1.endpoints import assistant as assistant_endpoint
from app.core.config import Settings, settings
from app.db.session import configure_sqlite_unicode
from app.schemas.assistant import AssistantChatRequest
from app.services.assistant.context import AssistantContextService
from app.services.assistant.openrouter import (
    FreeModelInfo,
    OpenRouterClient,
    OpenRouterError,
    StreamDelta,
    StreamSummary,
)
from app.services.assistant.quota import (
    AssistantQuotaLedger,
    initialize_assistant_schema,
)
from app.services.assistant.service import AssistantService

CODE = "c13-private-code-123456"
CODE_HASH = hashlib.sha256(CODE.encode()).hexdigest()
PRIMARY = "nvidia/nemotron-3-super-120b-a12b:free"
RESERVE = "nvidia/nemotron-3.5-super-vl:free"


@pytest_asyncio.fixture
async def assistant_ledger(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'assistant-stream.sqlite').as_posix()}")
    configure_sqlite_unicode(engine)
    await initialize_assistant_schema(engine)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def reset_endpoint_limiters():
    assistant_endpoint._failures._entries.clear()
    assistant_endpoint._chat_inbound.reset()
    assistant_endpoint._capabilities_inbound.reset()
    yield


def _enable(monkeypatch, **overrides):
    values = {
        "AI_ASSISTANT_ENABLED": True,
        "AI_ASSISTANT_ACCESS_MODE": "private_test",
        "AI_ASSISTANT_ACCESS_CODE_HASH": CODE_HASH,
        "OPENROUTER_API_KEY": "sk-or-v1-test",
        "AI_ASSISTANT_MODEL_PRIMARY": PRIMARY,
        "AI_ASSISTANT_MODEL_RESERVE": RESERVE,
        "AI_ASSISTANT_RESERVE_ENABLED": False,
        "AI_ASSISTANT_DATA_COLLECTION": "deny",
        "AI_ASSISTANT_DATA_COLLECTION_ACK": False,
        "AI_ASSISTANT_MAX_HISTORY_MESSAGES": 8,
        "AI_ASSISTANT_MAX_HISTORY_CHARS": 4000,
        "AI_ASSISTANT_GLOBAL_PER_MINUTE": 8,
        "AI_ASSISTANT_GLOBAL_PER_DAY": 100,
        "AI_ASSISTANT_SOURCE_PER_MINUTE": 3,
        "AI_ASSISTANT_SOURCE_PER_DAY": 20,
        "AI_ASSISTANT_MAX_CONCURRENT": 2,
        "AI_ASSISTANT_STREAMING_ENABLED": True,
        "AI_ASSISTANT_EXPORT_ENABLED": False,
        "AI_ASSISTANT_EXPORT_TOKEN_HASH": None,
        "AI_ASSISTANT_BACKUP_ON_START": False,
    }
    values.update(overrides)
    for key, value in values.items():
        monkeypatch.setattr(settings, key, value)


def _guest(monkeypatch, **overrides):
    values = {
        "AI_ASSISTANT_ACCESS_MODE": "guest_limited",
        "AI_ASSISTANT_SOURCE_SALT": "s" * 64,
        "AI_ASSISTANT_DATA_COLLECTION": "allow",
        "AI_ASSISTANT_DATA_COLLECTION_ACK": True,
    }
    values.update(overrides)
    _enable(monkeypatch, **values)


def _service(client, engine):
    return AssistantService(
        client=client,
        quota=AssistantQuotaLedger(engine=engine, config=settings),
        context=AssistantContextService(settings),
        config=settings,
    )


def _request(message="Расскажи об Исаакиевском соборе"):
    return AssistantChatRequest(message=message)


class FakeStreamingClient:
    def __init__(self, *, chunks=None):
        self.chunks = list(chunks or [])
        self.stream_calls = []
        self.verified = []
        self.closed = False

    async def verify_model(self, model_id):
        self.verified.append(model_id)
        return FreeModelInfo(model_id, Decimal(0), Decimal(0), 0.0)

    async def stream_chat(self, *, model_id, messages):
        self.stream_calls.append({"model_id": model_id, "messages": messages})
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    async def close(self):
        self.closed = True


class _SseBody(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk


def _frame(model=PRIMARY, content=None, finish=None, usage=None, error=None):
    payload = {"model": model, "choices": []}
    if content is not None or finish is not None:
        payload["choices"] = [{
            "delta": {"content": content} if content is not None else {},
            "finish_reason": finish,
        }]
    if usage is not None:
        payload["usage"] = usage
    if error is not None:
        payload["error"] = error
    return f"data: {json.dumps(payload)}\n\n".encode()


def _summary(**overrides):
    values = dict(
        model_id=PRIMARY, prompt_tokens=10, completion_tokens=5,
        cost_usd=0.0, cost_verified=True, finish_reason="stop",
        truncated=False)
    values.update(overrides)
    return StreamSummary(**values)


async def _rows(engine):
    async with engine.connect() as connection:
        return list((await connection.execute(text(
            "SELECT outcome, error_code, provider_cost "
            "FROM assistant_quota_ledger ORDER BY id"))).all())


def _parse_sse(raw: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for block in raw.replace("\r\n", "\n").split("\n\n"):
        name = None
        data = None
        for line in block.split("\n"):
            if line.startswith("event: "):
                name = line[len("event: "):]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: "):])
        if name is not None and data is not None:
            events.append((name, data))
    return events


def test_streaming_endpoint_is_wired_without_weakening_config():
    defaults = Settings(_env_file=None)
    assert defaults.AI_ASSISTANT_STREAMING_ENABLED is True
    assert defaults.AI_ASSISTANT_STREAM_MAX_EVENT_BYTES == 65536
    paths = {route.path for route in assistant_endpoint.router.routes}
    assert "/chat" in paths
    assert "/chat/stream" in paths


@pytest.mark.asyncio
async def test_openrouter_stream_parses_comments_fragments_usage_and_done(
        monkeypatch):
    _enable(monkeypatch)
    request_body = {}

    def handler(request):
        request_body.update(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_SseBody([
                b": OPENROUTER PROCESSING\n\n",
                _frame(content="Привет"),
                _frame(content=", мир"),
                _frame(finish="stop", usage={
                    "prompt_tokens": 10, "completion_tokens": 5, "cost": 0.0}),
                b"data: [DONE]\n\n",
            ]),
        )

    client = OpenRouterClient(
        config=settings, transport=httpx.MockTransport(handler))
    try:
        items = [item async for item in client.stream_chat(
            model_id=PRIMARY, messages=[{"role": "user", "content": "Q"}])]
    finally:
        await client.close()

    deltas = [item for item in items if isinstance(item, StreamDelta)]
    assert [item.text for item in deltas] == ["Привет", ", мир"]
    summary = items[-1]
    assert isinstance(summary, StreamSummary)
    assert summary.model_id == PRIMARY
    assert summary.cost_usd == 0.0 and summary.cost_verified is True
    assert summary.prompt_tokens == 10 and summary.completion_tokens == 5
    assert request_body["stream"] is True
    assert request_body["provider"] == {
        "allow_fallbacks": False,
        "require_parameters": True,
        "data_collection": "deny",
        "max_price": {"prompt": 0, "completion": 0},
    }


@pytest.mark.asyncio
async def test_openrouter_stream_rejects_model_mismatch_and_paid_usage(
        monkeypatch):
    _enable(monkeypatch)

    def mismatch(request):
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            stream=_SseBody([
                _frame(content="Начало"),
                _frame(model=RESERVE, content="подмена"),
                b"data: [DONE]\n\n",
            ]))

    client = OpenRouterClient(
        config=settings, transport=httpx.MockTransport(mismatch))
    try:
        received = []
        with pytest.raises(OpenRouterError) as raised:
            async for item in client.stream_chat(model_id=PRIMARY, messages=[]):
                received.append(item)
    finally:
        await client.close()
    assert raised.value.code == "model_mismatch"
    assert [item.text for item in received] == ["Начало"]

    def paid(request):
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            stream=_SseBody([
                _frame(content="Текст"),
                _frame(finish="stop", usage={
                    "prompt_tokens": 1, "completion_tokens": 1, "cost": 0.002}),
                b"data: [DONE]\n\n",
            ]))

    client = OpenRouterClient(
        config=settings, transport=httpx.MockTransport(paid))
    try:
        with pytest.raises(OpenRouterError) as paid_error:
            async for _ in client.stream_chat(model_id=PRIMARY, messages=[]):
                pass
    finally:
        await client.close()
    assert paid_error.value.code == "paid_usage"
    assert paid_error.value.cost_usd == 0.002


@pytest.mark.asyncio
async def test_openrouter_stream_detects_midstream_error_missing_done_and_size(
        monkeypatch):
    _enable(monkeypatch)

    def error_frame(request):
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            stream=_SseBody([
                _frame(content="Начало"),
                _frame(error={"message": "provider failed"}),
                b"data: [DONE]\n\n",
            ]))

    client = OpenRouterClient(
        config=settings, transport=httpx.MockTransport(error_frame))
    try:
        with pytest.raises(OpenRouterError) as raised:
            async for _ in client.stream_chat(model_id=PRIMARY, messages=[]):
                pass
    finally:
        await client.close()
    assert raised.value.code == "provider_error"

    def no_done(request):
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            stream=_SseBody([
                _frame(content="Обрыв"),
                _frame(finish="stop", usage={
                    "prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0}),
            ]))

    client = OpenRouterClient(
        config=settings, transport=httpx.MockTransport(no_done))
    try:
        with pytest.raises(OpenRouterError) as missing:
            async for _ in client.stream_chat(model_id=PRIMARY, messages=[]):
                pass
    finally:
        await client.close()
    assert missing.value.code == "bad_response"

    huge = b"data: " + b"x" * 5000 + b"\n\n"
    _enable(monkeypatch, AI_ASSISTANT_STREAM_MAX_EVENT_BYTES=4096)

    def too_large(request):
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            stream=_SseBody([huge]))

    client = OpenRouterClient(
        config=settings, transport=httpx.MockTransport(too_large))
    try:
        with pytest.raises(OpenRouterError) as size_error:
            async for _ in client.stream_chat(model_id=PRIMARY, messages=[]):
                pass
    finally:
        await client.close()
    assert size_error.value.code == "response_too_large"


@pytest.mark.asyncio
async def test_service_stream_emits_typed_events_and_confirms_after_ledger(
        db_session, assistant_ledger, monkeypatch):
    _enable(monkeypatch)
    fake = FakeStreamingClient(chunks=[
        StreamDelta("Ответ ", PRIMARY),
        StreamDelta("помощника.", PRIMARY),
        _summary(),
    ])
    service = _service(fake, assistant_ledger)
    lease = await service.prepare_stream(
        db_session, _request(), source_hash="j" * 32)
    events = [event async for event in service.stream(lease)]

    assert [event["event"] for event in events] == [
        "started", "delta", "delta", "completed"]
    assert events[0]["data"] == {"model": PRIMARY}
    assert events[1]["data"]["text"] == "Ответ "
    completed = events[-1]["data"]
    assert completed["answer"] == "Ответ помощника."
    assert completed["model"] == PRIMARY
    assert completed["usage"]["cost_verified"] is True
    assert await _rows(assistant_ledger) == [("ok", None, 0.0)]
    assert service._semaphore.locked() is False
    assert fake.stream_calls[0]["model_id"] == PRIMARY


@pytest.mark.asyncio
async def test_service_stream_error_never_emits_completed_and_settles_guard(
        db_session, assistant_ledger, monkeypatch):
    _enable(monkeypatch)

    async def chunks():
        yield StreamDelta("Предварительный текст", PRIMARY)
        raise OpenRouterError("model_mismatch")

    fake = FakeStreamingClient()
    fake.stream_chat = lambda *, model_id, messages: chunks()
    service = _service(fake, assistant_ledger)
    lease = await service.prepare_stream(
        db_session, _request(), source_hash="k" * 32)
    events = [event async for event in service.stream(lease)]

    assert [event["event"] for event in events] == [
        "started", "delta", "error"]
    assert events[-1]["data"]["code"] == "model_mismatch"
    assert "Предварительный текст" not in json.dumps(
        events[-1]["data"], ensure_ascii=False)
    assert await _rows(assistant_ledger) == [
        ("guard_error", "model_mismatch", None)]
    assert service._semaphore.locked() is False


@pytest.mark.asyncio
async def test_service_stream_paid_usage_trips_kill_switch_without_answer(
        db_session, assistant_ledger, monkeypatch):
    _enable(monkeypatch)

    async def paid_chunks():
        yield StreamDelta("Текст", PRIMARY)
        raise OpenRouterError("paid_usage", cost_usd=0.02)

    fake = FakeStreamingClient()
    fake.stream_chat = lambda *, model_id, messages: paid_chunks()
    service = _service(fake, assistant_ledger)
    lease = await service.prepare_stream(
        db_session, _request(), source_hash="l" * 32)
    events = [event async for event in service.stream(lease)]

    assert [event["event"] for event in events] == ["started", "delta", "error"]
    assert await _rows(assistant_ledger) == [("paid_usage", "paid_usage", 0.02)]
    assert await AssistantQuotaLedger(
        engine=assistant_ledger).kill_switch_reason() == "paid_usage_detected"


@pytest.mark.asyncio
async def test_service_stream_cancel_settles_and_releases_the_slot(
        db_session, assistant_ledger, monkeypatch):
    _enable(monkeypatch)

    async def blocked_chunks():
        yield StreamDelta("Начало", PRIMARY)
        await asyncio.Event().wait()
        yield _summary()

    fake = FakeStreamingClient()
    fake.stream_chat = lambda *, model_id, messages: blocked_chunks()
    service = _service(fake, assistant_ledger)
    lease = await service.prepare_stream(
        db_session, _request(), source_hash="m" * 32)
    stream = service.stream(lease)
    assert (await stream.__anext__())["event"] == "started"
    assert (await stream.__anext__())["event"] == "delta"
    await stream.aclose()

    assert await _rows(assistant_ledger) == [
        ("cancelled", "cancelled", None)]
    assert service._semaphore.locked() is False


@pytest.mark.asyncio
async def test_service_stream_rejects_when_streaming_is_disabled(
        db_session, assistant_ledger, monkeypatch):
    _enable(monkeypatch, AI_ASSISTANT_STREAMING_ENABLED=False)
    service = _service(FakeStreamingClient(), assistant_ledger)
    from app.services.assistant.service import AssistantUnavailable

    with pytest.raises(AssistantUnavailable) as raised:
        await service.prepare_stream(
            db_session, _request(), source_hash="n" * 32)
    assert raised.value.code == "disabled"
    assert service._semaphore.locked() is False


@pytest.mark.asyncio
async def test_api_stream_returns_sse_fragments_and_final_confirmation(
        client, db_session, assistant_ledger, monkeypatch):
    _guest(monkeypatch)
    fake = FakeStreamingClient(chunks=[
        StreamDelta("Первый ", PRIMARY),
        StreamDelta("фрагмент", PRIMARY),
        _summary(),
    ])
    service = _service(fake, assistant_ledger)
    monkeypatch.setattr(assistant_endpoint, "get_assistant_service", lambda: service)

    async with client.stream(
            "POST", "/api/v1/assistant/chat/stream",
            json={"message": "Расскажи о городе"}) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-store"
        raw = "".join([chunk async for chunk in response.aiter_text()])

    events = _parse_sse(raw)
    assert [name for name, _ in events] == [
        "started", "delta", "delta", "completed"]
    assert events[1][1]["text"] == "Первый "
    assert events[2][1]["text"] == "фрагмент"
    assert events[3][1]["answer"] == "Первый фрагмент"
    assert events[3][1]["model"] == PRIMARY
    assert "sk-or-v1" not in raw and CODE not in raw


@pytest.mark.asyncio
async def test_api_stream_preflight_keeps_http_quota_contract(
        client, db_session, assistant_ledger, monkeypatch):
    _guest(monkeypatch, AI_ASSISTANT_INBOUND_CHAT_PER_MINUTE=1)
    fake = FakeStreamingClient(chunks=[
        StreamDelta("Ответ", PRIMARY), _summary()])
    service = _service(fake, assistant_ledger)
    monkeypatch.setattr(assistant_endpoint, "get_assistant_service", lambda: service)

    async with client.stream(
            "POST", "/api/v1/assistant/chat/stream",
            json={"message": "Первый"}) as first:
        assert first.status_code == 200
        async for _ in first.aiter_bytes():
            pass
    second = await client.post(
        "/api/v1/assistant/chat/stream", json={"message": "Второй"})
    assert second.status_code == 429
    assert int(second.headers["retry-after"]) >= 1
    assert len(fake.stream_calls) == 1
