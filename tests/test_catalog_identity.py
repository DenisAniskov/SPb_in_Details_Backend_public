"""Reviewed tourist content must stay with a stable catalog record."""
import sqlite3

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.seed_data import SPB_SEED_PLACES
from app.models.place import Place


def test_catalog_keys_are_unique_and_independent_of_titles():
    assert len({item['catalog_key'] for item in SPB_SEED_PLACES}) == len(SPB_SEED_PLACES)
    assert all(item['catalog_key'] != item['title'] for item in SPB_SEED_PLACES)


@pytest.mark.asyncio
async def test_editorial_content_survives_database_title_changes(client, db_session):
    item = SPB_SEED_PLACES[0]
    place = await db_session.scalar(select(Place).where(Place.catalog_key == item['catalog_key']))
    place.title = 'Новое отображаемое название'
    await db_session.commit()
    response = await client.get(f'/api/v1/places/{place.id}')
    assert response.status_code == 200
    data = response.json()
    assert data['title'] == place.title
    assert data['why_visit'] == item['why_visit']
    assert data['visit_note'] == item['visit_note']
    assert data['official_website'] == item['official_website']
    assert data['source_url'] == item['source_url']


@pytest.mark.asyncio
async def test_unrelated_same_title_or_coordinates_do_not_inherit_metadata(client, db_session):
    item = SPB_SEED_PLACES[0]
    outsider = Place(
        title=item['title'], short_description='Другой объект', full_description='Другой объект',
        category=item['category'], latitude=item['latitude'], longitude=item['longitude'],
        address=item['address'], recommended_visit_minutes=30,
    )
    db_session.add(outsider)
    await db_session.commit()
    data = (await client.get(f'/api/v1/places/{outsider.id}')).json()
    assert data['source_url'] is None
    assert data['why_visit'] is None
    assert data['visit_note'] is None
    assert data['official_website'] is None
    assert data['wikipedia_url'] is None
    assert data['coordinate_kind'] == data['address_status'] == 'unverified'


@pytest.mark.asyncio
async def test_modified_coordinates_are_not_claimed_as_verified(client, db_session):
    item = SPB_SEED_PLACES[0]
    place = await db_session.scalar(select(Place).where(Place.catalog_key == item['catalog_key']))
    place.latitude += .01
    await db_session.commit()
    data = (await client.get(f'/api/v1/places/{place.id}')).json()
    assert data['coordinate_kind'] == 'unverified'
    assert data['why_visit'] == item['why_visit']


@pytest.mark.asyncio
async def test_seed_rename_and_same_title_row_preserve_keyed_ids(tmp_path, monkeypatch):
    from app.db import session as lifecycle
    engine = create_async_engine(f'sqlite+aiosqlite:///{tmp_path / "rename.db"}')
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(lifecycle, 'engine', engine)
    monkeypatch.setattr(lifecycle, 'async_session_factory', factory)
    item = SPB_SEED_PLACES[0]
    try:
        await lifecycle.init_db()
        async with factory() as session:
            place = await session.scalar(select(Place).where(Place.catalog_key == item['catalog_key']))
            original_id = place.id
            place.title = SPB_SEED_PLACES[1]['title']
            outsider = Place(title=item['title'], short_description='Сторонняя запись',
                             full_description='Сторонняя запись', category=item['category'],
                             latitude=55.75, longitude=37.61, address='Другой адрес')
            session.add(outsider)
            await session.commit()
            outsider_id = outsider.id
        monkeypatch.setitem(item, 'title', 'Уточнённое название в каталоге')
        await lifecycle.init_db()
        await lifecycle.init_db()
        async with factory() as session:
            assert await session.scalar(select(func.count(Place.id))) == len(SPB_SEED_PLACES) + 1
            place = await session.get(Place, original_id)
            assert place.catalog_key == item['catalog_key']
            assert place.title == item['title']
            assert place.why_visit == item['why_visit']
            outsider = await session.get(Place, outsider_id)
            assert outsider.catalog_key is None
            assert outsider.title != item['title']
            assert outsider.address == 'Другой адрес'
    finally:
        await engine.dispose()


def _old_database(path):
    connection = sqlite3.connect(path)
    connection.execute('''CREATE TABLE places (
        id INTEGER PRIMARY KEY, title VARCHAR(200) NOT NULL,
        short_description VARCHAR(500) NOT NULL, full_description TEXT NOT NULL,
        category VARCHAR(30) NOT NULL, is_unusual BOOLEAN,
        latitude FLOAT NOT NULL, longitude FLOAT NOT NULL, address VARCHAR(300) NOT NULL,
        photo_url VARCHAR(500), working_hours VARCHAR(200), tags VARCHAR(300), rating FLOAT,
        architectural_style VARCHAR(150), year_built VARCHAR(50)
    )''')
    return connection


def _insert_legacy(connection, item, id, title=None, latitude=None, longitude=None):
    connection.execute('''INSERT INTO places
        (id,title,short_description,full_description,category,is_unusual,latitude,longitude,address)
        VALUES (?,?,?,?,?,?,?,?,?)''', (
            id, title or item['title'], item['short_description'], item['full_description'],
            item['category'].name, item['is_unusual'],
            item['latitude'] if latitude is None else latitude,
            item['longitude'] if longitude is None else longitude, item['address'],
        ))


@pytest.mark.asyncio
async def test_original_seed_migration_preserves_all_ids_and_leaves_lookalikes(tmp_path, monkeypatch):
    from app.db import session as lifecycle
    path = tmp_path / 'old-catalog.db'
    connection = _old_database(path)
    # This fixture represents the real pre-expansion catalog, not new Phase B rows.
    original_places = SPB_SEED_PLACES[:25]
    for index, item in enumerate(original_places):
        lat, lon = item['legacy_coordinates'][0]
        title = item.get('legacy_titles', [item['title']])[0]
        _insert_legacy(connection, item, 100 + index, title, lat, lon)
    _insert_legacy(connection, SPB_SEED_PLACES[0], 200, latitude=55.75, longitude=37.61)
    _insert_legacy(connection, SPB_SEED_PLACES[0], 201, title='Соседний сторонний объект')
    connection.execute('CREATE TABLE saved_walk_references (place_id INTEGER REFERENCES places(id))')
    connection.executemany('INSERT INTO saved_walk_references VALUES (?)', [(100,), (124,), (200,)])
    connection.commit()
    connection.close()
    engine = create_async_engine(f'sqlite+aiosqlite:///{path}')
    lifecycle.configure_sqlite_unicode(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(lifecycle, 'engine', engine)
    monkeypatch.setattr(lifecycle, 'async_session_factory', factory)
    try:
        await lifecycle.init_db()
        await lifecycle.init_db()
        async with factory() as session:
            assert await session.scalar(select(func.count(Place.id))) == len(SPB_SEED_PLACES) + 2
            for index, item in enumerate(original_places):
                place = await session.get(Place, 100 + index)
                assert place.catalog_key == item['catalog_key']
                assert place.title == item['title']
                assert place.latitude == item['latitude']
                assert place.longitude == item['longitude']
                assert place.why_visit == item['why_visit']
            for id in [200, 201]:
                outsider = await session.get(Place, id)
                assert outsider.catalog_key is None
                assert outsider.source_url is None
            from sqlalchemy import text
            assert (await session.scalars(text('SELECT place_id FROM saved_walk_references ORDER BY place_id'))).all() == [100, 124, 200]
            assert (await session.execute(text('PRAGMA foreign_key_check'))).all() == []
        # Migration enforces the same unique key constraint as fresh installs.
        connection = sqlite3.connect(path)
        try:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute('UPDATE places SET catalog_key=? WHERE id=200',
                                   (SPB_SEED_PLACES[0]['catalog_key'],))
        finally:
            connection.close()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_ambiguous_legacy_rows_are_not_silently_claimed(tmp_path, monkeypatch):
    from app.db import session as lifecycle
    path = tmp_path / 'ambiguous-catalog.db'
    connection = _old_database(path)
    for id in [50, 51]:
        _insert_legacy(connection, SPB_SEED_PLACES[0], id)
    connection.commit()
    connection.close()
    engine = create_async_engine(f'sqlite+aiosqlite:///{path}')
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(lifecycle, 'engine', engine)
    monkeypatch.setattr(lifecycle, 'async_session_factory', factory)
    try:
        await lifecycle.init_db()
        await lifecycle.init_db()
        async with factory() as session:
            assert await session.scalar(select(func.count(Place.id))) == len(SPB_SEED_PLACES) + 2
            assert (await session.get(Place, 50)).catalog_key is None
            assert (await session.get(Place, 51)).catalog_key is None
            reviewed = await session.scalar(select(Place).where(
                Place.catalog_key == SPB_SEED_PLACES[0]['catalog_key']))
            assert reviewed.id not in [50, 51]
    finally:
        await engine.dispose()
