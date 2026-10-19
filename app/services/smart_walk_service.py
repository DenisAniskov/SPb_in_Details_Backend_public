"""Deterministic, bounded geographic proposals; native MapKit validates routes."""
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from pydantic import ValidationError

from app.models.enums import PlaceCategory
from app.models.place import Place
from app.schemas.place import PlaceRead


# These are geometric estimates, not measured pedestrian travel times.
WALKING_SPEED_METERS_PER_MINUTE = 75.0
EARTH_RADIUS_METERS = 6371000.0
MAX_SEARCH_CANDIDATES = 24
BEAM_WIDTH = 32
MAX_STOPS = 6
MAX_ALTERNATIVES = 2
# A route entirely within the start's immediate coordinate uncertainty is not a walk.
MIN_MEANINGFUL_ROUTE_METERS = 25.0
UNUSUAL_BONUS = 4.0


def calculate_haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (math.sin(delta_phi / 2.0) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2)
    a = min(1.0, max(0.0, a))
    return EARTH_RADIUS_METERS * 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def estimate_walking_minutes(distance_meters: float) -> float:
    return distance_meters / WALKING_SPEED_METERS_PER_MINUTE


def is_unusual_flag(place: Place) -> bool:
    value = place.is_unusual
    return value is True or (type(value) is int and value == 1)


def _visit_minutes(place: Place) -> int:
    return 30 if place.recommended_visit_minutes is None else place.recommended_visit_minutes


def _candidate_score(place: Place, distance: float, categories: List[str],
                     prefer_unusual: bool, visited_categories) -> float:
    interest = 50.0 if categories else 30.0
    # Duration gives modest value to a substantive visit, but never rewards a
    # long museum sufficiently to crowd out several useful local stops.
    sightseeing = 0.2 * min(_visit_minutes(place), 45)
    unusual = UNUSUAL_BONUS if prefer_unusual and is_unusual_flag(place) else 0.0
    proximity = 40.0 * max(0.0, 1.0 - distance / 3500.0)
    diversity = (12.0 if (not categories or len(categories) > 1)
                 and place.category.value not in visited_categories else 0.0)
    return interest + sightseeing + unusual + proximity + diversity


def score_candidate_place(candidate: Place, current_lat: float, current_lon: float,
                          target_categories: Optional[List[str]], prefer_unusual: bool,
                          visited_categories: List[str]) -> Tuple[float, List[str], float]:
    """Explain a stop in its final order; the route-level travel penalty is separate."""
    categories = target_categories or []
    category = candidate.category.value
    display = PlaceCategory.get_display_name(candidate.category)
    distance = calculate_haversine_distance(current_lat, current_lon,
                                             candidate.latitude, candidate.longitude)
    title = candidate.title if isinstance(candidate.title, str) else "Название недоступно"
    reasons = [f"Интересный объект: {title}"]
    if categories and category in categories:
        reasons.append(f"Соответствует интересу: {display}")
    if prefer_unusual and is_unusual_flag(candidate):
        reasons.append("Необычное атмосферное место СПб")
    if distance <= 1200.0:
        reasons.append(f"Рядом по предварительной геометрической оценке (~{round(distance)} м)")
    if (not categories or len(categories) > 1) and category not in visited_categories:
        reasons.append(f"Разнообразие: новая категория ({display})")
    return (_candidate_score(candidate, distance, categories, prefer_unusual, visited_categories),
            reasons, distance)


def two_opt_optimize_route(start_lat: float, start_lon: float, places: List[Place]) -> List[Place]:
    """Retained compatibility helper for deterministic open-path geometric 2-opt."""
    def route_distance(order):
        total, latitude, longitude = 0.0, start_lat, start_lon
        for place in order:
            total += calculate_haversine_distance(latitude, longitude, place.latitude, place.longitude)
            latitude, longitude = place.latitude, place.longitude
        return total

    best = list(places)
    best_distance = route_distance(best)
    for _ in range(50):
        improved = False
        for i in range(len(best) - 1):
            for k in range(i + 1, len(best)):
                order = best[:i] + best[i:k + 1][::-1] + best[k + 1:]
                distance = route_distance(order)
                if distance < best_distance - 0.01:
                    best, best_distance, improved = order, distance, True
                    break
            if improved:
                break
        if not improved:
            break
    return best


@dataclass(frozen=True)
class _RouteState:
    sequence: Tuple[int, ...]  # Indices into the bounded candidate pool.
    distance: float
    visits: int
    candidate_score: float
    first_distance: float
    categories: frozenset


def _route_score(state: _RouteState, budget: int) -> float:
    walking = estimate_walking_minutes(state.distance)
    first_walking = estimate_walking_minutes(state.first_distance)
    # Fractions of the unchanged requested budget make locality stronger for
    # quick walks and allow coherent wider walks for the longer presets.
    return state.candidate_score - 120.0 * walking / budget - 80.0 * first_walking / budget


def _fits_budget(distance: float, visits: int, budget: int) -> bool:
    # Round walking up to seconds, then include exact integer editorial minutes.
    return math.ceil(estimate_walking_minutes(distance) * 60.0) + visits * 60 <= budget * 60


def _position_bucket(place: Place) -> Tuple[int, int, int]:
    """25 m Earth-centered buckets avoid quadratic same-title duplicate scans.

    Any points within 25 m have chord differences <=25 m on every axis, so
    checking the 27 adjacent buckets is complete even near poles/dateline.
    """
    latitude, longitude = math.radians(place.latitude), math.radians(place.longitude)
    radius = EARTH_RADIUS_METERS * math.cos(latitude)
    return tuple(math.floor(value / 25.0) for value in (
        radius * math.cos(longitude), radius * math.sin(longitude),
        EARTH_RADIUS_METERS * math.sin(latitude)))


class SmartWalkService:
    @staticmethod
    def _has_finite_coordinates(place: Place) -> bool:
        latitude, longitude = getattr(place, "latitude", None), getattr(place, "longitude", None)
        return (type(latitude) in (int, float) and type(longitude) in (int, float)
                and math.isfinite(latitude) and math.isfinite(longitude)
                and -90 <= latitude <= 90 and -180 <= longitude <= 180)

    @staticmethod
    def propose_walk(start_lat: float, start_lon: float, time_budget_minutes: int,
                     all_places: List[Place], categories: Optional[List[str]] = None,
                     prefer_unusual: bool = False, max_places: int = 6) -> Dict[str, Any]:
        """At most 24 candidates × 32 beam states × six depths; no network calls."""
        if (type(start_lat) not in (int, float) or type(start_lon) not in (int, float)
                or not math.isfinite(start_lat) or not math.isfinite(start_lon)
                or not -90 <= start_lat <= 90 or not -180 <= start_lon <= 180):
            raise ValueError("Start must be finite numeric WGS84 coordinates")
        if type(time_budget_minutes) is not int or not 30 <= time_budget_minutes <= 360:
            raise ValueError("Time budget must be an integer in 30..360")
        if type(max_places) is not int or max_places < 1:
            raise ValueError("Maximum places must be a positive integer")
        if type(prefer_unusual) is not bool:
            raise ValueError("Unusual preference must be boolean")
        if not isinstance(all_places, (list, tuple)):
            raise ValueError("Places must be a collection")
        if categories is not None and not isinstance(categories, (list, tuple)):
            raise ValueError("Categories must be a list")
        if any(not isinstance(category, (str, PlaceCategory))
               or category not in set(PlaceCategory) for category in (categories or [])):
            raise ValueError("Unknown category")
        categories = sorted({category.value if isinstance(category, PlaceCategory) else category
                             for category in (categories or [])})
        max_places = min(max_places, MAX_STOPS)

        valid = [place for place in all_places
                 if SmartWalkService._has_finite_coordinates(place)
                 and type(getattr(place, "id", None)) is int and place.id > 0
                 and isinstance(getattr(place, "category", None), PlaceCategory)
                 and (place.recommended_visit_minutes is None
                      or type(place.recommended_visit_minutes) is int
                      and 5 <= place.recommended_visit_minutes <= 180)]
        # Validate response serialization before a corrupt row can win selection.
        # Existing PlaceRead safely labels missing/BLOB text and malformed flags;
        # other schema violations make the record ineligible.
        readable, serialized, identities = [], {}, {}
        for place in valid:
            try:
                model = PlaceRead.model_validate(place)
            except (ValidationError, TypeError, ValueError):
                continue
            key = place.catalog_key if isinstance(place.catalog_key, str) else None
            fingerprint = (model.model_dump_json(), key)
            identities.setdefault(place.id, set()).add(fingerprint)
            serialized[place.id] = model
            readable.append(place)
        # Conflicting duplicate IDs are ambiguous; reject the whole identity,
        # rather than choosing whichever row happened to arrive first.
        valid = [place for place in readable if len(identities[place.id]) == 1]
        # Repeated IDs/keys and same-title co-located objects represent one stop.
        # Stable sorting keeps rejection independent of input/DB ordering.
        valid.sort(key=lambda place: (place.id, place.latitude, place.longitude,
                                      place.category.value, _visit_minutes(place)))
        unique, ids, keys, titles = [], set(), set(), {}
        for place in valid:
            key = place.catalog_key if isinstance(place.catalog_key, str) and place.catalog_key else None
            title = " ".join(place.title.casefold().split()) if isinstance(place.title, str) else ""
            if place.id in ids or key is not None and key in keys:
                continue
            bucket = _position_bucket(place) if title else None
            if title:
                nearby = (other
                          for x in range(bucket[0] - 1, bucket[0] + 2)
                          for y in range(bucket[1] - 1, bucket[1] + 2)
                          for z in range(bucket[2] - 1, bucket[2] + 2)
                          for other in titles.get(title, {}).get((x, y, z), []))
                if any(calculate_haversine_distance(place.latitude, place.longitude,
                                                    other.latitude, other.longitude) <= 25.0
                       for other in nearby):
                    continue
            unique.append(place)
            ids.add(place.id)
            if key is not None:
                keys.add(key)
            if title:
                titles.setdefault(title, {}).setdefault(bucket, []).append(place)

        eligible = [place for place in unique if not categories or place.category.value in categories]

        def candidate_payload(sequence):
            selected = [pool[index] for index in sequence]
            distance, visits, latitude, longitude = 0.0, 0, start_lat, start_lon
            items, visited = [], []
            for place in selected:
                _, reasons, leg = score_candidate_place(place, latitude, longitude, categories,
                                                         prefer_unusual, visited)
                distance += leg
                visits += _visit_minutes(place)
                items.append({"place": serialized[place.id],
                              "visit_duration_minutes": _visit_minutes(place), "reasons": reasons})
                visited.append(place.category.value)
                latitude, longitude = place.latitude, place.longitude
            seconds = math.ceil(estimate_walking_minutes(distance) * 60.0)
            return {"time_budget_minutes": time_budget_minutes,
                    "estimated_walking_duration_seconds": seconds,
                    "estimated_visit_duration_minutes": visits,
                    "estimated_total_duration_minutes": math.ceil(seconds / 60.0 + visits),
                    "estimated_total_distance_meters": round(distance, 1),
                    "suggested_places_sequence": [place.id for place in selected], "items": items}

        def empty(reason):
            messages = {
                "no_eligible_places": "В каталоге нет пригодных мест для подбора. Попробуйте позднее или выберите другой старт.",
                "category_filter_empty": "В каталоге нет мест выбранных категорий. Измените интересы или старт.",
                "preliminary_budget_infeasible": "Подходящие места не помещаются в предварительный бюджет. Выберите более долгую прогулку или другой старт.",
                "no_meaningful_route": "Подходящие места находятся у самой стартовой точки. Выберите другой старт или более долгую прогулку.",
            }
            return {"status": "empty", "reason_code": reason, "message": messages[reason],
                    **candidate_payload(()), "alternatives": []}

        pool = []
        if not unique:
            return empty("no_eligible_places")
        if not eligible:
            return empty("category_filter_empty")
        first_distances = {place.id: calculate_haversine_distance(start_lat, start_lon,
                                                                 place.latitude, place.longitude)
                           for place in eligible}
        feasible = [place for place in eligible
                    if _fits_budget(first_distances[place.id], _visit_minutes(place), time_budget_minutes)]
        if not feasible:
            return empty("preliminary_budget_infeasible")
        # Stops effectively at the current position cannot crowd a useful
        # destination out of the bounded pool or fabricate a zero-length walk.
        feasible = [place for place in feasible
                    if first_distances[place.id] >= MIN_MEANINGFUL_ROUTE_METERS]
        if not feasible:
            return empty("no_meaningful_route")

        def initial_rank(place):
            distance = first_distances[place.id]
            state = _RouteState((), distance, _visit_minutes(place),
                                _candidate_score(place, distance, categories, prefer_unusual, []),
                                distance, frozenset([place.category.value]))
            return (-_route_score(state, time_budget_minutes), distance, place.id)

        nearest = sorted(feasible, key=lambda place: (first_distances[place.id], place.id))
        # Reserve a feasible representative of each interest; reserve 16 nearby
        # candidates; fill remaining slots by complete singleton utility.
        reserved = []
        if not categories or len(categories) > 1:
            for category in sorted({place.category.value for place in feasible}):
                reserved.append(min((place for place in feasible if place.category.value == category),
                                    key=initial_rank))
        for place in reserved + nearest[:16] + sorted(feasible, key=initial_rank):
            if place.id not in {candidate.id for candidate in pool}:
                pool.append(place)
            if len(pool) == MAX_SEARCH_CANDIDATES:
                break
        # Cache a small complete geometric matrix. Search never calls a router.
        origin = [first_distances[place.id] for place in pool]
        distances = [[calculate_haversine_distance(a.latitude, a.longitude, b.latitude, b.longitude)
                      for b in pool] for a in pool]
        beam = [_RouteState((), 0.0, 0, 0.0, 0.0, frozenset())]
        completed = {}

        def rank(state):
            return (-_route_score(state, time_budget_minutes), state.distance, state.visits,
                    tuple(pool[index].id for index in state.sequence))

        for _ in range(max_places):
            expanded = {}
            for state in beam:
                for index, place in enumerate(pool):
                    if index in state.sequence:
                        continue
                    leg = distances[state.sequence[-1]][index] if state.sequence else origin[index]
                    distance, visits = state.distance + leg, state.visits + _visit_minutes(place)
                    if not _fits_budget(distance, visits, time_budget_minutes):
                        continue
                    next_state = _RouteState(state.sequence + (index,), distance, visits,
                                             state.candidate_score + _candidate_score(
                                                 place, leg, categories, prefer_unusual, state.categories),
                                             state.first_distance if state.sequence else leg,
                                             state.categories | {place.category.value})
                    identity = (frozenset(next_state.sequence), index)
                    previous = expanded.get(identity)
                    if previous is None or rank(next_state) < rank(previous):
                        expanded[identity] = next_state
            if not expanded:
                break
            # Keep every singleton for native fallback, later at most 32 states
            # per depth. Completed partial routes remain first-class outcomes.
            beam = sorted(expanded.values(), key=rank)[:BEAM_WIDTH]
            for state in beam:
                if state.distance >= MIN_MEANINGFUL_ROUTE_METERS:
                    identity = frozenset(state.sequence)
                    previous = completed.get(identity)
                    if previous is None or rank(state) < rank(previous):
                        completed[identity] = state
        if not completed:
            return empty("no_meaningful_route")
        ranked = sorted(completed.values(), key=rank)
        primary = ranked[0]
        chosen = [primary]
        # Give native fallback a different initial destination and at least one
        # replacement POI where available, rather than only reordered copies.
        for _ in range(MAX_ALTERNATIVES):
            remaining = [state for state in ranked
                         if frozenset(state.sequence) not in {frozenset(item.sequence) for item in chosen}]
            if not remaining:
                break
            replacements = [state for state in remaining
                            if set(state.sequence) - set(primary.sequence)
                            and state.sequence[0] != primary.sequence[0]]
            selected = (replacements or remaining)[0]
            chosen.append(selected)
        result = candidate_payload(primary.sequence)
        return {"status": "success", "reason_code": None,
                "message": f"Предварительно подобрано {len(primary.sequence)} мест на ~{result['estimated_total_duration_minutes']} мин.",
                **result, "alternatives": [candidate_payload(state.sequence) for state in chosen[1:]]}
