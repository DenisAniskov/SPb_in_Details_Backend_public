import pytest
from httpx import AsyncClient
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.db.base import Base
from app.db.seed_data import SPB_SEED_PLACES
from app.models.place import Place


@pytest.mark.asyncio
async def test_each_category_and_combined_search_filters(client: AsyncClient):
    from app.models.enums import PlaceCategory
    for category in PlaceCategory:
        data = (await client.get('/api/v1/places', params={'category': category.value, 'limit': 200})).json()
        assert data['items'] and data['total'] == len(data['items'])
        assert all(item['category'] == category.value for item in data['items'])
    data = (await client.get('/api/v1/places', params={
        'category': 'art', 'q': '  РУССКИЙ  ', 'is_unusual': 'false',
    })).json()
    assert data['total'] == 1
    assert 'Русский музей' in data['items'][0]['title']
    assert not data['items'][0]['is_unusual']


@pytest.mark.asyncio
async def test_cyrillic_search_is_case_insensitive(client: AsyncClient):
    for query in ['эрмитаж', 'ЭРМИТАЖ', 'ЭрМиТаЖ']:
        response = await client.get('/api/v1/places', params={'q': query})
        assert response.status_code == 200
        assert any('Эрмитаж' in item['title'] for item in response.json()['items'])


@pytest.mark.asyncio
async def test_search_wildcards_are_literal(client: AsyncClient):
    response = await client.get('/api/v1/places', params={'q': '%'})
    assert response.json()['total'] == 0


@pytest.mark.asyncio
async def test_combined_filters_and_pagination(client: AsyncClient):
    response = await client.get('/api/v1/places', params=[
        ('category', 'art'), ('category', 'modern_spaces'),
        ('is_unusual', 'true'), ('limit', '1'), ('offset', '1'),
    ])
    assert response.status_code == 200
    data = response.json()
    assert data['total'] >= 2 and len(data['items']) == 1
    assert data['items'][0]['category'] in ['art', 'modern_spaces']
    assert data['items'][0]['is_unusual'] is True


@pytest.mark.asyncio
async def test_no_unsupported_photos_ratings_or_hours(client: AsyncClient):
    items = (await client.get('/api/v1/places?limit=200')).json()['items']
    assert len(items) == len(SPB_SEED_PLACES)
    for item in items:
        assert item['source_url'].startswith('https://www.openstreetmap.org/')
        assert item['coordinate_kind'] in ['osm_node', 'osm_bounds_center']
        assert item['rating'] is None
        assert item['photo_url'] is None
        detail = (await client.get(f"/api/v1/places/{item['id']}")).json()
        assert detail['working_hours'] is None


@pytest.mark.asyncio
async def test_evaluate_is_explicitly_unimplemented(client: AsyncClient):
    payload = {'start_point': {'latitude': 59.94, 'longitude': 30.31}, 'destination_place_id': 1}
    data = (await client.post('/api/v1/routes/evaluate', json=payload)).json()
    assert data['status'] == 'not_implemented'
    assert data['route_distance_meters'] is None
    assert data['estimated_walking_duration_seconds'] is None
    assert 'не рассчитываются' in data['message']
    payload['destination_place_id'] = 99999
    assert (await client.post('/api/v1/routes/evaluate', json=payload)).status_code == 404
    payload['start_point']['latitude'] = 100
    assert (await client.post('/api/v1/routes/evaluate', json=payload)).status_code == 422


def test_seed_coordinates_have_traceable_sources():
    assert len({item['title'] for item in SPB_SEED_PLACES}) == len(SPB_SEED_PLACES)
    assert len({item['category'] for item in SPB_SEED_PLACES}) == 6
    for item in SPB_SEED_PLACES:
        assert item['source_url'].startswith('https://www.openstreetmap.org/')
        assert item['coordinate_kind'] in ['osm_node', 'osm_bounds_center']
        # Phase B extends the documented city boundary beyond the historical center.
        assert 59.78 <= item['latitude'] <= 60.15
        assert 30.10 <= item['longitude'] <= 30.58


@pytest.mark.asyncio
async def test_startup_seed_sync_is_idempotent_and_preserves_ids(tmp_path, monkeypatch):
    from app.db import session as db
    import app.main as main
    from unittest.mock import AsyncMock
    engine = create_async_engine(f'sqlite+aiosqlite:///{tmp_path / "lifecycle.db"}')
    db.configure_sqlite_unicode(engine)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, 'engine', engine)
    monkeypatch.setattr(db, 'async_session_factory', sessions)
    dispose = AsyncMock(wraps=engine.dispose)
    from types import SimpleNamespace
    monkeypatch.setattr(main, 'engine', SimpleNamespace(dispose=dispose))
    async with main.lifespan(main.app):
        async with sessions() as s:
            place = await s.scalar(select(Place).where(Place.title == SPB_SEED_PLACES[0]['title']))
            place_id = place.id
            place.latitude = 0
            place.rating = 4.9
            await s.commit()
        await db.init_db()
        async with sessions() as s:
            assert await s.scalar(select(func.count(Place.id))) == len(SPB_SEED_PLACES)
            place = await s.get(Place, place_id)
            assert place.latitude == SPB_SEED_PLACES[0]['latitude']
            assert place.rating is None
    dispose.assert_awaited_once()
    # Production shutdown is a lifecycle boundary: lifespan exit permanently
    # closes the shared search singleton. Reopen it for the rest of the suite.
    from app.services.yandex_search_service import yandex_search_service
    yandex_search_service._closing = False
    yandex_search_service._client = None
