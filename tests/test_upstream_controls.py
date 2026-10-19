"""All upstream traffic is synthetic; real Yandex quota is never exercised."""
import asyncio
from unittest.mock import patch

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings
from app.core.upstream_controls import (
    QUOTA_TABLE, ROLLING_DAY_SECONDS, UpstreamControls, initialize_quota_schema,
    upstream_controls,
)
from app.db.session import configure_sqlite_unicode
from app.services.yandex_search_service import YandexSearchService


async def ledger_rows(engine):
    async with engine.connect() as connection:
        return (await connection.execute(text(
            f'SELECT product, reserved_at FROM {QUOTA_TABLE} ORDER BY id'))).all()


@pytest.mark.asyncio
async def test_atomic_budget_across_connections_and_reopened_database(
        upstream_ledger_engine, monkeypatch):
    monkeypatch.setattr(settings, 'YANDEX_SUGGEST_DAILY_BUDGET', 3)
    second_engine = create_async_engine(upstream_ledger_engine.url)
    configure_sqlite_unicode(second_engine)
    controls = [UpstreamControls(upstream_ledger_engine), UpstreamControls(second_engine)]
    results = await asyncio.gather(
        *[controls[index % 2].reserve('suggest') for index in range(20)],
        return_exceptions=True)
    assert results.count(None) == 3
    assert all(result is None or isinstance(result, HTTPException)
               and result.status_code == 429 for result in results)
    assert len(await ledger_rows(upstream_ledger_engine)) == 3
    await second_engine.dispose()
    reopened = create_async_engine(upstream_ledger_engine.url)
    try:
        with pytest.raises(HTTPException) as rejected:
            await UpstreamControls(reopened).reserve('suggest')
        assert rejected.value.status_code == 429
        assert int(rejected.value.headers['Retry-After']) > 0
    finally:
        await reopened.dispose()


@pytest.mark.asyncio
async def test_daily_budget_rolls_after_24_hours_and_prunes_old_rows(
        upstream_ledger_engine, monkeypatch):
    monkeypatch.setattr(settings, 'YANDEX_SUGGEST_DAILY_BUDGET', 1)
    current = [100_000.0]
    controls = UpstreamControls(upstream_ledger_engine, clock=lambda: current[0])
    await controls.reserve('suggest')
    current[0] += ROLLING_DAY_SECONDS - 1
    with pytest.raises(HTTPException) as rejected:
        await controls.reserve('suggest')
    assert rejected.value.status_code == 429
    assert rejected.value.headers == {'Retry-After': '1'}
    current[0] += 1
    await controls.reserve('suggest')
    assert await ledger_rows(upstream_ledger_engine) == [('suggest', current[0])]


@pytest.mark.asyncio
async def test_server_rate_is_shared_by_products_and_survives_new_guard(
        upstream_ledger_engine, monkeypatch):
    monkeypatch.setattr(settings, 'YANDEX_SERVER_RATE_LIMIT', 2)
    current = [100_000.0]
    controls = UpstreamControls(upstream_ledger_engine, clock=lambda: current[0])
    await controls.reserve('suggest')
    await controls.reserve('geocoder')
    with pytest.raises(HTTPException) as rejected:
        await UpstreamControls(upstream_ledger_engine,
                               clock=lambda: current[0]).reserve('suggest')
    assert rejected.value.status_code == 429
    assert rejected.value.headers == {'Retry-After': '60'}
    current[0] += settings.YANDEX_RATE_WINDOW_SECONDS
    await controls.reserve('suggest')
    assert len(await ledger_rows(upstream_ledger_engine)) == 3


@pytest.mark.asyncio
async def test_zero_budget_disables_only_that_product(upstream_ledger_engine, monkeypatch):
    monkeypatch.setattr(settings, 'YANDEX_SUGGEST_DAILY_BUDGET', 0)
    controls = UpstreamControls(upstream_ledger_engine)
    with pytest.raises(HTTPException) as rejected:
        await controls.reserve('suggest')
    assert rejected.value.status_code == 429
    await controls.reserve('geocoder')
    assert [row.product for row in await ledger_rows(upstream_ledger_engine)] == ['geocoder']


@pytest.mark.asyncio
async def test_geocode_and_reverse_geocode_share_the_product_budget(
        upstream_ledger_engine, monkeypatch):
    monkeypatch.setattr(settings, 'GEOCODER_API_KEY', 'test-only')
    monkeypatch.setattr(settings, 'YANDEX_GEOCODER_DAILY_BUDGET', 1)
    payload = {'response': {'GeoObjectCollection': {'featureMember': [{
        'GeoObject': {'name': 'Test', 'Point': {'pos': '30.3 59.9'}}}]}}}
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json=payload)

    service = YandexSearchService()
    service._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        assert (await service.geocode_object(text='test')).title == 'Test'
        with pytest.raises(HTTPException) as rejected:
            await service.reverse_geocode(59.9, 30.3)
        assert rejected.value.status_code == 429
        assert len(calls) == 1
        assert len(await ledger_rows(upstream_ledger_engine)) == 1
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure,expected', [
    ('error', 502), ('timeout', 504), ('invalid_json', 502), ('redirect', 502),
])
async def test_failed_attempts_are_charged_once_without_retry(
        upstream_ledger_engine, monkeypatch, failure, expected):
    monkeypatch.setattr(settings, 'SUGGEST_API_KEY', 'test-only')
    monkeypatch.setattr(settings, 'YANDEX_SUGGEST_DAILY_BUDGET', 1)
    calls = 0

    def respond(request):
        nonlocal calls
        calls += 1
        if failure == 'timeout':
            raise httpx.ReadTimeout('private request data', request=request)
        if failure == 'invalid_json':
            return httpx.Response(200, text='invalid json')
        if failure == 'redirect':
            return httpx.Response(302, headers={'Location': 'https://untrusted.invalid/'})
        return httpx.Response(500, text='private upstream data')

    service = YandexSearchService()
    service._client = httpx.AsyncClient(transport=httpx.MockTransport(respond),
                                      follow_redirects=False)
    try:
        with pytest.raises(HTTPException) as error:
            await service.get_suggestions('first')
        assert error.value.status_code == expected
        assert 'private' not in str(error.value.detail)
        with pytest.raises(HTTPException) as rejected:
            await service.get_suggestions('second')
        assert rejected.value.status_code == 429
        assert calls == 1
        assert len(await ledger_rows(upstream_ledger_engine)) == 1
        assert service._inflight == service._waiters == {}
        assert upstream_controls._active == 0
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_singleflight_and_cancelled_attempt_accounting(upstream_ledger_engine, monkeypatch):
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

    service = YandexSearchService()
    service._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        tasks = [asyncio.create_task(service.get_suggestions('same')) for _ in range(3)]
        await asyncio.wait_for(started.wait(), 2)
        assert calls == 1
        assert len(await ledger_rows(upstream_ledger_engine)) == 1
        release.set()
        assert await asyncio.gather(*tasks) == [[], [], []]
        release.clear()
        started.clear()
        cancelled = asyncio.create_task(service.get_suggestions('cancelled'))
        await asyncio.wait_for(started.wait(), 2)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        assert calls == 2
        assert len(await ledger_rows(upstream_ledger_engine)) == 2
        assert service._inflight == service._waiters == {}
        assert upstream_controls._active == 0
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_concurrency_is_process_wide_immediate_and_allows_shared_waiters(
        upstream_ledger_engine, monkeypatch):
    monkeypatch.setattr(settings, 'SUGGEST_API_KEY', 'test-only')
    monkeypatch.setattr(settings, 'YANDEX_MAX_CONCURRENT', 4)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def respond(request):
        nonlocal calls
        calls += 1
        if calls == 4:
            started.set()
        await release.wait()
        return httpx.Response(200, json={'results': []})

    services = [YandexSearchService(), YandexSearchService()]
    for service in services:
        service._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    tasks = [asyncio.create_task(services[index % 2].get_suggestions(str(index)))
             for index in range(4)]
    try:
        await asyncio.wait_for(started.wait(), 2)
        with pytest.raises(HTTPException) as rejected:
            await asyncio.wait_for(services[0].get_suggestions('extra'), 0.5)
        assert rejected.value.status_code == 503
        assert rejected.value.headers == {'Retry-After': '1'}
        duplicate = asyncio.create_task(services[0].get_suggestions('0'))
        await asyncio.sleep(0)
        assert upstream_controls._active == 4
        assert sum(len(service._inflight) for service in services) == 4
        release.set()
        assert await asyncio.gather(*tasks, duplicate) == [[], [], [], [], []]
        assert calls == 4
        assert len(await ledger_rows(upstream_ledger_engine)) == 4
        assert upstream_controls._active == 0
    finally:
        await asyncio.gather(*(service.close() for service in services))


@pytest.mark.asyncio
async def test_service_releases_admission_if_cancelled_before_upstream_task_starts(
        upstream_ledger_engine, monkeypatch):
    monkeypatch.setattr(settings, 'SUGGEST_API_KEY', 'test-only')
    monkeypatch.setattr(settings, 'YANDEX_MAX_CONCURRENT', 1)
    calls = []
    service = YandexSearchService()
    service._client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: calls.append(request) or httpx.Response(200, json={'results': []})))
    try:
        waiter = asyncio.create_task(service.get_suggestions('test'))
        await asyncio.sleep(0)  # registration runs; newly created upstream task has not run
        task = next(iter(service._inflight.values()))
        assert upstream_controls._active == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert upstream_controls._active == 0
        assert calls == []
        assert await ledger_rows(upstream_ledger_engine) == []
        assert service._inflight == service._waiters == {}
        assert await service.get_suggestions('after cancellation') == []
        assert upstream_controls._active == 0
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_missing_ledger_fails_closed_and_releases_admission(
        upstream_ledger_engine, monkeypatch):
    monkeypatch.setattr(settings, 'SUGGEST_API_KEY', 'test-secret-only')
    async with upstream_ledger_engine.begin() as connection:
        await connection.execute(text(f'DROP TABLE {QUOTA_TABLE}'))
    calls = []
    service = YandexSearchService()
    service._client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: calls.append(request) or httpx.Response(200, json={'results': []})))
    try:
        with pytest.raises(HTTPException) as rejected:
            await service.get_suggestions('private-search-input')
        assert rejected.value.status_code == 503
        assert rejected.value.headers == {'Retry-After': '1'}
        for private in ['test-secret-only', 'private-search-input', 'sqlite', 'quota.sqlite']:
            assert private not in str(rejected.value.detail)
        assert calls == []
        assert upstream_controls._active == 0
        assert service._inflight == service._waiters == {}
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_missing_key_and_empty_input_do_not_charge(upstream_ledger_engine, monkeypatch):
    monkeypatch.setattr(settings, 'SUGGEST_API_KEY', None)
    service = YandexSearchService()
    assert await service.get_suggestions(' ') == []
    with pytest.raises(HTTPException) as missing:
        await service.get_suggestions('test')
    assert missing.value.status_code == 503
    assert await ledger_rows(upstream_ledger_engine) == []
    assert upstream_controls._active == 0
    await service.close()


@pytest.mark.asyncio
async def test_ledger_schema_retains_only_attempt_metadata(upstream_ledger_engine):
    await initialize_quota_schema(upstream_ledger_engine)  # idempotent startup
    await UpstreamControls(upstream_ledger_engine).reserve('suggest')
    async with upstream_ledger_engine.connect() as connection:
        columns, indexes = await connection.run_sync(lambda sync: (
            inspect(sync).get_columns(QUOTA_TABLE), inspect(sync).get_indexes(QUOTA_TABLE)))
    assert {column['name'] for column in columns} == {'id', 'product', 'reserved_at'}
    assert {index['name'] for index in indexes} == {
        'ix_upstream_quota_product_time', 'ix_upstream_quota_time'}


@pytest.mark.asyncio
async def test_http_client_uses_configured_timeouts_and_ignores_environment(monkeypatch):
    monkeypatch.setattr(settings, 'YANDEX_TIMEOUT_SECONDS', 8.0)
    monkeypatch.setattr(settings, 'YANDEX_CONNECT_TIMEOUT_SECONDS', 2.0)
    service = YandexSearchService()
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={'results': []})))
    try:
        with patch('app.services.yandex_search_service.httpx.AsyncClient',
                   return_value=upstream) as constructor:
            assert await service.get_client() is upstream
        kwargs = constructor.call_args.kwargs
        assert kwargs['timeout'].read == 8.0
        assert kwargs['timeout'].connect == 2.0
        assert kwargs['trust_env'] is False
        assert kwargs['follow_redirects'] is False
    finally:
        await service.close()
