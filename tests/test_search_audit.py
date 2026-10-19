import asyncio
import logging
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from app.core.config import settings
from app.services.yandex_search_service import YandexSearchService, yandex_search_service

PATHS = [('/suggest', {'text': 'Московский вокзал'}),
         ('/geocode', {'text': 'Московский вокзал'}),
         ('/reverse-geocode', {'lat': 59.93, 'lon': 30.36})]
GEO = {'response': {'GeoObjectCollection': {'featureMember': [{
    'GeoObject': {'name': 'Московский вокзал', 'Point': {'pos': '30.36 59.93'},
                  'metaDataProperty': {'GeocoderMetaData': {'text': 'СПб'}}}
}]}}}


@pytest.mark.asyncio
async def test_lifespan_closes_http_client_even_if_database_disposal_fails(monkeypatch):
    import app.main as main
    from types import SimpleNamespace
    close = AsyncMock()
    monkeypatch.setattr(main, 'init_db', AsyncMock())
    monkeypatch.setattr(main, 'engine', SimpleNamespace(dispose=AsyncMock(side_effect=RuntimeError('disposal failure'))))
    monkeypatch.setattr(main, 'yandex_search_service', SimpleNamespace(close=close))
    with pytest.raises(RuntimeError, match='disposal failure'):
        async with main.lifespan(main.app): pass
    close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('path,params', PATHS)
@pytest.mark.parametrize('error,expected', [(httpx.TimeoutException('private test-only'), 504),
                                         (httpx.ConnectError('private test-only'), 502)])
async def test_timeout_and_network_error_on_all_services(client, monkeypatch, path, params, error, expected):
    monkeypatch.setattr(settings, 'SUGGEST_API_KEY', 'test-only')
    monkeypatch.setattr(settings, 'GEOCODER_API_KEY', 'test-only')
    mocked = AsyncMock()
    mocked.get.side_effect = error
    with patch.object(yandex_search_service, 'get_client', AsyncMock(return_value=mocked)):
        response = await client.get('/api/v1/search'+path, params=params)
    assert response.status_code == expected
    assert 'private test-only' not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize('path,params', PATHS)
@pytest.mark.parametrize('upstream,expected', [(401, 503), (403, 503), (429, 429), (500, 502)])
async def test_all_services_normalize_upstream_errors(client, monkeypatch, path, params, upstream, expected):
    monkeypatch.setattr(settings, 'SUGGEST_API_KEY', 'test-credential-only')
    monkeypatch.setattr(settings, 'GEOCODER_API_KEY', 'test-credential-only')
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(upstream, text='private raw upstream test-credential-only')
    )) as upstream_client:
        with patch.object(yandex_search_service, 'get_client', AsyncMock(return_value=upstream_client)):
            response = await client.get('/api/v1/search'+path, params=params)
    assert response.status_code == expected
    assert 'test-credential-only' not in response.text
    assert 'private raw' not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize('path,params,payload', [
    (*PATHS[0], []), (*PATHS[0], {}), (*PATHS[0], {'results': None}),
    (*PATHS[0], {'results': [None]}), (*PATHS[0], {'results': [{'title': 'wrong type'}]}),
    (*PATHS[0], {'results': [{'title': {'text': 'name'}, 'tags': [4]}]}),
    (*PATHS[1], {'response': []}), (*PATHS[1], {'response': {'GeoObjectCollection': {'featureMember': [None]}}}),
    (*PATHS[2], {'response': {'GeoObjectCollection': {'featureMember': 'wrong'}}}),
])
async def test_malformed_upstream_is_502(client, monkeypatch, path, params, payload):
    monkeypatch.setattr(settings, 'SUGGEST_API_KEY', 'test-only')
    monkeypatch.setattr(settings, 'GEOCODER_API_KEY', 'test-only')
    mocked = AsyncMock()
    mocked.get.return_value = httpx.Response(200, json=payload)
    with patch.object(yandex_search_service, 'get_client', AsyncMock(return_value=mocked)):
        response = await client.get('/api/v1/search'+path, params=params)
    assert response.status_code == 502
    assert set(response.json()) == {'detail'}


@pytest.mark.asyncio
@pytest.mark.parametrize('pos', ['nan 59', '30 inf', '181 59', '30 91', '30', 'bad 59'])
async def test_invalid_upstream_coordinates(client, monkeypatch, pos):
    monkeypatch.setattr(settings, 'GEOCODER_API_KEY', 'test-only')
    import copy
    data = copy.deepcopy(GEO)
    data['response']['GeoObjectCollection']['featureMember'][0]['GeoObject']['Point']['pos'] = pos
    mocked = AsyncMock()
    mocked.get.return_value = httpx.Response(200, json=data)
    with patch.object(yandex_search_service, 'get_client', AsyncMock(return_value=mocked)):
        response = await client.get('/api/v1/search/geocode', params={'text': 'СПб'})
    assert response.status_code == 502


@pytest.mark.asyncio
@pytest.mark.parametrize('path,params', PATHS)
async def test_invalid_json_and_missing_keys(client, monkeypatch, path, params):
    for name in ['SUGGEST_API_KEY', 'GEOCODER_API_KEY']:
        monkeypatch.setattr(settings, name, None)
    assert (await client.get('/api/v1/search'+path, params=params)).status_code == 503
    for name in ['SUGGEST_API_KEY', 'GEOCODER_API_KEY']:
        monkeypatch.setattr(settings, name, 'test-only')
    mocked = AsyncMock()
    mocked.get.return_value = httpx.Response(200, text='not json')
    with patch.object(yandex_search_service, 'get_client', AsyncMock(return_value=mocked)):
        assert (await client.get('/api/v1/search'+path, params=params)).status_code == 502


@pytest.mark.asyncio
async def test_request_parameters_empty_uri_and_secret_logs(client, monkeypatch, caplog):
    secret = 'audit-fixture-credential'
    monkeypatch.setattr(settings, 'SUGGEST_API_KEY', secret)
    monkeypatch.setattr(settings, 'GEOCODER_API_KEY', secret)
    captured = []
    def respond(request):
        captured.append(request)
        return httpx.Response(200, json={'results': [{'title': {'text': 'Вокзал'}, 'uri': ''}]} if 'suggest' in request.url.path else GEO)
    caplog.set_level(logging.INFO, logger='httpx')
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as upstream:
        with patch.object(yandex_search_service, 'get_client', AsyncMock(return_value=upstream)):
            suggest = await client.get('/api/v1/search/suggest', params={'text': 'Московский вокзал'})
            assert suggest.json()['results'][0]['uri'] is None
            assert captured[-1].url.params['lang'] == 'ru'
            assert captured[-1].url.params['attrs'] == 'uri'
            assert captured[-1].url.params['print_address'] == '1'
            assert captured[-1].url.params['ll'].startswith('30.')
            assert 'strict_bounds' not in captured[-1].url.params
            await client.get('/api/v1/search/geocode', params={'uri': 'ymapsbm1://org?oid=test'})
            assert 'geocode' not in captured[-1].url.params
            await client.get('/api/v1/search/reverse-geocode', params={'lat': 59.93, 'lon': 30.36})
            assert captured[-1].url.params['geocode'] == '30.36,59.93'
            before = len(captured)
            assert (await client.get('/api/v1/search/geocode', params={'text': ' ', 'uri': ' '})).status_code == 400
            assert (await client.get('/api/v1/search/suggest', params={'text': 'x'*151})).status_code == 422
            assert len(captured) == before
    assert secret not in caplog.text
    assert 'apikey=***' in caplog.text


@pytest.mark.asyncio
async def test_single_client_singleflight_no_result_cache_and_cancellation(monkeypatch):
    service = YandexSearchService()
    monkeypatch.setattr(settings, 'SUGGEST_API_KEY', 'test-only')
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0
    async def respond(request):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return httpx.Response(200, json={'results': []})
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    service._client = upstream
    assert await service.get_client() is upstream
    first = asyncio.create_task(service.get_suggestions('вокзал'))
    await started.wait()
    second = asyncio.create_task(service.get_suggestions('вокзал'))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError): await first
    release.set()
    assert await second == []
    assert calls == 1
    assert service._inflight == {}
    await service.get_suggestions('вокзал')
    assert calls == 2  # Completed results are not cached.
    release.clear(); started.clear()
    pending = asyncio.create_task(service.get_suggestions('Невский'))
    await started.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError): await pending
    assert service._inflight == {}
    await service.close()
    assert upstream.is_closed and service._client is None
