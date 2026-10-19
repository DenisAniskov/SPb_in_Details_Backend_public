"""Concurrency regressions for the Yandex search singleflight primitive.

Multiple concurrent callers may await one upstream request: a failure must
reach every waiter, one cancelled waiter must not poison the others, and the
internal bookkeeping (`_inflight`/`_waiters`) must drain completely so later
requests build fresh upstream calls.
"""
import asyncio

import httpx
import pytest
from fastapi import HTTPException

from app.core.config import settings
from app.services.yandex_search_service import YandexSearchService


@pytest.fixture
def service():
    return YandexSearchService()


@pytest.mark.asyncio
async def test_error_reaches_every_waiter_and_state_drains(service, monkeypatch):
    monkeypatch.setattr(settings, 'SUGGEST_API_KEY', 'test-only')
    release = asyncio.Event()

    async def respond(request):
        await release.wait()
        return httpx.Response(500, text='boom')

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    service._client = upstream
    tasks = [asyncio.create_task(service.get_suggestions('вокзал')) for _ in range(3)]
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(r, HTTPException) and r.status_code == 502 for r in results), results
    assert service._inflight == {}
    assert service._waiters == {}
    await service.close()


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_poison_shared_result(service, monkeypatch):
    monkeypatch.setattr(settings, 'SUGGEST_API_KEY', 'test-only')
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def respond(request):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return httpx.Response(200, json={'results': [{'title': {'text': 'Вокзал'}}]})

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    service._client = upstream
    first = asyncio.create_task(service.get_suggestions('вокзал'))
    await started.wait()
    second = asyncio.create_task(service.get_suggestions('вокзал'))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    assert [item.title for item in await second] == ['Вокзал']
    assert calls == 1, 'the shared upstream call must survive one cancelled waiter'
    assert service._inflight == {} and service._waiters == {}
    await service.close()


@pytest.mark.asyncio
async def test_failure_does_not_poison_the_next_request(service, monkeypatch):
    monkeypatch.setattr(settings, 'SUGGEST_API_KEY', 'test-only')
    fail_first = True

    async def respond(request):
        if fail_first:
            return httpx.Response(500, text='boom')
        return httpx.Response(200, json={'results': [{'title': {'text': 'Вокзал'}}]})

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    service._client = upstream
    with pytest.raises(HTTPException):
        await service.get_suggestions('вокзал')
    fail_first = False
    assert [item.title for item in await service.get_suggestions('вокзал')] == ['Вокзал']
    assert service._waiters == {} and service._inflight == {}
    await service.close()


@pytest.mark.asyncio
async def test_close_without_inflight_closes_client_and_is_idempotent(service):
    upstream = httpx.AsyncClient()
    service._client = upstream
    await service.close()
    assert service._client is None
    assert upstream.is_closed
    await service.close()  # repeated close is safe and changes nothing
    assert service._client is None
    assert service._inflight == {} and service._waiters == {}


@pytest.mark.asyncio
async def test_close_cancels_inflight_and_rejects_late_requests(service, monkeypatch):
    """Shutdown must be a lifecycle boundary: the inflight request is cancelled,
    a request racing shutdown gets an honest 503, and no new client can be
    created (and leaked) behind close()'s back."""
    monkeypatch.setattr(settings, 'SUGGEST_API_KEY', 'test-only')
    started = asyncio.Event()

    async def respond(request):
        started.set()
        await asyncio.Event().wait()  # hangs until cancelled
        return httpx.Response(200, json={'results': []})

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    service._client = upstream
    waiter = asyncio.create_task(service.get_suggestions('вокзал'))
    await started.wait()
    closer = asyncio.create_task(service.close())
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(waiter, timeout=2)
    # A fresh request while shutdown is draining: 503, never a recreated client.
    with pytest.raises(HTTPException) as exc:
        await service.get_suggestions('вокзал')
    assert exc.value.status_code == 503
    await closer
    assert service._client is None
    assert upstream.is_closed
    assert service._inflight == {} and service._waiters == {}
    # Request after close is still an honest 503.
    with pytest.raises(HTTPException) as again:
        await service.get_suggestions('вокзал')
    assert again.value.status_code == 503


@pytest.mark.asyncio
async def test_close_with_shared_waiters_drains_state(service, monkeypatch):
    monkeypatch.setattr(settings, 'SUGGEST_API_KEY', 'test-only')
    started = asyncio.Event()

    async def respond(request):
        started.set()
        await asyncio.Event().wait()
        return httpx.Response(200, json={'results': []})

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    service._client = upstream
    tasks = [asyncio.create_task(service.get_suggestions('вокзал')) for _ in range(3)]
    await started.wait()
    closer = asyncio.create_task(service.close())
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(r, asyncio.CancelledError) for r in results), results
    await closer
    assert service._inflight == {} and service._waiters == {}
    assert upstream.is_closed


@pytest.mark.asyncio
async def test_request_starting_concurrently_with_close_is_rejected(service, monkeypatch):
    """Freeze shutdown between the closing flag and the inflight snapshot: a
    request entering the window must be rejected instead of registering a task
    that escapes cancellation and races the client close."""
    monkeypatch.setattr(settings, 'SUGGEST_API_KEY', 'test-only')
    async with service._client_lock:
        closer = asyncio.create_task(service.close())
        await asyncio.sleep(0)
        assert service._closing is True
        request = asyncio.create_task(service.get_suggestions('вокзал'))
        await asyncio.sleep(0)
        assert not request.done(), 'the request must wait for the lifecycle lock'
    await closer
    with pytest.raises(HTTPException) as exc:
        await request
    assert exc.value.status_code == 503
    assert service._client is None, 'shutdown must not recreate a client'
    assert service._inflight == {} and service._waiters == {}
