import pytest
from httpx import AsyncClient
from app.services.smart_walk_service import (
    calculate_haversine_distance,
    estimate_walking_minutes,
    two_opt_optimize_route,
    SmartWalkService,
)
from app.models.place import Place
from app.models.enums import PlaceCategory


def test_haversine_distance_known_points():
    # Дворцовая площадь (59.9390, 30.3158) -> Исаакиевский собор (59.9341, 30.3060)
    # Примерно 780-800 метров
    dist = calculate_haversine_distance(59.9390, 30.3158, 59.9341, 30.3060)
    assert 700 <= dist <= 900
    walk_min = estimate_walking_minutes(dist)
    assert 9 <= walk_min <= 13


def test_two_opt_optimization_reduces_or_maintains_distance():
    # Создаем 4 тестовых места
    p1 = Place(id=1, title="P1", category=PlaceCategory.ART, latitude=59.93, longitude=30.30, recommended_visit_minutes=20)
    p2 = Place(id=2, title="P2", category=PlaceCategory.ART, latitude=59.95, longitude=30.35, recommended_visit_minutes=20)
    p3 = Place(id=3, title="P3", category=PlaceCategory.ART, latitude=59.94, longitude=30.32, recommended_visit_minutes=20)
    p4 = Place(id=4, title="P4", category=PlaceCategory.ART, latitude=59.96, longitude=30.38, recommended_visit_minutes=20)

    # Неоптимальный зигзаг
    zigzag = [p4, p1, p3, p2]
    optimized = two_opt_optimize_route(59.92, 30.29, zigzag)
    assert len(optimized) == 4
    # Первой после старта (59.92, 30.29) должна оказаться p1 (59.93, 30.30)
    assert optimized[0].id == 1


@pytest.mark.asyncio
async def test_smart_walk_time_budget_respect(client: AsyncClient):
    # Тест: 45 минут бюджет -> должно поместиться 1-2 места
    payload = {
        "start_point": {"latitude": 59.9343, "longitude": 30.3351},
        "time_budget_minutes": 45,
        "max_places": 6,
    }
    resp = await client.post("/api/v1/routes/propose-walk", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert 1 <= len(data["suggested_places_sequence"]) <= 2
    assert data["estimated_total_duration_minutes"] <= 45


@pytest.mark.asyncio
async def test_smart_walk_category_filtering(client: AsyncClient):
    # Тест: запрашиваем категорию 'art'
    payload = {
        "start_point": {"latitude": 59.9343, "longitude": 30.3351},
        "time_budget_minutes": 180,
        "categories": ["art"],
        "max_places": 4,
    }
    resp = await client.post("/api/v1/routes/propose-walk", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    for item in data["items"]:
        assert item["place"]["category"] == "art"
        assert any("Искусство" in r for r in item["reasons"])


@pytest.mark.asyncio
async def test_smart_walk_unusual_preference(client: AsyncClient, monkeypatch):
    from app.services.place_service import PlaceService
    # Equal nearby options demonstrate preference without requiring an unusual
    # attraction to displace a more useful local route in the real catalog.
    options = [Place(id=901 + index, title=f"Место {index}", category=PlaceCategory.ART,
                     short_description="Описание", full_description="Описание", address="СПб",
                     latitude=59.935, longitude=30.3351, recommended_visit_minutes=10,
                     is_unusual=bool(index)) for index in range(2)]
    async def candidates(**kwargs):
        return options
    monkeypatch.setattr(PlaceService, 'get_places', candidates)
    # Тест: предпочитаем необычные места
    payload = {
        "start_point": {"latitude": 59.9343, "longitude": 30.3351},
        "time_budget_minutes": 180,
        "prefer_unusual": True,
        "max_places": 1,
    }
    resp = await client.post("/api/v1/routes/propose-walk", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    # Проверяем, что есть необычные места среди выбранных
    unusual_items = [item for item in data["items"] if item["place"]["is_unusual"]]
    assert len(unusual_items) >= 1
    assert data['suggested_places_sequence'] == [902]
    for u in unusual_items:
        assert any("Необычное" in r for r in u["reasons"])


@pytest.mark.asyncio
async def test_smart_walk_deterministic_output(client: AsyncClient):
    # Тест: одинаковые входные данные дают одинаковую последовательность мест
    payload = {
        "start_point": {"latitude": 59.9298, "longitude": 30.3622},  # Московский вокзал
        "time_budget_minutes": 120,
        "categories": ["history_architecture"],
        "prefer_unusual": False,
        "max_places": 4,
    }
    resp1 = await client.post("/api/v1/routes/propose-walk", json=payload)
    resp2 = await client.post("/api/v1/routes/propose-walk", json=payload)
    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert resp1.json()["suggested_places_sequence"] == resp2.json()["suggested_places_sequence"]
