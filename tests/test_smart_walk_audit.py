import math
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.db.seed_data import SPB_SEED_PLACES
from app.models.enums import PlaceCategory as C
from app.models.place import Place
from app.schemas.place import PlaceRead
from app.services.smart_walk_service import (
    SmartWalkService, calculate_haversine_distance as distance,
    estimate_walking_minutes, score_candidate_place, two_opt_optimize_route,
)


def place(id, category=C.HISTORY_ARCHITECTURE, lat=59.94, lon=30.31, visit=10, unusual=False):
    return Place(id=id, title=f'Место {id}', category=category, latitude=lat,
                 longitude=lon, recommended_visit_minutes=visit, is_unusual=unusual,
                 short_description='Описание', full_description='Описание', address='СПб')


def proposal(places, budget=120, categories=None, unusual=False, lat=59.939, lon=30.31):
    return SmartWalkService.propose_walk(lat, lon, budget, places, categories, unusual)


def length(start, places):
    coords = [start] + [(p.latitude, p.longitude) for p in places]
    return sum(distance(*a, *b) for a, b in zip(coords, coords[1:]))


def test_haversine_units_symmetry_zero_and_antipodes():
    assert distance(59.94, 30.31, 59.94, 30.31) == 0
    assert distance(0, 0, 0, 1) == pytest.approx(111194.9266, abs=.1)
    assert distance(59.94, 30.31, 59.93, 30.36) == pytest.approx(distance(59.93, 30.36, 59.94, 30.31))
    assert distance(0, 0, 0, 180) == pytest.approx(math.pi * 6371000)
    assert estimate_walking_minutes(4500) == 60


def test_two_opt_preserves_ids_nonincreasing_deterministic_and_start():
    places = [place(i + 1, lat=lat, lon=lon) for i, (lat, lon) in enumerate([
        (59.95, 30.38), (59.94, 30.31), (59.945, 30.34), (59.935, 30.32), (59.95, 30.32)])]
    original = list(places)
    optimized = two_opt_optimize_route(59.94, 30.31, places)
    assert sorted(p.id for p in optimized) == sorted(p.id for p in places)
    assert length((59.94, 30.31), optimized) <= length((59.94, 30.31), places)
    assert optimized == two_opt_optimize_route(59.94, 30.31, places)
    assert places == original
    pair = [place(1, lat=59.95), place(2)]
    assert two_opt_optimize_route(59.94, 30.31, pair)[0].id == 2


def test_scoring_soft_unusual_preference_and_stable_ties():
    normal = place(1)
    unusual = place(2, unusual=True)
    assert score_candidate_place(normal, 59.94, 30.31, ['history_architecture'], False, [])[0] == 92
    assert score_candidate_place(unusual, 59.94, 30.31, ['history_architecture'], True, [])[0] == 96
    assert score_candidate_place(unusual, 59.94, 30.31, ['history_architecture'], False, [])[0] == 92
    selected = proposal([unusual, normal], budget=30, unusual=True)
    assert set(selected['suggested_places_sequence']) == {1, 2}  # preference, not filter
    assert proposal([place(2), place(1)], budget=30) == proposal([place(1), place(2)], budget=30)


def test_category_no_fallback_and_feasible_diversity():
    candidates = [place(1), place(2), place(3, C.ART, lat=59.955, lon=30.34)]
    result = proposal(candidates, budget=120, categories=['history_architecture', 'art'])
    assert {i['place'].category for i in result['items']} == {C.HISTORY_ARCHITECTURE, C.ART}
    assert proposal([place(1)], categories=['art'])['status'] == 'empty'
    assert all(i['place'].category == C.ART for i in proposal(candidates, categories=['art'])['items'])


@pytest.mark.parametrize('categories', [None, [], ['art'], ['art', 'history_architecture'], [c.value for c in C]])
@pytest.mark.parametrize('budget', [30, 240])
def test_budget_visit_included_max_six_no_duplicates(categories, budget):
    candidates = [place(i + 1, list(C)[i % 6], visit=10) for i in range(9)]
    candidates.append(candidates[0])
    result = proposal(candidates, budget=budget, categories=categories)
    ids = result['suggested_places_sequence']
    assert len(ids) == len(set(ids)) <= 6
    assert result['estimated_total_duration_minutes'] <= budget
    assert result['estimated_total_duration_minutes'] == math.ceil(
        result['estimated_walking_duration_seconds'] / 60 + result['estimated_visit_duration_minutes'])
    assert result['estimated_visit_duration_minutes'] == 10 * len(ids)


def test_impossible_remote_short_budget_one_and_empty():
    assert proposal([place(1, visit=50)], budget=30)['items'] == []
    assert proposal([place(1)], lat=0, lon=0, budget=240)['status'] == 'empty'
    assert len(proposal([place(1)], budget=30)['items']) == 1
    assert proposal([])['items'] == []


def test_final_order_explanations_recomputed_not_pedestrian_claims():
    candidates = [place(1, lat=59.945), place(2, C.ART, lat=59.941), place(3, lat=59.943)]
    result = proposal(candidates, categories=['art', 'history_architecture'])
    lat, lon, visited = 59.939, 30.31, []
    for item in result['items']:
        p = next(p for p in candidates if p.id == item['place'].id)
        expected = score_candidate_place(p, lat, lon, ['art', 'history_architecture'], False, visited)[1]
        assert item['reasons'] == expected
        assert not any('пешей доступности' in r for r in item['reasons'])
        lat, lon = p.latitude, p.longitude
        visited.append(p.category.value)


@pytest.mark.asyncio
async def test_endpoint_validation_and_actual_place_minutes(client):
    response = await client.get('/api/v1/places')
    assert all(5 <= p['recommended_visit_minutes'] <= 180 for p in response.json()['items'])
    response = await client.post('/api/v1/routes/propose-walk', json={
        'start_point': {'latitude': 59.94, 'longitude': 30.31}, 'categories': ['wrong']})
    assert response.status_code == 422
    for minutes in [0, 181, 3.5]:
        data = dict(id=1, title='Место', short_description='Описание', category='art',
                    latitude=59.94, longitude=30.31, address='СПб', recommended_visit_minutes=minutes)
        with pytest.raises(ValidationError): PlaceRead(**data)


@pytest.mark.asyncio
@pytest.mark.parametrize('existing', [False, True])
async def test_clean_install_and_old_sqlite_migration(tmp_path, monkeypatch, existing):
    import app.db.session as lifecycle
    db_path = tmp_path / 'upgrade.db'
    if existing:
        # Existing schema, including unrelated ID, with no new column.
        ddl = str(__import__('sqlalchemy').schema.CreateTable(Place.__table__).compile(
            dialect=__import__('sqlalchemy').dialects.sqlite.dialect()))
        ddl = '\n'.join(line for line in ddl.splitlines() if 'recommended_visit_minutes' not in line)
        ddl = ddl.replace('PRIMARY KEY (id),', 'PRIMARY KEY (id)')
        conn = sqlite3.connect(db_path)
        conn.execute(ddl)
        item = dict(SPB_SEED_PLACES[0])
        fields = ['title','short_description','full_description','category','latitude','longitude','address','is_unusual']
        values = [item[f].name if f == 'category' else item[f] for f in fields]
        conn.execute(f"INSERT INTO places(id,{','.join(fields)}) VALUES(77,{','.join('?' for _ in fields)})", values)
        values[0] = 'Стороннее место'
        conn.execute(f"INSERT INTO places(id,{','.join(fields)}) VALUES(88,{','.join('?' for _ in fields)})", values)
        conn.commit(); conn.close()
    engine = create_async_engine(f'sqlite+aiosqlite:///{db_path}')
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(lifecycle, 'engine', engine)
    monkeypatch.setattr(lifecycle, 'async_session_factory', factory)
    try:
        await lifecycle.init_db()
        await lifecycle.init_db()  # Idempotent, IDs preserved.
        async with factory() as session:
            rows = list((await session.scalars(select(Place))).all())
        assert len(rows) == len(SPB_SEED_PLACES) + int(existing)
        assert all(5 <= p.recommended_visit_minutes <= 180 for p in rows)
        if existing:
            assert next(p for p in rows if p.title == SPB_SEED_PLACES[0]['title']).id == 77
            assert next(p for p in rows if p.id == 88).recommended_visit_minutes == 30
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_real_local_db_copy_upgrade(tmp_path, monkeypatch):
    import app.db.session as lifecycle
    source = Path(__file__).parents[1] / 'smart_navigator_spb.db'
    if not source.exists(): pytest.skip('Local DB not present')
    target = tmp_path / 'local-copy.db'
    original = sqlite3.connect(source)
    copy = sqlite3.connect(target)
    original.backup(copy); original.close(); copy.close()
    engine = create_async_engine(f'sqlite+aiosqlite:///{target}')
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(lifecycle, 'engine', engine)
    monkeypatch.setattr(lifecycle, 'async_session_factory', factory)
    try:
        await lifecycle.init_db()
        async with factory() as session:
            rows = list((await session.scalars(select(Place))).all())
        assert len(rows) >= len(SPB_SEED_PLACES)
        assert all(p.recommended_visit_minutes is not None for p in rows)
    finally: await engine.dispose()


@pytest.mark.asyncio
async def test_existing_column_repairs_invalid_legacy_values(tmp_path, monkeypatch):
    import app.db.session as lifecycle
    from sqlalchemy.schema import CreateTable
    from sqlalchemy.dialects.sqlite import dialect
    path = tmp_path / 'invalid-values.db'
    ddl = str(CreateTable(Place.__table__).compile(dialect=dialect()))
    ddl = '\n'.join(line for line in ddl.splitlines() if 'CHECK (' not in line)
    ddl = ddl.replace('recommended_visit_minutes INTEGER DEFAULT \'30\' NOT NULL',
                      'recommended_visit_minutes INTEGER')
    ddl = ddl.replace('PRIMARY KEY (id),', 'PRIMARY KEY (id)')
    conn = sqlite3.connect(path); conn.execute(ddl)
    for id, value in [(90, None), (91, -1), (92, 200), (93, 12.5)]:
        conn.execute('INSERT INTO places (id,title,short_description,full_description,category,is_unusual,latitude,longitude,address,recommended_visit_minutes) VALUES(?,?,?,?,?,?,?,?,?,?)',
                     (id, f'Стороннее {id}', 'Описание', 'Описание', 'ART', False, 59.94, 30.31, 'СПб', value))
    conn.commit(); conn.close()
    engine = create_async_engine(f'sqlite+aiosqlite:///{path}')
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(lifecycle, 'engine', engine)
    monkeypatch.setattr(lifecycle, 'async_session_factory', factory)
    try:
        await lifecycle.init_db()
        async with factory() as session:
            rows = list((await session.scalars(select(Place).where(Place.id.in_([90,91,92,93])))).all())
        assert len(rows) == 4 and all(p.recommended_visit_minutes == 30 for p in rows)
    finally: await engine.dispose()
