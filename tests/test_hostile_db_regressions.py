"""Regressions for hostile/legacy rows in the SQLite catalog.

The database is a local development file: rows edited outside the app (or left
by older tools) can hold values whose storage class does not match the column
(TEXT/BLOB in REAL columns, garbage in numeric/text columns). Such a row must
never take the whole catalog or the walk proposal down with a 500.
"""
import sqlite3

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.dialects.sqlite import dialect as sqlite_dialect
from sqlalchemy.schema import CreateTable

from app.db.base import Base
from app.db.seed_data import SPB_SEED_PLACES
from app.models.enums import PlaceCategory as C
from app.models.place import Place
from app.services.smart_walk_service import SmartWalkService


def place(id, category=C.HISTORY_ARCHITECTURE, lat=59.94, lon=30.31, visit=10):
    return Place(id=id, title=f'Место {id}', category=category, latitude=lat,
                 longitude=lon, recommended_visit_minutes=visit, is_unusual=False,
                 short_description='Описание', full_description='Описание', address='СПб')


@pytest.mark.asyncio
@pytest.mark.parametrize('lat,lon', [('abc', 30.31), (59.94, 'abc')])
async def test_propose_walk_skips_rows_with_non_numeric_coordinates(client, db_session, lat, lon):
    """TEXT survives in REAL columns when the value is not numeric; the proposal
    must exclude such a row instead of crashing on haversine arithmetic."""
    from sqlalchemy import text
    await db_session.execute(text(
        "INSERT INTO places (id,title,short_description,full_description,category,is_unusual,"
        "latitude,longitude,address,recommended_visit_minutes) "
        "VALUES (999,'Мусорная запись','Описание','Описание','ART',0,:lat,:lon,'СПб',30)"),
        {'lat': lat, 'lon': lon})
    await db_session.commit()

    response = await client.post('/api/v1/routes/propose-walk', json={
        'start_point': {'latitude': 59.94, 'longitude': 30.31},
        'time_budget_minutes': 120,
    })
    assert response.status_code == 200, response.text
    sequence = response.json()['suggested_places_sequence']
    assert sequence, 'healthy seed places must still be proposed'
    assert 999 not in sequence


def test_propose_walk_skips_non_finite_coordinates_in_memory():
    hostile = place(1, lat=float('inf'), lon=30.31)
    result = SmartWalkService.propose_walk(59.94, 30.31, 120, [hostile, place(2, lat=59.941)])
    assert result['suggested_places_sequence'] == [2]


@pytest.mark.asyncio
async def test_propose_walk_all_hostile_pool_reports_empty(client, db_session):
    from sqlalchemy import text
    await db_session.execute(text('DELETE FROM places'))
    await db_session.execute(text(
        "INSERT INTO places (id,title,short_description,full_description,category,is_unusual,"
        "latitude,longitude,address,recommended_visit_minutes) "
        "VALUES (999,'Мусорная запись','Описание','Описание','ART',0,'abc','30.31','СПб',30)"))
    await db_session.commit()

    response = await client.post('/api/v1/routes/propose-walk', json={
        'start_point': {'latitude': 59.94, 'longitude': 30.31},
        'time_budget_minutes': 120,
    })
    assert response.status_code == 200
    assert response.json()['status'] == 'empty'


@pytest.mark.asyncio
@pytest.mark.parametrize('column,value', [
    ('photo_url', "x'00ff'"),
    ('rating', "'high'"),
    ('rating', "x'00ff'"),
    ('rating', '7.5'),
])
async def test_listing_survives_hostile_photo_url_and_rating(client, db_session, column, value):
    from sqlalchemy import text
    await db_session.execute(text(
        f"INSERT INTO places (id,title,short_description,full_description,category,is_unusual,"
        f"latitude,longitude,address,recommended_visit_minutes,{column}) "
        f"VALUES (999,'Ок','Описание','Описание','ART',0,59.94,30.31,'СПб',30,{value})"))
    await db_session.commit()

    response = await client.get('/api/v1/places?limit=200')
    assert response.status_code == 200, response.text
    hostile = next(i for i in response.json()['items'] if i['id'] == 999)
    if column == 'rating':
        assert hostile['rating'] is None
    else:
        assert hostile['photo_url'] is None  # arbitrary bytes are not a usable URL


@pytest.mark.asyncio
@pytest.mark.parametrize('lat_raw,lon_raw', [("'abc'", '30.31'), ("x'00ff'", '30.31'),
                                             ('59.94', "x'00ff'")])
async def test_init_db_removes_rows_with_non_numeric_coordinates(tmp_path, monkeypatch,
                                                                 lat_raw, lon_raw):
    """Startup repair drops rows whose coordinates cannot participate in any
    geospatial arithmetic; seed rows are re-created by the sync below it."""
    import app.db.session as lifecycle
    db_path = tmp_path / 'hostile-coordinates.db'
    ddl = str(CreateTable(Place.__table__).compile(dialect=sqlite_dialect()))
    conn = sqlite3.connect(db_path)
    conn.execute(ddl)
    conn.execute(
        'INSERT INTO places (id,title,short_description,full_description,category,is_unusual,'
        'latitude,longitude,address,recommended_visit_minutes) '
        f"VALUES (501,'Сломано','Описание','Описание','ART',0,{lat_raw},{lon_raw},'СПб',30)")
    conn.execute(
        'INSERT INTO places (id,title,short_description,full_description,category,is_unusual,'
        'latitude,longitude,address,recommended_visit_minutes) '
        "VALUES (502,'Здорово','Описание','Описание','ART',0,59.94,30.31,'СПб',30)")
    conn.commit()
    conn.close()

    engine = create_async_engine(f'sqlite+aiosqlite:///{db_path}')
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(lifecycle, 'engine', engine)
    monkeypatch.setattr(lifecycle, 'async_session_factory', factory)
    try:
        await lifecycle.init_db()
        async with factory() as session:
            rows = list((await session.scalars(select(Place))).all())
        titles = {row.title for row in rows}
        assert 'Сломано' not in titles, 'row with non-numeric coordinates must be removed'
        assert 'Здорово' in titles
        assert len(rows) == len(SPB_SEED_PLACES) + 1  # healthy manual row + trusted catalog
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize('raw', ["'false'", "'true'", "'нет'", "x'00ff'", '2'])
async def test_hostile_is_unusual_never_surfaces_as_true_and_filters_stay_consistent(
        client, db_session, raw):
    """SQLite dynamic typing: TEXT/BLOB (and out-of-domain numeric) storage in
    the boolean column must not be read as unusual=true by SQLAlchemy truthiness,
    and such a row must not appear in one filter result while missing from the
    other. The hostile row is excluded from every read until startup repair
    normalizes it (same policy as non-numeric coordinates)."""
    from sqlalchemy import text
    await db_session.execute(text(
        "INSERT INTO places (id,title,short_description,full_description,category,is_unusual,"
        "latitude,longitude,address,recommended_visit_minutes) "
        f"VALUES (998,'Флаг','Описание','Описание','ART',{raw},59.94,30.31,'СПб',30)"))
    await db_session.commit()

    response = await client.get('/api/v1/places?limit=200')
    assert response.status_code == 200, response.text
    assert 998 not in {i['id'] for i in response.json()['items']}, (
        'a hostile boolean must not be published with invented truthiness')
    true_response = await client.get('/api/v1/places?is_unusual=true&limit=200')
    false_response = await client.get('/api/v1/places?is_unusual=false&limit=200')
    assert true_response.status_code == 200 and false_response.status_code == 200
    true_ids = {i['id'] for i in true_response.json()['items']}
    false_ids = {i['id'] for i in false_response.json()['items']}
    assert 998 not in true_ids and 998 not in false_ids
    assert true_ids.isdisjoint(false_ids)


@pytest.mark.asyncio
async def test_is_unusual_filters_split_healthy_rows(client, db_session):
    """Explicit true/false filtering semantics on healthy 0/1 storage."""
    from sqlalchemy import text
    await db_session.execute(text(
        "INSERT INTO places (id,title,short_description,full_description,category,is_unusual,"
        "latitude,longitude,address,recommended_visit_minutes) "
        "VALUES (996,'Обычное','Описание','Описание','ART',0,59.94,30.31,'СПб',30)"))
    await db_session.execute(text(
        "INSERT INTO places (id,title,short_description,full_description,category,is_unusual,"
        "latitude,longitude,address,recommended_visit_minutes) "
        "VALUES (997,'Необычное','Описание','Описание','ART',1,59.94,30.31,'СПб',30)"))
    # REAL 0.0 has NUMERIC column affinity: SQLite canonicalizes it to integer 0,
    # a legitimate false — it must read as false and sit in the false filter only.
    await db_session.execute(text(
        "INSERT INTO places (id,title,short_description,full_description,category,is_unusual,"
        "latitude,longitude,address,recommended_visit_minutes) "
        "VALUES (995,'Ноль','Описание','Описание','ART',0.0,59.94,30.31,'СПб',30)"))
    await db_session.commit()

    true_ids = {i['id'] for i in
                (await client.get('/api/v1/places?is_unusual=true&limit=200')).json()['items']}
    false_ids = {i['id'] for i in
                 (await client.get('/api/v1/places?is_unusual=false&limit=200')).json()['items']}
    assert 997 in true_ids and 997 not in false_ids
    assert 996 in false_ids and 996 not in true_ids
    assert 995 in false_ids and 995 not in true_ids


def test_place_read_never_invents_unusual_from_malformed_value():
    """Last-line schema boundary: only a real boolean (or 0/1) can be unusual."""
    from app.schemas.place import PlaceRead
    for malformed in ('false', 'true', 'любой текст', b'x', 5, 0.0):
        row = Place(id=1, title='t', short_description='d', full_description='d',
                    category=C.ART, latitude=59.94, longitude=30.31,
                    address='СПб', recommended_visit_minutes=30, is_unusual=malformed)
        assert PlaceRead.model_validate(row).is_unusual is False, malformed
    assert PlaceRead.model_validate(place(1)).is_unusual is False
    unusual = place(2)
    unusual.is_unusual = True
    assert PlaceRead.model_validate(unusual).is_unusual is True


def test_smart_walk_scoring_never_receives_malformed_unusual_bonus():
    """The unusual bonus must require a real boolean flag, not truthiness."""
    from app.services.smart_walk_service import is_unusual_flag, score_candidate_place
    for malformed in ('false', 'true', 'текст', b'x', 5):
        hostile = place(1)
        hostile.is_unusual = malformed
        assert is_unusual_flag(hostile) is False
        score, explanations, _ = score_candidate_place(
            hostile, 59.94, 30.31, None, True, [])
        assert 'Необычное атмосферное место СПб' not in explanations
    trusted = place(2)
    trusted.is_unusual = True
    _, explanations, _ = score_candidate_place(trusted, 59.94, 30.31, None, True, [])
    assert 'Необычное атмосферное место СПб' in explanations


@pytest.mark.asyncio
async def test_init_db_repairs_hostile_is_unusual_storage(tmp_path, monkeypatch, caplog):
    """Startup repair normalizes hostile boolean storage to the documented
    default (false) instead of letting truthiness mint unusual=true."""
    import logging
    import app.db.session as lifecycle
    db_path = tmp_path / 'hostile-flag.db'
    ddl = str(CreateTable(Place.__table__).compile(dialect=sqlite_dialect()))
    conn = sqlite3.connect(db_path)
    conn.execute(ddl)
    conn.execute(
        "INSERT INTO places (id,title,short_description,full_description,category,is_unusual,"
        "latitude,longitude,address,recommended_visit_minutes) "
        "VALUES (601,'ТекстФлаг','Описание','Описание','ART','false',59.94,30.31,'СПб',30)")
    conn.execute(
        "INSERT INTO places (id,title,short_description,full_description,category,is_unusual,"
        "latitude,longitude,address,recommended_visit_minutes) "
        "VALUES (602,'ЗдоровыйФлаг','Описание','Описание','ART',0,59.94,30.31,'СПб',30)")
    conn.commit()
    conn.close()

    engine = create_async_engine(f'sqlite+aiosqlite:///{db_path}')
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(lifecycle, 'engine', engine)
    monkeypatch.setattr(lifecycle, 'async_session_factory', factory)
    try:
        with caplog.at_level(logging.WARNING, logger='app.db.startup_repair'):
            await lifecycle.init_db()
        async with factory() as session:
            first = await session.get(Place, 601)
            second = await session.get(Place, 602)
        assert first is not None and first.is_unusual is False
        assert second is not None and second.is_unusual is False
        assert any('hostile_boolean_storage' in record.message for record in caplog.records)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_init_db_removes_orphan_catalog_key_and_logs(tmp_path, monkeypatch, caplog):
    """A keyed row whose catalog_key the current trusted catalogue no longer
    contains is incoherent: keeping it would duplicate the canonical row the
    sync re-creates after an accidental seed key rename. Remove and log."""
    import logging
    import app.db.session as lifecycle
    db_path = tmp_path / 'orphan-key.db'
    ddl = str(CreateTable(Place.__table__).compile(dialect=sqlite_dialect()))
    conn = sqlite3.connect(db_path)
    conn.execute(ddl)
    conn.execute(
        "INSERT INTO places (id,title,short_description,full_description,category,is_unusual,"
        "latitude,longitude,address,recommended_visit_minutes,catalog_key) "
        "VALUES (603,'Сирота','Описание','Описание','ART',0,59.94,30.31,'СПб',30,"
        "'retired-legacy-key')")
    conn.commit()
    conn.close()

    engine = create_async_engine(f'sqlite+aiosqlite:///{db_path}')
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(lifecycle, 'engine', engine)
    monkeypatch.setattr(lifecycle, 'async_session_factory', factory)
    try:
        with caplog.at_level(logging.WARNING, logger='app.db.startup_repair'):
            await lifecycle.init_db()
        async with factory() as session:
            rows = list((await session.scalars(select(Place))).all())
        assert all(row.catalog_key != 'retired-legacy-key' for row in rows)
        assert all(row.id != 603 for row in rows)
        assert len([row for row in rows if row.catalog_key]) == len(SPB_SEED_PLACES), (
            'exactly the trusted catalogue remains keyed')
        assert any('orphan_catalog_identity' in record.message
                   and 'retired-legacy-key' in record.message
                   for record in caplog.records)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_init_db_normalizes_documented_enum_value_form(tmp_path, monkeypatch, caplog):
    """A row written with the documented API enum value ('art') is unambiguous:
    normalize it to the storage name form instead of silently deleting it."""
    import logging
    import app.db.session as lifecycle
    db_path = tmp_path / 'value-form-enum.db'
    ddl = str(CreateTable(Place.__table__).compile(dialect=sqlite_dialect()))
    conn = sqlite3.connect(db_path)
    conn.execute(ddl)
    conn.execute(
        "INSERT INTO places (id,title,short_description,full_description,category,is_unusual,"
        "latitude,longitude,address,recommended_visit_minutes) "
        "VALUES (604,'ФормаЗначения','Описание','Описание','art',0,59.94,30.31,'СПб',30)")
    conn.commit()
    conn.close()

    engine = create_async_engine(f'sqlite+aiosqlite:///{db_path}')
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(lifecycle, 'engine', engine)
    monkeypatch.setattr(lifecycle, 'async_session_factory', factory)
    try:
        with caplog.at_level(logging.WARNING, logger='app.db.startup_repair'):
            await lifecycle.init_db()
        async with factory() as session:
            row = await session.get(Place, 604)
        assert row is not None, 'documented value form must be normalized, not deleted'
        assert row.category is C.ART
        assert any('enum_value_form' in record.message for record in caplog.records)
    finally:
        await engine.dispose()
