"""Expanded data, upgrade identity, API contracts, and unchanged ranking evidence."""
from copy import deepcopy
import math

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.seed_data import SPB_SEED_PLACES
from app.models.enums import PlaceCategory
from app.models.place import Place
from app.schemas.place import PlaceDetail, PlaceRead
from app.services.smart_walk_service import (
    SmartWalkService, calculate_haversine_distance, estimate_walking_minutes,
)
from scripts.validate_phase_b_catalog import (
    ORIGINAL_COUNT, ORIGINAL_KEYS, ORIGINAL_SHA256, ROOT, START_SCENARIOS, TIME_BUDGETS,
    catalog_digest, make_places, nearby_pairs, read_json, scenario_report,
    canonical_identity, point_in_boundary, replay_district_assignments,
    validate_boundary_evidence, validate_catalog, validate_geographic_audit,
)


def catalog_inputs():
    return (read_json(ROOT / 'app/db/seed_places.json')[:77],
            read_json(ROOT / 'docs/osm_source_snapshot.json'))


def test_catalog_matches_final_manifest_and_preserves_original_content():
    items, snapshot = catalog_inputs()
    stats = validate_catalog(items, snapshot)
    manifest = read_json(ROOT / 'docs/phase_b_catalog_manifest.json')
    assert manifest['original_count'] == ORIGINAL_COUNT == 25
    assert manifest['original_catalog_keys'] == list(ORIGINAL_KEYS)
    assert manifest['original_catalog_sha256'] == catalog_digest(items[:25]) == ORIGINAL_SHA256
    assert manifest['accepted_catalog_keys'] == [item['catalog_key'] for item in items]
    assert manifest['catalog_sha256'] == catalog_digest(items)
    for field, value in stats.items():
        assert manifest[field] == value
    assert manifest['added_count'] > 0  # Exact accepted count is documented, never a forced quota.
    assert len(stats['category_counts']) == len(PlaceCategory) == 6


@pytest.mark.parametrize('field,value,error', [
    ('catalog_key', '', 'catalog_key'),
    ('latitude', True, 'finite numbers'),
    ('latitude', float('nan'), 'finite numbers'),
    ('longitude', float('inf'), 'finite numbers'),
    ('latitude', 30.31, 'boundary'),
    ('category', 'new_category', 'category'),
    ('is_unusual', 1, 'boolean'),
    ('recommended_visit_minutes', True, '5..180'),
    ('recommended_visit_minutes', 181, '5..180'),
    ('short_description', 'x' * 501, 'short_description'),
    ('full_description', '', 'full_description'),
    ('why_visit', 'TODO', 'placeholder'),
    ('tags', 'x' * 301, 'tags'),
    ('tags', 'история, История', 'duplicate topic tags'),
    ('source_url', 'https://example.com/node/69424', 'OSM reference'),
    ('source_url', 'https://www.openstreetmap.org/node/0', 'OSM reference'),
    ('source_url', 'https://www.openstreetmap.org/node/999999999999', 'absent from snapshot'),
    ('source_checked_on', 'unverified', 'verification date'),
    ('coordinate_kind', 'osm_node', 'requires a node'),
    ('address_status', 'verified', 'address provenance'),
    ('official_website', 'https://example.org/fabricated-official-site', 'website does not match'),
    ('wikipedia_url', 'https://ru.wikipedia.org/wiki/Fabricated_article', 'Wikipedia URL does not match'),
])
def test_auditor_rejects_invalid_seed_contracts_and_provenance(field, value, error):
    items, snapshot = catalog_inputs()
    candidate = deepcopy(items[0])
    candidate[field] = value
    with pytest.raises(ValueError, match=error):
        validate_catalog([candidate], snapshot, preserve_original=False)


def test_auditor_rejects_duplicate_identity_even_with_distinct_keys():
    items, snapshot = catalog_inputs()
    duplicate = deepcopy(items[0])
    duplicate['catalog_key'] = 'second-name-for-same-osm-object'
    with pytest.raises(ValueError, match='duplicate canonical OSM identity'):
        validate_catalog([items[0], duplicate], snapshot, preserve_original=False)
    duplicate['catalog_key'] = items[0]['catalog_key']
    with pytest.raises(ValueError, match='Duplicate catalog_key'):
        validate_catalog([items[0], duplicate], snapshot, preserve_original=False)


@pytest.mark.parametrize('field,value,error', [
    ('coordinate_source_url', 'https://www.openstreetmap.org/node/1', 'coordinate source disagrees'),
    ('access_point_status', 'verified_main_entrance', 'falsely verified access'),
    ('coordinate_uncertainty', '', 'coordinate_uncertainty'),
    ('verification_evidence', 'TODO', 'verification_evidence'),
    ('district', '', 'district'),
    ('visit_mode', 'instant_photo_stop', 'visit mode'),
    # The message is phase-neutral since C6.1 parameterised the audit date; the
    # default remains the frozen Phase B date.
    ('source_checked_on', '2026-10-03', 'phase verification date'),
])
def test_new_record_provenance_does_not_invent_access_or_drop_verification(field, value, error):
    items, snapshot = catalog_inputs()
    candidate = deepcopy(items[ORIGINAL_COUNT])
    candidate[field] = value
    with pytest.raises(ValueError, match=error):
        validate_catalog([candidate], snapshot, preserve_original=False)


@pytest.mark.parametrize('tag,value', [
    ('access', 'private'), ('access', 'no'), ('foot', 'no'), ('disused', 'yes'),
    ('abandoned', 'yes'), ('disused:tourism', 'museum'),
])
def test_new_restricted_or_inactive_attraction_cannot_be_published_as_accessible(tag, value):
    items, snapshot = catalog_inputs()
    candidate = items[ORIGINAL_COUNT]
    snapshot = deepcopy(snapshot)
    identity = canonical_identity(candidate['source_url'])
    source = next(element for element in snapshot['elements']
                  if (element['type'], element['id']) == identity)
    source['tags'][tag] = value
    with pytest.raises(ValueError, match='restricted or inactive'):
        validate_catalog([candidate], snapshot, preserve_original=False)


def test_two_osm_representations_of_one_wikidata_attraction_are_rejected():
    items, snapshot = catalog_inputs()
    snapshot = deepcopy(snapshot)
    sources = {(element['type'], element['id']): element for element in snapshot['elements']}
    first, second = items[0], items[ORIGINAL_COUNT]
    assert first['source_url'] != second['source_url']
    sources[canonical_identity(second['source_url'])]['tags']['wikidata'] = (
        sources[canonical_identity(first['source_url'])]['tags']['wikidata'])
    with pytest.raises(ValueError, match='duplicate canonical Wikidata'):
        validate_catalog([first, second], snapshot, preserve_original=False)


@pytest.mark.parametrize('corruption', ['coordinated-center-change', 'missing-bounds'])
def test_locally_derived_bounds_centers_are_recomputed_from_source_bounds(corruption):
    items, snapshot = catalog_inputs()
    snapshot = deepcopy(snapshot)
    sources = {(element['type'], element['id']): element for element in snapshot['elements']}
    candidate = deepcopy(next(item for item in items[ORIGINAL_COUNT:]
                              if 'center_derivation' in sources[canonical_identity(item['source_url'])]))
    source = sources[canonical_identity(candidate['source_url'])]
    if corruption == 'coordinated-center-change':
        candidate['latitude'] += .001
        source['center']['lat'] += .001  # Matching fake seed/snapshot coordinates still fail bounds replay.
        error = 'bounds midpoint'
    else:
        del source['bounds']
        error = 'derived bounds-center evidence'
    with pytest.raises(ValueError, match=error):
        validate_catalog([candidate], snapshot, preserve_original=False)


def test_boundary_replay_respects_open_member_segments_and_inner_holes():
    # Four separate outer ways must be joined by their real endpoints, never individually closed.
    outer = [[0, 0], [0, 10], [10, 10], [10, 0], [0, 0]]
    boundary = {'ways': [{'osm_way_id': index + 1, 'role': 'outer',
                         'coordinates': outer[index:index + 2]} for index in range(4)]}
    boundary['ways'].append({'osm_way_id': 5, 'role': 'inner',
                             'coordinates': [[3, 3], [3, 7], [7, 7], [7, 3], [3, 3]]})
    assert point_in_boundary(1, 1, boundary)
    assert not point_in_boundary(5, 5, boundary)
    assert not point_in_boundary(-1, 1, boundary)
    with pytest.raises(ValueError, match='lies on a district boundary'):
        point_in_boundary(0, 1, boundary)


def test_boundary_snapshot_cannot_silently_drop_a_member_segment():
    evidence = read_json(ROOT / 'docs/phase_b_boundary_evidence.json')
    validate_boundary_evidence(evidence)
    incomplete = deepcopy(evidence)
    incomplete['boundaries'][0]['ways'].pop(0)
    with pytest.raises(ValueError, match='closed rings'):
        validate_boundary_evidence(incomplete)


@pytest.mark.parametrize('corruption,error', [
    ('wrong-district', 'boundary replay'), ('stale-coordinate', 'stale catalog coordinates'),
    ('missing-identity', 'each catalog identity'),
])
def test_administrative_assignments_are_checked_against_source_geometry(corruption, error):
    items, _ = catalog_inputs()
    assignments = read_json(ROOT / 'docs/phase_b_district_assignments.json')
    evidence = read_json(ROOT / 'docs/phase_b_boundary_evidence.json')
    first = assignments[items[0]['catalog_key']]
    if corruption == 'wrong-district':
        first['district'] = 'Приморский район'
    elif corruption == 'stale-coordinate':
        first['longitude'] += .01
    else:
        del assignments[items[0]['catalog_key']]
    with pytest.raises(ValueError, match=error):
        replay_district_assignments(items, assignments, evidence)


def test_new_editorial_district_must_match_independent_boundary_replay():
    items, _ = catalog_inputs()
    items[ORIGINAL_COUNT]['district'] = 'Центральный район'
    with pytest.raises(ValueError, match='new seed district disagrees'):
        replay_district_assignments(items, read_json(ROOT / 'docs/phase_b_district_assignments.json'),
                                   read_json(ROOT / 'docs/phase_b_boundary_evidence.json'))
def test_nearby_different_sources_require_review_and_are_not_automatically_duplicates():
    items, _ = catalog_inputs()
    left, right = deepcopy(items[0]), deepcopy(items[1])
    right['latitude'], right['longitude'] = left['latitude'], left['longitude'] + .0001
    assert left['source_url'] != right['source_url']
    pairs = nearby_pairs([left, right])
    assert len(pairs) == 1 and 0 < pairs[0]['distance_meters'] < 120
    assert pairs[0]['catalog_keys'] == sorted([left['catalog_key'], right['catalog_key']])
    assert 'classification' not in pairs[0]  # Distance flags review; it does not establish identity.


def test_geographic_assignments_and_all_nearby_pairs_have_review_evidence():
    items, _ = catalog_inputs()
    audit = read_json(ROOT / 'docs/phase_b_geographic_audit.json')
    validate_geographic_audit(items, audit)
    incomplete = deepcopy(audit)
    incomplete['places'].pop()
    with pytest.raises(ValueError, match='every catalog identity'):
        validate_geographic_audit(items, incomplete)
    if audit['nearby_pairs']:
        incomplete = deepcopy(audit)
        incomplete['nearby_pairs'][0]['rationale'] = None
        with pytest.raises(ValueError, match='identity review'):
            validate_geographic_audit(items, incomplete)


@pytest.mark.asyncio
@pytest.mark.parametrize('existing_25', [False, True], ids=['fresh', 'keyed-25-upgrade'])
async def test_expansion_keeps_original_ids_user_rows_saved_references_and_is_idempotent(
        tmp_path, monkeypatch, existing_25):
    from app.db import session as lifecycle
    engine = create_async_engine(f'sqlite+aiosqlite:///{tmp_path / "catalog.db"}')
    lifecycle.configure_sqlite_unicode(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(lifecycle, 'engine', engine)
    monkeypatch.setattr(lifecycle, 'async_session_factory', factory)
    try:
        if existing_25:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            originals = make_places(catalog_inputs()[0][:ORIGINAL_COUNT])
            async with factory() as session:
                for index, place in enumerate(originals):
                    place.id = 101 + index * 7  # Non-contiguous historical IDs must stay intact.
                session.add_all(originals)
                await session.commit()
        else:
            await lifecycle.init_db()
        async with factory() as session:
            before = dict((await session.execute(select(Place.catalog_key, Place.id).where(
                Place.catalog_key.in_(ORIGINAL_KEYS)))).all())
            assert len(before) == ORIGINAL_COUNT
            outsider = Place(id=9001, title='Личная запись пользователя',
                             short_description='Описание пользователя', full_description='Личное описание',
                             category=PlaceCategory.ART, latitude=59.94, longitude=30.31,
                             address='Условная точка', is_unusual=False,
                             recommended_visit_minutes=20, tags='пользовательский тег')
            session.add(outsider)
            await session.commit()
        async with engine.begin() as connection:
            await connection.execute(text('CREATE TABLE saved_walk_references '
                                          '(position INTEGER PRIMARY KEY, place_id INTEGER NOT NULL REFERENCES places(id))'))
            saved = [(0, before[ORIGINAL_KEYS[0]]), (1, before[ORIGINAL_KEYS[-1]]), (2, 9001)]
            for position, place_id in saved:
                await connection.execute(text('INSERT INTO saved_walk_references VALUES (:position,:place_id)'),
                                         {'position': position, 'place_id': place_id})
        await lifecycle.init_db()
        await lifecycle.check_database_ready()
        async with factory() as session:
            once = dict((await session.execute(select(Place.catalog_key, Place.id).where(
                Place.catalog_key.is_not(None)))).all())
        await lifecycle.init_db()
        await lifecycle.init_db()
        async with factory() as session:
            rows = list((await session.scalars(select(Place))).all())
            after = {row.catalog_key: row.id for row in rows if row.catalog_key}
            assert after == once and len(rows) == len(SPB_SEED_PLACES) + 1
            assert {key: after[key] for key in ORIGINAL_KEYS} == before
            if existing_25:
                assert before == {key: 101 + index * 7 for index, key in enumerate(ORIGINAL_KEYS)}
                assert all(after[item['catalog_key']] > 9001 for item in SPB_SEED_PLACES[ORIGINAL_COUNT:])
            else:
                assert before == {key: index + 1 for index, key in enumerate(ORIGINAL_KEYS)}
            outsider = await session.get(Place, 9001)
            assert outsider.catalog_key is None and outsider.source_url is None
            assert (outsider.title, outsider.full_description, outsider.tags) == (
                'Личная запись пользователя', 'Личное описание', 'пользовательский тег')
            assert list((await session.execute(text(
                'SELECT position,place_id FROM saved_walk_references ORDER BY position'))).all()) == saved
            assert (await session.execute(text('PRAGMA foreign_key_check'))).all() == []
            for item in SPB_SEED_PLACES:
                row = next(row for row in rows if row.catalog_key == item['catalog_key'])
                assert row.source_url == item['source_url']
                assert row.coordinate_kind == item['coordinate_kind']
                assert row.recommended_visit_minutes == item['recommended_visit_minutes']
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_expanded_api_pagination_has_no_missing_or_duplicate_records_and_same_shape(client):
    ids = []
    for offset in range(0, len(SPB_SEED_PLACES), 7):
        response = await client.get('/api/v1/places', params={'limit': 7, 'offset': offset})
        assert response.status_code == 200
        payload = response.json()
        assert payload['total'] == len(SPB_SEED_PLACES)
        assert len(payload['items']) == min(7, len(SPB_SEED_PLACES) - offset)
        for item in payload['items']:
            assert set(item) == set(PlaceRead.model_fields)
            ids.append(PlaceRead.model_validate(item).id)
    assert ids == list(range(1, len(SPB_SEED_PLACES) + 1))
    assert (await client.get('/api/v1/places', params={'offset': len(ids)})).json()['items'] == []
    # Detail metadata for every original and added row travels through the existing response schema.
    for place_id in ids:
        response = await client.get(f'/api/v1/places/{place_id}')
        assert response.status_code == 200
        payload = response.json()
        assert set(payload) == set(PlaceDetail.model_fields)
        detail = PlaceDetail.model_validate(payload)
        item = SPB_SEED_PLACES[place_id - 1]
        assert detail.source_url == item['source_url']
        assert detail.why_visit == item['why_visit']


@pytest.mark.asyncio
@pytest.mark.parametrize('category', list(PlaceCategory))
async def test_expanded_api_category_unusual_filters_report_exact_catalog_count(client, category):
    expected = [index + 1 for index, item in enumerate(SPB_SEED_PLACES)
                if item['category'] == category and item['is_unusual']]
    params = {'category': category.value, 'is_unusual': 'true', 'limit': 3}
    response = await client.get('/api/v1/places', params=params)
    assert response.status_code == 200
    payload = response.json()
    assert payload['total'] == len(expected)
    assert [item['id'] for item in payload['items']] == expected[:3]
    params['offset'] = 3
    second = (await client.get('/api/v1/places', params=params)).json()
    assert second['total'] == len(expected)
    assert [item['id'] for item in second['items']] == expected[3:6]


@pytest.mark.parametrize('scenario,district,start_lat,start_lon', START_SCENARIOS,
                         ids=[scenario[0] for scenario in START_SCENARIOS])
@pytest.mark.parametrize('budget', TIME_BUDGETS)
def test_expanded_smart_walk_is_deterministic_and_has_honest_geometric_budget(
        scenario, district, start_lat, start_lon, budget):
    places = make_places(catalog_inputs()[0])
    result = SmartWalkService.propose_walk(start_lat, start_lon, budget, places)
    reordered = SmartWalkService.propose_walk(start_lat, start_lon, budget, list(reversed(places)))
    assert result == reordered
    sequence = result['suggested_places_sequence']
    assert len(sequence) == len(set(sequence)) <= 6
    assert result['estimated_total_duration_minutes'] <= budget
    if not sequence:
        assert result['status'] == 'empty' and result['items'] == []
        assert result['estimated_walking_duration_seconds'] == 0
        assert result['estimated_visit_duration_minutes'] == 0
        return
    assert result['status'] == 'success'
    selected = [next(place for place in places if place.id == place_id) for place_id in sequence]
    distance = 0.0
    lat, lon = start_lat, start_lon
    for place in selected:
        distance += calculate_haversine_distance(lat, lon, place.latitude, place.longitude)
        lat, lon = place.latitude, place.longitude
    visits = sum(place.recommended_visit_minutes for place in selected)
    seconds = math.ceil(estimate_walking_minutes(distance) * 60)
    assert result['estimated_total_distance_meters'] == round(distance, 1)
    assert result['estimated_walking_duration_seconds'] == seconds
    assert result['estimated_visit_duration_minutes'] == visits
    assert result['estimated_total_duration_minutes'] == math.ceil(seconds / 60 + visits)
    assert [item['place'].id for item in result['items']] == sequence
    assert all(item['reasons'] for item in result['items'])


def test_committed_smart_walk_comparison_is_reproducible():
    items, _ = catalog_inputs()
    report = read_json(ROOT / 'docs/phase_b_smart_walk_scenarios.json')
    assert report == scenario_report(items)
    assert len(report['cases']) == len(START_SCENARIOS) * len(TIME_BUDGETS) == 24
