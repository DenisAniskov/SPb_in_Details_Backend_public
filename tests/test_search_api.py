import pytest
from unittest.mock import AsyncMock, patch
import httpx
from httpx import AsyncClient
from app.core.config import settings
from app.services.yandex_search_service import yandex_search_service


@pytest.fixture(autouse=True)
def clear_service_cache():
    yield


@pytest.mark.asyncio
async def test_suggest_validation(client: AsyncClient):
    # Empty query string
    response = await client.get("/api/v1/search/suggest?text=")
    assert response.status_code == 422

    # Missing query parameter
    response = await client.get("/api/v1/search/suggest")
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_suggest_success_normalization(client: AsyncClient):
    mock_yandex_response = {
        "suggest_reqid": "123456789",
        "results": [
            {
                "title": {"text": "Московский вокзал", "hl": [[0, 10]]},
                "subtitle": {"text": "Санкт-Петербург, Невский проспект, 85"},
                "tags": ["train_station", "transit"],
                "distance": {"value": 2500, "text": "2.5 км"},
                "uri": "ymapsbm1://org?oid=1023778014",
            },
            {
                "title": {"text": "Московские ворота"},
                "subtitle": {"text": "Санкт-Петербург, Московский проспект"},
                "tags": ["metro"],
                "uri": "ymapsbm1://geo?data=123",
            },
        ],
    }

    mock_client = AsyncMock()
    mock_client.get.return_value = httpx.Response(
        200,
        json=mock_yandex_response,
        request=httpx.Request("GET", "https://suggest-maps.yandex.ru/v1/suggest"),
    )

    with patch.object(yandex_search_service, "get_client", return_value=mock_client), \
         patch.object(settings, "SUGGEST_API_KEY", "dummy_secret_key_123"):
        response = await client.get("/api/v1/search/suggest?text=Московский")

    assert response.status_code == 200
    data = response.json()
    assert data["query"] == "Московский"
    assert len(data["results"]) == 2

    first = data["results"][0]
    assert first["title"] == "Московский вокзал"
    assert first["subtitle"] == "Санкт-Петербург, Невский проспект, 85"
    assert "train_station" in first["tags"]
    assert first["uri"] == "ymapsbm1://org?oid=1023778014"
    assert first["distance_text"] == "2.5 км"

    # Verify no secret is leaked in JSON response
    assert "dummy_secret_key_123" not in response.text
    assert "suggest_reqid" not in response.text  # Raw Yandex wrapper not leaked


@pytest.mark.asyncio
async def test_suggest_missing_api_key(client: AsyncClient):
    with patch.object(settings, "SUGGEST_API_KEY", None):
        response = await client.get("/api/v1/search/suggest?text=Эрмитаж")
        assert response.status_code == 503
        data = response.json()
        assert "временно недоступен" in data["detail"]
        assert "не настроен ключ API" in data["detail"]


@pytest.mark.asyncio
async def test_suggest_timeout(client: AsyncClient):
    mock_client = AsyncMock()
    mock_client.get.side_effect = httpx.TimeoutException("Timeout")

    with patch.object(yandex_search_service, "get_client", return_value=mock_client), \
         patch.object(settings, "SUGGEST_API_KEY", "dummy_key"):
        response = await client.get("/api/v1/search/suggest?text=Невский")
        assert response.status_code == 504
        assert "Время ожидания" in response.json()["detail"]


@pytest.mark.asyncio
async def test_suggest_upstream_auth_error(client: AsyncClient):
    mock_client = AsyncMock()
    mock_client.get.return_value = httpx.Response(
        403,
        json={"message": "Invalid API key"},
        request=httpx.Request("GET", "https://suggest-maps.yandex.ru/v1/suggest"),
    )

    with patch.object(yandex_search_service, "get_client", return_value=mock_client), \
         patch.object(settings, "SUGGEST_API_KEY", "secret_key_abc"):
        response = await client.get("/api/v1/search/suggest?text=Невский")
        assert response.status_code == 503
        assert "авторизации" in response.json()["detail"]
        assert "secret_key_abc" not in response.text


@pytest.mark.asyncio
async def test_suggest_rate_limit(client: AsyncClient):
    mock_client = AsyncMock()
    mock_client.get.return_value = httpx.Response(
        429,
        text="Rate limit exceeded",
        request=httpx.Request("GET", "https://suggest-maps.yandex.ru/v1/suggest"),
    )

    with patch.object(yandex_search_service, "get_client", return_value=mock_client), \
         patch.object(settings, "SUGGEST_API_KEY", "dummy_key"):
        response = await client.get("/api/v1/search/suggest?text=Невский")
        assert response.status_code == 429
        assert "Превышен лимит запросов" in response.json()["detail"]


@pytest.mark.asyncio
async def test_geocode_validation_missing_params(client: AsyncClient):
    response = await client.get("/api/v1/search/geocode")
    assert response.status_code == 400
    assert "Необходимо указать" in response.json()["detail"]


@pytest.mark.asyncio
async def test_geocode_success_normalization(client: AsyncClient):
    mock_geocoder_response = {
        "response": {
            "GeoObjectCollection": {
                "featureMember": [
                    {
                        "GeoObject": {
                            "name": "Московский вокзал",
                            "description": "Санкт-Петербург, Невский проспект, 85",
                            "metaDataProperty": {
                                "GeocoderMetaData": {
                                    "kind": "station",
                                    "text": "Россия, Санкт-Петербург, Невский проспект, 85",
                                }
                            },
                            "Point": {
                                "pos": "30.362145 59.929828"  # "lon lat"
                            },
                        }
                    }
                ]
            }
        }
    }

    mock_client = AsyncMock()
    mock_client.get.return_value = httpx.Response(
        200,
        json=mock_geocoder_response,
        request=httpx.Request("GET", "https://geocode-maps.yandex.ru/1.x/"),
    )

    with patch.object(yandex_search_service, "get_client", return_value=mock_client), \
         patch.object(settings, "GEOCODER_API_KEY", "geocoder_secret_key"):
        response = await client.get("/api/v1/search/geocode?uri=ymapsbm1://org?oid=1023778014")

    assert response.status_code == 200
    data = response.json()
    assert data["title"] == "Московский вокзал"
    assert data["subtitle"] == "Санкт-Петербург, Невский проспект, 85"
    assert data["address"] == "Россия, Санкт-Петербург, Невский проспект, 85"
    assert data["longitude"] == pytest.approx(30.362145)
    assert data["latitude"] == pytest.approx(59.929828)
    assert data["kind"] == "station"
    assert "geocoder_secret_key" not in response.text


@pytest.mark.asyncio
async def test_geocode_not_found(client: AsyncClient):
    mock_empty_response = {
        "response": {
            "GeoObjectCollection": {
                "featureMember": []
            }
        }
    }

    mock_client = AsyncMock()
    mock_client.get.return_value = httpx.Response(
        200,
        json=mock_empty_response,
        request=httpx.Request("GET", "https://geocode-maps.yandex.ru/1.x/"),
    )

    with patch.object(yandex_search_service, "get_client", return_value=mock_client), \
         patch.object(settings, "GEOCODER_API_KEY", "dummy_key"):
        response = await client.get("/api/v1/search/geocode?text=НесуществующееМесто123XYZ")
        assert response.status_code == 404
        assert "не найдены" in response.json()["detail"]


@pytest.mark.asyncio
async def test_reverse_geocode_success(client: AsyncClient):
    mock_reverse_response = {
        "response": {
            "GeoObjectCollection": {
                "featureMember": [
                    {
                        "GeoObject": {
                            "name": "Дворцовая набережная, 10",
                            "description": "Санкт-Петербург, Россия",
                            "metaDataProperty": {
                                "GeocoderMetaData": {
                                    "kind": "house",
                                    "text": "Россия, Санкт-Петербург, Дворцовая набережная, 10",
                                }
                            },
                            "Point": {
                                "pos": "30.315868 59.939095"
                            },
                        }
                    }
                ]
            }
        }
    }

    mock_client = AsyncMock()
    mock_client.get.return_value = httpx.Response(
        200,
        json=mock_reverse_response,
        request=httpx.Request("GET", "https://geocode-maps.yandex.ru/1.x/"),
    )

    with patch.object(yandex_search_service, "get_client", return_value=mock_client), \
         patch.object(settings, "GEOCODER_API_KEY", "dummy_key"):
        response = await client.get("/api/v1/search/reverse-geocode?lat=59.939095&lon=30.315868")

    assert response.status_code == 200
    data = response.json()
    assert data["title"] == "Дворцовая набережная, 10"
    assert data["address"] == "Россия, Санкт-Петербург, Дворцовая набережная, 10"
    assert data["latitude"] == pytest.approx(59.939095)
    assert data["longitude"] == pytest.approx(30.315868)


@pytest.mark.asyncio
async def test_reverse_geocode_validation_coords(client: AsyncClient):
    # Latitude out of bounds
    response = await client.get("/api/v1/search/reverse-geocode?lat=120.0&lon=30.0")
    assert response.status_code == 422

    # Longitude out of bounds
    response = await client.get("/api/v1/search/reverse-geocode?lat=59.0&lon=200.0")
    assert response.status_code == 422
