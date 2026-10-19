import pytest
from httpx import AsyncClient
from app.models.enums import PlaceCategory


@pytest.mark.asyncio
async def test_health_check(client: AsyncClient):
    response = await client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "SmartNavigator SPb" in data["service"]


@pytest.mark.asyncio
async def test_get_categories(client: AsyncClient):
    response = await client.get("/api/v1/places/categories")
    assert response.status_code == 200
    categories = response.json()
    assert len(categories) == 6
    category_ids = [c["id"] for c in categories]
    assert "history_architecture" in category_ids
    assert "art" in category_ids
    assert "science_technology" in category_ids
    assert "nature" in category_ids
    assert "modern_spaces" in category_ids
    assert "family_leisure" in category_ids
    for c in categories:
        assert c["count"] > 0
        assert len(c["name"]) > 0


@pytest.mark.asyncio
async def test_list_all_places(client: AsyncClient):
    response = await client.get("/api/v1/places?limit=200")
    assert response.status_code == 200
    data = response.json()
    assert "total" in data
    assert "items" in data
    assert data["total"] > 0
    assert len(data["items"]) == data["total"]
    
    first_place = data["items"][0]
    assert "id" in first_place
    assert "title" in first_place
    assert "latitude" in first_place
    assert "longitude" in first_place
    assert "category" in first_place
    assert "is_unusual" in first_place


@pytest.mark.asyncio
async def test_default_page_size_and_offset_preserve_expanded_catalog(client: AsyncClient):
    first_response = await client.get('/api/v1/places')
    remainder_response = await client.get('/api/v1/places?limit=200&offset=100')
    full_response = await client.get('/api/v1/places?limit=200')
    assert first_response.status_code == remainder_response.status_code == full_response.status_code == 200
    first, remainder, full = first_response.json(), remainder_response.json(), full_response.json()
    assert len(first['items']) == min(100, first['total'])
    assert first['total'] == remainder['total'] == full['total']
    assert first['items'] + remainder['items'] == full['items']
    assert len({item['id'] for item in full['items']}) == full['total']


@pytest.mark.asyncio
async def test_filter_places_by_category(client: AsyncClient):
    # Filter by art
    response = await client.get("/api/v1/places?category=art")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] > 0
    for item in data["items"]:
        assert item["category"] == PlaceCategory.ART.value

    # Filter by multiple categories
    response = await client.get("/api/v1/places?category=nature&category=modern_spaces")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] > 0
    for item in data["items"]:
        assert item["category"] in [PlaceCategory.NATURE.value, PlaceCategory.MODERN_SPACES.value]


@pytest.mark.asyncio
async def test_filter_places_by_unusual(client: AsyncClient):
    response = await client.get("/api/v1/places?is_unusual=true")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] > 0
    for item in data["items"]:
        assert item["is_unusual"] is True


@pytest.mark.asyncio
async def test_search_places(client: AsyncClient):
    response = await client.get("/api/v1/places?q=Эрмитаж")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] >= 1
    assert any("Эрмитаж" in item["title"] for item in data["items"])


@pytest.mark.asyncio
async def test_get_place_detail_success(client: AsyncClient):
    list_res = await client.get("/api/v1/places?limit=1")
    place_id = list_res.json()["items"][0]["id"]
    
    response = await client.get(f"/api/v1/places/{place_id}")
    assert response.status_code == 200
    place = response.json()
    assert place["id"] == place_id
    assert "full_description" in place
    assert len(place["full_description"]) > 10


@pytest.mark.asyncio
async def test_get_place_detail_not_found(client: AsyncClient):
    response = await client.get("/api/v1/places/99999")
    assert response.status_code == 404
    assert "не найдено" in response.json()["detail"]


@pytest.mark.asyncio
async def test_route_evaluation_endpoint(client: AsyncClient):
    list_res = await client.get("/api/v1/places?limit=1")
    place_id = list_res.json()["items"][0]["id"]

    payload = {
        "start_point": {
            "latitude": 59.939095,
            "longitude": 30.315868
        },
        "destination_place_id": place_id,
        "via_place_ids": []
    }
    response = await client.post("/api/v1/routes/evaluate", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert "shade_assessment" in data
    assert "quietness_assessment" in data
    assert "pollen_assessment" in data
    assert data["shade_assessment"]["status"] == "requires_3d_spatial_data"


@pytest.mark.asyncio
async def test_propose_walk_endpoint_success(client: AsyncClient):
    payload = {
        "start_point": {"latitude": 59.9343, "longitude": 30.3351},
        "time_budget_minutes": 90,
        "max_places": 4,
    }
    response = await client.post("/api/v1/routes/propose-walk", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert len(data["suggested_places_sequence"]) > 0
    assert len(data["items"]) == len(data["suggested_places_sequence"])
    assert data["estimated_total_duration_minutes"] <= 120
    assert data["estimated_total_distance_meters"] > 0
