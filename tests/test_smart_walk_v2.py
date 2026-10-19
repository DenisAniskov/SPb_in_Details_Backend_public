"""Locality, greedy-trap recovery, honest failures and bounded v2 contracts."""
import math

import pytest
from pydantic import ValidationError

from app.models.enums import PlaceCategory as C
from app.models.place import Place
from app.schemas.route import AutoWalkProposalResponse
from app.services import smart_walk_service as service
from app.services.smart_walk_service import SmartWalkService, calculate_haversine_distance


def place(identity, latitude=59.942, longitude=30.31, visit=10, category=C.HISTORY_ARCHITECTURE,
          unusual=False, title=None, key=None):
    return Place(id=identity, title=title or f"Место {identity}", catalog_key=key,
                 short_description="Описание", full_description="Описание", address="СПб",
                 category=category, latitude=latitude, longitude=longitude,
                 recommended_visit_minutes=visit, is_unusual=unusual)


def propose(places, budget=60, categories=None, unusual=True, **kwargs):
    return SmartWalkService.propose_walk(59.94, 30.31, budget, places, categories, unusual, **kwargs)


def assert_candidate(candidate, places, budget):
    selected = {item.id: item for item in places}
    sequence = candidate['suggested_places_sequence']
    assert 1 <= len(sequence) == len(set(sequence)) <= 6
    assert [item['place'].id for item in candidate['items']] == sequence
    distance, visits, latitude, longitude = 0.0, 0, 59.94, 30.31
    for identity in sequence:
        item = selected[identity]
        distance += calculate_haversine_distance(latitude, longitude, item.latitude, item.longitude)
        visits += item.recommended_visit_minutes or 30
        latitude, longitude = item.latitude, item.longitude
    seconds = math.ceil(distance / 75 * 60)
    assert candidate['estimated_total_distance_meters'] == round(distance, 1)
    assert candidate['estimated_walking_duration_seconds'] == seconds
    assert candidate['estimated_visit_duration_minutes'] == visits
    assert candidate['estimated_total_duration_minutes'] == math.ceil(seconds / 60 + visits) <= budget
    assert seconds + visits * 60 <= budget * 60


def test_beam_recovers_two_local_stops_after_long_unusual_greedy_trap():
    long = place(1, latitude=59.941, visit=50, unusual=True)
    nearby = [place(2, latitude=59.942), place(3, latitude=59.943)]
    result = propose([long, *nearby])
    assert set(result['suggested_places_sequence']) == {2, 3}
    assert_candidate(result, [long, *nearby], 60)
    assert any(1 in alternative['suggested_places_sequence'] for alternative in result['alternatives'])


def test_unusual_bonus_cannot_override_a_much_closer_useful_single_stop():
    nearby = place(1, latitude=59.941)
    distant_unusual = place(2, latitude=59.96, unusual=True)
    result = propose([distant_unusual, nearby], max_places=1)
    assert result['suggested_places_sequence'] == [1]
    assert result['alternatives'][0]['suggested_places_sequence'] == [2]


def test_equal_local_options_get_only_soft_unusual_preference():
    options = [place(1), place(2, unusual=True)]
    assert propose(options, max_places=1)['suggested_places_sequence'] == [2]
    assert propose(options, unusual=False, max_places=1)['suggested_places_sequence'] == [1]
    assert set(propose(options)['suggested_places_sequence']) == {1, 2}


@pytest.mark.parametrize('budget', [60, 120, 240])
def test_all_presets_have_feasible_deterministic_bounded_distinct_alternatives(budget):
    options = [place(index, latitude=59.941 + index * .001, visit=12,
                     category=list(C)[index % 6], unusual=index % 3 == 0) for index in range(1, 12)]
    result = propose(options, budget)
    assert result == propose(list(reversed(options)), budget)
    assert result['reason_code'] is None
    assert len(result['alternatives']) <= 2
    candidates = [result, *result['alternatives']]
    sets = [frozenset(candidate['suggested_places_sequence']) for candidate in candidates]
    assert len(sets) == len(set(sets))
    for candidate in candidates:
        assert_candidate(candidate, options, budget)


def test_alternatives_replace_an_initial_destination_when_an_option_exists():
    options = [place(1, visit=40), place(2, latitude=59.943, visit=40),
               place(3, latitude=59.944, visit=40)]
    result = propose(options)
    assert result['suggested_places_sequence'] == [1]
    assert [alternative['suggested_places_sequence'] for alternative in result['alternatives']] == [[2], [3]]


def test_one_stop_has_nonzero_geometry_and_does_not_require_extra_destinations():
    result = propose([place(1, visit=40)])
    assert result['suggested_places_sequence'] == [1]
    assert result['estimated_walking_duration_seconds'] > 0
    assert result['estimated_total_distance_meters'] >= 25
    assert result['alternatives'] == []


def test_at_start_records_cannot_hide_a_meaningful_feasible_destination():
    at_start = [place(identity, latitude=59.94) for identity in range(1, 31)]
    destination = place(99, latitude=59.945)
    result = propose([*at_start, destination])
    assert result['suggested_places_sequence'] == [99]


def test_extended_budget_can_reach_a_far_stop_without_a_hard_search_radius():
    far = place(1, latitude=60.0, visit=60)
    assert propose([far], budget=120)['reason_code'] == 'preliminary_budget_infeasible'
    result = propose([far], budget=240)
    assert result['suggested_places_sequence'] == [1]
    assert result['estimated_total_distance_meters'] > 5000


@pytest.mark.parametrize('places,categories,budget,reason', [
    ([], None, 60, 'no_eligible_places'),
    ([place(1, latitude=float('nan'))], None, 60, 'no_eligible_places'),
    ([place(1)], ['art'], 60, 'category_filter_empty'),
    ([place(1, visit=120)], None, 60, 'preliminary_budget_infeasible'),
    ([place(1, latitude=59.94)], None, 60, 'no_meaningful_route'),
])
def test_empty_reasons_have_zero_totals_without_inventing_a_network_failure(places, categories, budget, reason):
    result = propose(places, budget, categories)
    assert result['status'] == 'empty' and result['reason_code'] == reason
    assert result['suggested_places_sequence'] == result['items'] == result['alternatives'] == []
    assert all(result[field] == 0 for field in (
        'estimated_walking_duration_seconds', 'estimated_visit_duration_minutes',
        'estimated_total_duration_minutes', 'estimated_total_distance_meters'))


@pytest.mark.parametrize('extra', [-.000001, .000001])
def test_rounding_at_exact_budget_rejects_even_a_fraction_of_an_extra_second(extra):
    latitude = math.degrees((75.0 + extra) / service.EARTH_RADIUS_METERS)
    target = place(1, latitude=latitude, longitude=0, visit=59)
    result = SmartWalkService.propose_walk(0, 0, 60, [target])
    if extra < 0:
        assert result['status'] == 'success'
        assert result['estimated_walking_duration_seconds'] == 60
        assert result['estimated_total_duration_minutes'] == 60
    else:
        assert result['reason_code'] == 'preliminary_budget_infeasible'


def test_categories_remain_strict_in_primary_and_every_alternative():
    options = [place(1, category=C.ART), place(2, category=C.ART, latitude=59.943),
               place(3, category=C.NATURE, latitude=59.941, unusual=True)]
    result = propose(options, categories=['art'])
    for candidate in [result, *result['alternatives']]:
        assert all(item['place'].category == C.ART for item in candidate['items'])
        assert 3 not in candidate['suggested_places_sequence']


def test_category_variety_is_retained_when_two_interests_fit():
    options = [place(1), place(2, latitude=59.943), place(3, category=C.ART, latitude=59.944)]
    result = propose(options, budget=120, categories=['art', 'history_architecture'])
    assert {item['place'].category for item in result['items']} == {C.ART, C.HISTORY_ARCHITECTURE}


def test_duplicate_ids_keys_and_colocated_same_title_do_not_create_fake_stops():
    first = place(1, key='same-object')
    duplicate_key = place(2, key='same-object')
    same_title = place(3, title='  МЕСТО   1 ', latitude=59.94201)
    other = place(4, latitude=59.943)
    options = [first, first, duplicate_key, same_title, other]
    result = propose(options)
    assert set(result['suggested_places_sequence']) == {1, 4}
    assert result == propose(list(reversed(options)))


@pytest.mark.parametrize('field,value', [('title', 'Другое имя'), ('is_unusual', True),
                                       ('catalog_key', 'other-object')])
def test_conflicting_duplicate_identity_is_rejected_independent_of_input_order(field, value):
    original, conflicting, healthy = place(1), place(1), place(2, latitude=59.943)
    setattr(conflicting, field, value)
    options = [original, conflicting, healthy]
    assert propose(options)['suggested_places_sequence'] == [2]
    assert propose(options) == propose(list(reversed(options)))


@pytest.mark.parametrize('field,value', [('latitude', True), ('longitude', '30.31'),
                                       ('category', 'art'), ('recommended_visit_minutes', True),
                                       ('recommended_visit_minutes', 10.5), ('id', 0),
                                       ('id', '1'), ('title', 'x' * 201),
                                       ('address', 'x' * 301), ('catalog_key', [])])
def test_corrupt_database_records_cannot_break_or_contaminate_a_healthy_walk(field, value):
    hostile, healthy = place(1), place(2, latitude=59.943)
    setattr(hostile, field, value)
    result = propose([None, hostile, healthy])
    assert result['suggested_places_sequence'] == [2]


def test_blob_text_and_malformed_flag_are_honestly_serialized_without_a_bonus():
    hostile = place(1)
    hostile.title = hostile.address = hostile.short_description = b'\xff'
    hostile.is_unusual = 'true'
    result = propose([hostile])
    item = result['items'][0]
    assert item['place'].title == item['place'].address == 'Данные недоступны'
    assert item['place'].is_unusual is False
    assert not any('Необычное' in reason or '\\xff' in reason for reason in item['reasons'])


@pytest.mark.parametrize('kwargs', [dict(start_lat=True), dict(start_lon='30.31'),
                                  dict(start_lat=float('inf')), dict(start_lon=181),
                                  dict(time_budget_minutes=True), dict(time_budget_minutes=60.0),
                                  dict(categories='art'), dict(categories=['wrong']),
                                  dict(prefer_unusual='true'), dict(max_places=False)])
def test_service_rejects_malformed_request_parameters(kwargs):
    arguments = dict(start_lat=59.94, start_lon=30.31, time_budget_minutes=60, all_places=[place(1)])
    arguments.update(kwargs)
    with pytest.raises(ValueError):
        SmartWalkService.propose_walk(**arguments)


@pytest.mark.parametrize('same_title', [False, True])
def test_large_catalog_has_bounded_geometric_search_work(monkeypatch, same_title):
    options = [place(identity, latitude=59.941 + (identity % 20) * .0001,
                     longitude=30.311 + (identity // 20) * .0001,
                     category=list(C)[identity % 6], visit=5) for identity in range(1, 401)]
    if same_title:
        # Widely separated identical titles defeat a naive quadratic title scan.
        options = [place(identity, latitude=59.941 + identity * .00035,
                         title='Памятник', visit=5) for identity in range(1, 401)]
    calls = 0
    original = service.calculate_haversine_distance
    def counted(*args):
        nonlocal calls
        calls += 1
        return original(*args)
    monkeypatch.setattr(service, 'calculate_haversine_distance', counted)
    result = propose(options, budget=240)
    assert result['status'] == 'success'
    # Linear spatial review/origin scan, bounded matrix and returned stops.
    limit = (5 if same_title else 1) * len(options) + 24 * 24 + 18
    assert calls <= limit
    assert len(result['suggested_places_sequence']) <= 6


@pytest.mark.parametrize('origin,first,second', [
    ((0, 179.995), (0, 179.99999), (0, -179.99999)),
    ((89.99, 0), (89.9999, 0), (89.9999, 180)),
])
def test_semantic_duplicate_spatial_guard_handles_dateline_and_poles(origin, first, second):
    options = [place(1, latitude=first[0], longitude=first[1], title='Памятник'),
               place(2, latitude=second[0], longitude=second[1], title='ПАМЯТНИК')]
    result = SmartWalkService.propose_walk(*origin, 60, options)
    assert result['suggested_places_sequence'] == [1]
    assert result == SmartWalkService.propose_walk(*origin, 60, list(reversed(options)))


def test_additive_response_parses_old_contract_without_alternatives_or_reason():
    result = propose([place(1)])
    old = {key: value for key, value in result.items() if key not in ('alternatives', 'reason_code')}
    parsed = AutoWalkProposalResponse.model_validate(old)
    assert parsed.alternatives == [] and parsed.reason_code is None
    assert parsed.items[0].place.id == 1
    with pytest.raises(ValidationError):
        AutoWalkProposalResponse.model_validate({**result, 'alternatives': [result] * 3})


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('time_budget_minutes', True), ('time_budget_minutes', '60'),
                                       ('max_places', False), ('prefer_unusual', 'true'),
                                       ('latitude', True), ('longitude', '30.31')])
async def test_api_rejects_coerced_malformed_inputs(client, field, value):
    payload = {'start_point': {'latitude': 59.94, 'longitude': 30.31}, 'time_budget_minutes': 60}
    if field in ('latitude', 'longitude'):
        payload['start_point'][field] = value
    else:
        payload[field] = value
    response = await client.post('/api/v1/routes/propose-walk', json=payload)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_endpoint_serializes_additive_alternatives_with_strict_original_fields(client):
    response = await client.post('/api/v1/routes/propose-walk', json={
        'start_point': {'latitude': 59.94, 'longitude': 30.31}, 'time_budget_minutes': 60})
    assert response.status_code == 200
    data = response.json()
    assert data['reason_code'] is None
    assert len(data['alternatives']) <= 2
    for candidate in [data, *data['alternatives']]:
        assert candidate['time_budget_minutes'] == 60
        assert [item['place']['id'] for item in candidate['items']] == candidate['suggested_places_sequence']
        assert candidate['estimated_walking_duration_seconds'] + candidate['estimated_visit_duration_minutes'] * 60 <= 3600
