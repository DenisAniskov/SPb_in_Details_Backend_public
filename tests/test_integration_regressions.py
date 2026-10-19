"""Controller evidence for catalog repair ordering and honest hostile-data reads."""
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.seed_data import SPB_SEED_PLACES
from app.models.enums import PlaceCategory
from app.models.place import Place
from app.services.smart_walk_service import SmartWalkService


@pytest.mark.asyncio
async def test_truly_missing_catalog_retains_404(client, db_session):
    await db_session.execute(text('DELETE FROM places'))
    await db_session.commit()
    response = await client.post('/api/v1/routes/propose-walk', json={
        'start_point': {'latitude': 59.94, 'longitude': 30.31},
        'time_budget_minutes': 120,
    })
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_keyed_hostile_canonical_repair_preserves_all_ids(tmp_path, monkeypatch):
    from app.db import session as lifecycle
    engine = create_async_engine(f'sqlite+aiosqlite:///{tmp_path / "keyed-hostile.db"}')
    lifecycle.configure_sqlite_unicode(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(lifecycle, 'engine', engine)
    monkeypatch.setattr(lifecycle, 'async_session_factory', factory)
    try:
        await lifecycle.init_db()
        async with factory() as session:
            before = dict((await session.execute(text('SELECT catalog_key,id FROM places'))).all())
            # High unrelated ID makes accidental canonical deletion/recreation observable.
            await session.execute(text("INSERT INTO places (id,title,short_description,full_description,category,is_unusual,latitude,longitude,address,recommended_visit_minutes) VALUES (999,'Другой объект','Описание','Описание','ART',0,59.94,30.31,'СПб',30)"))
            await session.execute(text("UPDATE places SET latitude=x'00ff', category='corrupt', title=x'00ff' WHERE id=:id"), {'id': before[SPB_SEED_PLACES[0]['catalog_key']]})
            await session.execute(text('UPDATE places SET longitude=1e999 WHERE id=:id'), {'id': before[SPB_SEED_PLACES[1]['catalog_key']]})
            await session.commit()
        await lifecycle.init_db()
        await lifecycle.init_db()
        async with factory() as session:
            rows = (await session.scalars(select(Place))).all()
        canonical = {row.catalog_key: row for row in rows if row.catalog_key}
        assert len(rows) == len(SPB_SEED_PLACES) + 1 and len(canonical) == len(SPB_SEED_PLACES)
        assert {key: row.id for key, row in canonical.items()} == before
        for item in SPB_SEED_PLACES:
            row = canonical[item['catalog_key']]
            assert (row.latitude, row.longitude, row.category, row.title) == (
                item['latitude'], item['longitude'], item['category'], item['title'])
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize('column,value', [
    ('latitude', "x'00ff'"), ('longitude', "'bad'"),
    ('latitude', '1e999'), ('longitude', '-1e999'),
    ('latitude', '91'), ('longitude', '181'),
    ('category', "'unsupported'"), ('category', "x'00ff'"),
    ('recommended_visit_minutes', "'bad'"),
    ('recommended_visit_minutes', "x'00ff'"),
])
async def test_invalid_live_row_cannot_poison_listing_detail_or_walk(client, db_session, column, value):
    await db_session.execute(text("INSERT INTO places (id,title,short_description,full_description,category,is_unusual,latitude,longitude,address,recommended_visit_minutes) VALUES (999,'Стороннее','Описание','Описание','ART',0,59.94,30.31,'СПб',30)"))
    # SQLite CHECK permits text/BLOB storage for visit duration when checks are disabled.
    await db_session.execute(text('PRAGMA ignore_check_constraints=ON'))
    await db_session.execute(text(f'UPDATE places SET {column}={value} WHERE id=999'))
    await db_session.commit()
    try:
        listing = await client.get('/api/v1/places?limit=200')
        assert listing.status_code == 200
        assert listing.json()['total'] == len(listing.json()['items']) == len(SPB_SEED_PLACES)
        assert (await client.get('/api/v1/places/999')).status_code == 404
        response = await client.post('/api/v1/routes/propose-walk', json={
            'start_point': {'latitude': 59.94, 'longitude': 30.31},
            'time_budget_minutes': 120,
        })
        assert response.status_code == 200
        assert 999 not in response.json()['suggested_places_sequence']
    finally:
        await db_session.execute(text('PRAGMA ignore_check_constraints=OFF'))


@pytest.mark.asyncio
async def test_foreign_invalid_category_cannot_abort_startup(tmp_path, monkeypatch):
    from app.db import session as lifecycle
    engine = create_async_engine(f'sqlite+aiosqlite:///{tmp_path / "foreign-hostile.db"}')
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(lifecycle, 'engine', engine)
    monkeypatch.setattr(lifecycle, 'async_session_factory', factory)
    try:
        await lifecycle.init_db()
        async with factory() as session:
            await session.execute(text("INSERT INTO places (id,title,short_description,full_description,category,is_unusual,latitude,longitude,address,recommended_visit_minutes) VALUES (999,'Стороннее','Описание','Описание','corrupt',0,59.94,30.31,'СПб',30)"))
            await session.commit()
        await lifecycle.init_db()
        async with factory() as session:
            rows = (await session.scalars(select(Place))).all()
        assert len(rows) == len(SPB_SEED_PLACES)
        assert {row.catalog_key for row in rows} == {item['catalog_key'] for item in SPB_SEED_PLACES}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize('column', [
    'title', 'short_description', 'full_description', 'address', 'photo_url',
    'tags', 'working_hours', 'architectural_style', 'year_built',
])
async def test_blob_text_is_unavailable_instead_of_decoded_garbage(client, db_session, column):
    await db_session.execute(text(f"UPDATE places SET {column}=x'00ff' WHERE id=1"))
    await db_session.commit()
    listing = await client.get('/api/v1/places?limit=200')
    assert listing.status_code == 200
    detail = await client.get('/api/v1/places/1')
    assert detail.status_code == 200
    data = detail.json()
    if column in ['photo_url', 'tags', 'working_hours', 'architectural_style', 'year_built']:
        assert data[column] is None
    else:
        assert 'недоступ' in data[column].casefold()
    assert '\ufffd' not in str(data) and '\\x00' not in str(data)


@pytest.mark.parametrize('lat,lon,minutes', [
    (419.94, 30.31, 30), (59.94, 390.31, 30),
    (59.94, 30.31, 'bad'), (59.94, 30.31, b'\x00\xff'),
    (59.94, 30.31, -1), (59.94, 30.31, 181),
])
def test_smart_walk_rejects_invalid_spatial_or_visit_values(lat, lon, minutes):
    hostile = Place(id=999, title='Место', short_description='Описание', full_description='Описание',
                    address='СПб', category=PlaceCategory.ART, latitude=lat,
                    longitude=lon, recommended_visit_minutes=minutes, is_unusual=False)
    assert SmartWalkService.propose_walk(59.94, 30.31, 120, [hostile])['status'] == 'empty'


def test_smart_walk_explanation_does_not_interpolate_blob_title():
    hostile = Place(id=999, title=b'\x00\xff', short_description='Описание',
                    full_description='Описание', address='СПб', category=PlaceCategory.ART,
                    latitude=59.955, longitude=30.31, recommended_visit_minutes=30,
                    is_unusual=False)
    result = SmartWalkService.propose_walk(59.94, 30.31, 120, [hostile])
    item = result['items'][0]
    assert 'недоступ' in item['place'].title.casefold()
    assert all('\\x00' not in reason and '\\xff' not in reason for reason in item['reasons'])
    assert any('недоступ' in reason.casefold() for reason in item['reasons'])


@pytest.mark.asyncio
async def test_file_sqlite_pragmas_foreign_keys_and_session_rollback(tmp_path, monkeypatch):
    from app.db import session as lifecycle
    engine = create_async_engine(f'sqlite+aiosqlite:///{tmp_path / "production-pragmas.db"}')
    lifecycle.configure_sqlite_unicode(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(lifecycle, 'async_session_factory', factory)
    try:
        async with engine.begin() as conn:
            values = {key: await conn.scalar(text(f'PRAGMA {key}')) for key in [
                'foreign_keys', 'busy_timeout', 'journal_mode', 'synchronous']}
            assert values == {'foreign_keys': 1, 'busy_timeout': 5000,
                              'journal_mode': 'wal', 'synchronous': 1}
            await conn.execute(text('CREATE TABLE parent(id INTEGER PRIMARY KEY)'))
            await conn.execute(text('CREATE TABLE child(parent_id INTEGER REFERENCES parent(id))'))
            from sqlalchemy.exc import IntegrityError
            with pytest.raises(IntegrityError):
                await conn.execute(text('INSERT INTO child VALUES (99)'))
        dependency = lifecycle.get_db()
        session = await anext(dependency)
        await session.execute(text('INSERT INTO parent VALUES (7)'))
        with pytest.raises(RuntimeError, match='handler failure'):
            await dependency.athrow(RuntimeError('handler failure'))
        async with factory() as fresh:
            assert await fresh.scalar(text('SELECT count(*) FROM parent')) == 0
    finally:
        await engine.dispose()
