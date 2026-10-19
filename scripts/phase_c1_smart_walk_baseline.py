import math
from typing import List, Optional, Tuple, Dict, Any
from app.models.place import Place
from app.models.enums import PlaceCategory
from app.schemas.place import PlaceRead


# Geometric heuristic only: 4.5 km/h = 75 meters/min, not MapKit time.
WALKING_SPEED_METERS_PER_MINUTE = 75.0
EARTH_RADIUS_METERS = 6371000.0


def calculate_haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Вычисляет расстояние между двумя координатами по формуле Haversine в метрах.
    """
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (math.sin(delta_phi / 2.0) ** 2 +
         math.cos(phi1) * math.cos(phi2) * (math.sin(delta_lambda / 2.0) ** 2))
    a = min(1.0, max(0.0, a))  # Floating-point protection near antipodes.
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))

    return EARTH_RADIUS_METERS * c


def estimate_walking_minutes(distance_meters: float) -> float:
    """Оценка времени пешего перехода в минутах (при 4.5 км/ч)."""
    return distance_meters / WALKING_SPEED_METERS_PER_MINUTE


def is_unusual_flag(place: Place) -> bool:
    """Honest unusual flag: SQLite stores whatever was written, so only a real
    boolean (or 0/1) may earn the unusual bonus; malformed TEXT/BLOB values
    are "false" instead of SQLAlchemy truthiness."""
    value = place.is_unusual
    return value is True or (type(value) is int and value == 1)


def score_candidate_place(
    candidate: Place,
    current_lat: float,
    current_lon: float,
    target_categories: Optional[List[str]],
    prefer_unusual: bool,
    visited_categories: List[str],
) -> Tuple[float, List[str], float]:
    """
    Детерминированная функция скоринга туристической ценности кандидата.
    Возвращает (total_score, explanations, distance_meters).
    """
    score = 0.0
    explanations: List[str] = []

    # 1. Соответствие интересам (категориям)
    cand_cat = candidate.category.value if hasattr(candidate.category, "value") else str(candidate.category)
    cat_display = PlaceCategory.get_display_name(candidate.category) if hasattr(candidate, "category") else cand_cat

    if target_categories:
        if cand_cat in target_categories:
            score += 50.0
            explanations.append(f"Соответствует интересу: {cat_display}")
    else:
        # Если интересы не заданы, все категории получают базовый интерес
        score += 30.0

    # 2. Необычные места
    if is_unusual_flag(candidate):
        if prefer_unusual:
            score += 25.0
            explanations.append("Необычное атмосферное место СПб")

    # 3. Близость (Proximity) от текущей точки
    dist = calculate_haversine_distance(current_lat, current_lon, candidate.latitude, candidate.longitude)
    # Нормализация: до 3.5 км дает бонус до 40 баллов
    proximity_score = max(0.0, 1.0 - (dist / 3500.0)) * 40.0
    score += proximity_score
    if dist <= 1200.0:
        explanations.append(f"Рядом по предварительной геометрической оценке (~{round(dist)} м)")

    # 4. Баланс разнообразия (Diversity bonus)
    if target_categories and len(target_categories) > 1:
        if cand_cat not in visited_categories:
            score += 20.0
            explanations.append(f"Разнообразие: новая категория ({cat_display})")
        else:
            score -= 15.0

    # Если объяснений не накопилось (например, далековато или общая категория)
    if not explanations:
        display_title = candidate.title if isinstance(candidate.title, str) else "Название недоступно"
        explanations.append(f"Интересный объект: {display_title}")

    return score, explanations, dist


def two_opt_optimize_route(
    start_lat: float,
    start_lon: float,
    places: List[Place],
) -> List[Place]:
    """
    Локальная 2-opt оптимизация последовательности точек для минимизации общего пешего пути.
    Стартовая точка фиксирована. Переставляются промежуточные точки (1..N).
    """
    n = len(places)
    if n <= 1:
        return list(places)

    best_order = list(places)

    def route_distance(order: List[Place]) -> float:
        total = calculate_haversine_distance(start_lat, start_lon, order[0].latitude, order[0].longitude)
        for i in range(len(order) - 1):
            total += calculate_haversine_distance(
                order[i].latitude, order[i].longitude,
                order[i + 1].latitude, order[i + 1].longitude,
            )
        return total

    best_dist = route_distance(best_order)
    improved = True
    iterations = 0

    while improved and iterations < 50:
        improved = False
        iterations += 1
        for i in range(n - 1):
            for k in range(i + 1, n):
                # 2-opt swap (reverse slice i..k)
                new_order = best_order[:i] + best_order[i:k + 1][::-1] + best_order[k + 1:]
                new_dist = route_distance(new_order)
                if new_dist < best_dist - 0.01:
                    best_order = new_order
                    best_dist = new_dist
                    improved = True
                    break
            if improved:
                break

    return best_order


class SmartWalkService:
    """
    Сервис автоматического формирования туристической прогулки по СПб.
    Детерминированный эвристический алгоритм с учётом времени, географии и интересов.
    """

    @staticmethod
    def _has_finite_coordinates(place: Place) -> bool:
        """
        Координаты приходят из БД без гарантии типа (SQLite хранит то, что вставили).
        Строковые или нечисловые значения не должны ронять арифметику маршрута:
        такая запись просто не может быть предложена для прогулки.
        """
        lat, lon = place.latitude, place.longitude
        return (type(lat) in (int, float) and type(lon) in (int, float)
                and math.isfinite(lat) and math.isfinite(lon)
                and -90 <= lat <= 90 and -180 <= lon <= 180)

    @staticmethod
    def propose_walk(
        start_lat: float,
        start_lon: float,
        time_budget_minutes: int,
        all_places: List[Place],
        categories: Optional[List[str]] = None,
        prefer_unusual: bool = False,
        max_places: int = 6,
    ) -> Dict[str, Any]:
        """
        Основной метод генерации умной прогулки.
        """
        max_places = min(max_places, 6)
        if max_places < 1:
            max_places = 1

        # Записи с нечисловыми координатами не участвуют в подборе (см. _has_finite_coordinates).
        all_places = [p for p in all_places if SmartWalkService._has_finite_coordinates(p)]
        all_places = [p for p in all_places if isinstance(p.category, PlaceCategory)
                      and (p.recommended_visit_minutes is None
                           or type(p.recommended_visit_minutes) is int
                           and 5 <= p.recommended_visit_minutes <= 180)]
        # Фильтрация кандидатов по категориям если заданы
        # Примечание: если пользователь выбрал определенные категории, отдаем приоритет им.
        # Если кандидатов в выбранных категориях мало, не подмешиваем чужие, сохраняя фокус интереса.
        categories = sorted(set(categories or []))
        if categories:
            eligible_pool = [p for p in all_places if p.category.value in categories or str(p.category) in categories]
        else:
            eligible_pool = list(all_places)

        selected_places: List[Place] = []
        curr_lat, curr_lon = start_lat, start_lon
        used_time_minutes = 0.0
        remaining_pool = list(eligible_pool)

        # Жадный выбор с проверкой бюджета времени
        while len(selected_places) < max_places and remaining_pool:
            candidate_scores: List[Tuple[float, float, int, Place, List[str], float, float]] = []
            visited_cats = [p.category.value if hasattr(p.category, "value") else str(p.category) for p in selected_places]

            for cand in remaining_pool:
                score, expl, dist = score_candidate_place(
                    candidate=cand,
                    current_lat=curr_lat,
                    current_lon=curr_lon,
                    target_categories=categories,
                    prefer_unusual=prefer_unusual,
                    visited_categories=visited_cats,
                )
                leg_walk_minutes = estimate_walking_minutes(dist)
                visit_minutes = cand.recommended_visit_minutes or 30
                time_cost = leg_walk_minutes + visit_minutes

                # Проверка: помещается ли кандидат в оставшийся бюджет
                if math.ceil((used_time_minutes + time_cost) * 60) <= time_budget_minutes * 60:
                    # Кортеж для сортировки: (-score, distance, id) для детерминированного tie-break
                    candidate_scores.append((-score, dist, cand.id, cand, expl, leg_walk_minutes, visit_minutes))

            if not candidate_scores:
                # Ни один оставшийся кандидат не поместился по времени
                break

            # Cover feasible selected interests before repeating a category.
            # No out-of-category or over-budget fallback is allowed.
            if len(categories) > 1:
                novel = [entry for entry in candidate_scores
                         if entry[3].category.value not in visited_cats]
                if novel:
                    candidate_scores = novel

            # Сортируем: наибольший скор, наименьшая дистанция, детерминированный id
            candidate_scores.sort(key=lambda x: (x[0], x[1], x[2]))
            _, _, _, best_place, best_expl, leg_walk_min, best_visit_min = candidate_scores[0]

            selected_places.append(best_place)
            used_time_minutes += (leg_walk_min + best_visit_min)
            curr_lat, curr_lon = best_place.latitude, best_place.longitude
            remaining_pool = [p for p in remaining_pool if p.id != best_place.id]

        # 2-opt оптимизация порядка точек
        if len(selected_places) >= 2:
            selected_places = two_opt_optimize_route(start_lat, start_lon, selected_places)

        # Финальный подсчет дистанции и времени
        total_dist_meters = 0.0
        total_walk_minutes = 0.0
        total_visit_minutes = 0.0

        if selected_places:
            prev_lat, prev_lon = start_lat, start_lon
            for p in selected_places:
                leg_d = calculate_haversine_distance(prev_lat, prev_lon, p.latitude, p.longitude)
                total_dist_meters += leg_d
                total_walk_minutes += estimate_walking_minutes(leg_d)
                total_visit_minutes += (p.recommended_visit_minutes or 30)
                prev_lat, prev_lon = p.latitude, p.longitude

        total_walk_seconds = math.ceil(total_walk_minutes * 60.0)
        total_est_minutes = math.ceil(total_walk_seconds / 60.0 + total_visit_minutes)

        # Сборка детального списка для ответа
        items_detail = []
        prev_lat, prev_lon = start_lat, start_lon
        visited_categories = []
        for p in selected_places:
            # Explanations describe the final 2-opt order, not greedy order.
            _, reasons, _ = score_candidate_place(
                p, prev_lat, prev_lon, categories, prefer_unusual, visited_categories
            )
            items_detail.append({
                "place": PlaceRead.model_validate(p),
                "visit_duration_minutes": p.recommended_visit_minutes or 30,
                "reasons": reasons,
            })
            prev_lat, prev_lon = p.latitude, p.longitude
            visited_categories.append(p.category.value)

        return {
            "status": "success" if selected_places else "empty",
            "message": (f"Предварительно подобрано {len(selected_places)} мест на ~{total_est_minutes} мин."
                        if selected_places else "В этот бюджет не помещается ни одно подходящее место. Измените время или старт."),
            "time_budget_minutes": time_budget_minutes,
            "estimated_walking_duration_seconds": total_walk_seconds,
            "estimated_visit_duration_minutes": int(round(total_visit_minutes)),
            "estimated_total_duration_minutes": total_est_minutes,
            "estimated_total_distance_meters": round(total_dist_meters, 1),
            "suggested_places_sequence": [p.id for p in selected_places],
            "items": items_detail,
        }
