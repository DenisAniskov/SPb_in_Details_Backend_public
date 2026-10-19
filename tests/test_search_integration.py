"""Controller verification of search isolation, lifecycle, and diagnostic privacy."""
import asyncio
import logging
from unittest.mock import patch

import httpx
import pytest

from app.core.config import settings
from app.services.yandex_search_service import YandexSearchService


@pytest.mark.asyncio
async def test_search_and_access_logs_redact_query_values(monkeypatch, caplog):
    monkeypatch.setattr(settings, 'GEOCODER_API_KEY', 'fixture-private-key')
    caplog.set_level(logging.INFO, logger='httpx')
    caplog.set_level(logging.INFO, logger='uvicorn.access')
    service = YandexSearchService()
    service._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
        httpx.Response(200, json={'response': {'GeoObjectCollection': {'featureMember': [
            {'GeoObject': {'name': 'Объект', 'metaDataProperty': {'GeocoderMetaData': {'text': 'Адрес'}}}}
        ]}}})))
    try:
        await service.reverse_geocode(12.345678, 23.456789)
        logging.getLogger('uvicorn.access').info('%s - "%s %s HTTP/%s" %d',
            'client', 'GET', '/api/v1/search/reverse-geocode?lat=12.345678&lon=23.456789', '1.1', 200)
        for sensitive in ['fixture-private-key', '12.345678', '23.456789']:
            assert sensitive not in caplog.text
        assert 'reverse-geocode' in caplog.text and '200' in caplog.text
        assert 'apikey=***' in caplog.text
        from uvicorn.logging import AccessFormatter
        access_record = next(record for record in caplog.records if record.name == 'uvicorn.access')
        formatted = AccessFormatter().format(access_record)
        assert 'lat=***&lon=***' in formatted  # formatter retains its expected args tuple
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_concurrent_identical_successes_and_distinct_queries_are_isolated(monkeypatch):
    monkeypatch.setattr(settings, 'SUGGEST_API_KEY', 'test-only')
    service = YandexSearchService()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def respond(request):
        calls.append(request.url.params['text'])
        if len(calls) == 2:
            started.set()
        await release.wait()
        return httpx.Response(200, json={'results': [{'title': {'text': request.url.params['text']}}]})

    service._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        tasks = [asyncio.create_task(service.get_suggestions(query)) for query in ['А', 'А', 'Б']]
        await asyncio.wait_for(started.wait(), timeout=2)
        release.set()
        results = await asyncio.gather(*tasks)
        assert [[item.title for item in result] for result in results] == [['А'], ['А'], ['Б']]
        assert sorted(calls) == ['А', 'Б']
        assert service._inflight == service._waiters == {}
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_cold_start_uses_one_client_and_close_drains_waiters(monkeypatch):
    monkeypatch.setattr(settings, 'SUGGEST_API_KEY', 'test-only')
    started = asyncio.Event()
    release = asyncio.Event()

    async def respond(request):
        started.set()
        await release.wait()
        return httpx.Response(200, json={'results': []})

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    service = YandexSearchService()
    with patch('app.services.yandex_search_service.httpx.AsyncClient', return_value=upstream) as constructor:
        clients = await asyncio.gather(*[service.get_client() for _ in range(5)])
    assert all(client is upstream for client in clients)
    constructor.assert_called_once()
    tasks = [asyncio.create_task(service.get_suggestions('А')) for _ in range(3)]
    await asyncio.wait_for(started.wait(), timeout=2)
    await service.close()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    assert service._inflight == service._waiters == {}
    assert upstream.is_closed and service._client is None
