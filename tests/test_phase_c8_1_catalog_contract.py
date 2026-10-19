"""C8.1 exposes stable catalog identity without changing write or legacy contracts."""
import pytest
from sqlalchemy import select

from app.db.seed_data import SPB_SEED_PLACES
from app.models.place import Place
from app.schemas.place import PlaceBase, PlaceCreate, PlaceDetail, PlaceRead


def _legacy_place():
    return {
        "id": 1,
        "title": "Место",
        "short_description": "Описание",
        "category": "nature",
        "latitude": 59.93,
        "longitude": 30.31,
        "display_latitude": 59.93,
        "display_longitude": 30.31,
        "address": "Санкт-Петербург",
    }


def test_read_identity_is_optional_and_not_a_client_write_field():
    assert "catalog_key" not in PlaceBase.model_fields
    assert "catalog_key" not in PlaceCreate.model_fields
    assert not PlaceRead.model_fields["catalog_key"].is_required()
    assert not PlaceDetail.model_fields["catalog_key"].is_required()
    assert PlaceRead.model_validate(_legacy_place()).catalog_key is None
    assert "catalog_key" not in PlaceRead.model_json_schema()["required"]


@pytest.mark.parametrize("key", ["a", "hermitage", "a" * 80])
def test_valid_catalog_key_round_trips_without_changing_legacy_fields(key):
    legacy = PlaceRead.model_validate(_legacy_place()).model_dump()
    keyed = PlaceRead.model_validate({**_legacy_place(), "catalog_key": key})
    assert keyed.catalog_key == key
    assert keyed.model_dump(exclude={"catalog_key"}) == {
        field: value for field, value in legacy.items() if field != "catalog_key"
    }


@pytest.mark.parametrize("key", [
    None, True, 1, 1.5, {}, [], b"hermitage", "", " Upper", "with_underscore",
    "UPPER", "a" * 81, "hermitage\n", "hermitage/child",
])
def test_invalid_or_missing_catalog_key_is_unavailable_instead_of_an_identity(key):
    place = PlaceRead.model_validate({**_legacy_place(), "catalog_key": key})
    assert place.catalog_key is None
    assert place.model_dump()["catalog_key"] is None


@pytest.mark.asyncio
async def test_list_and_detail_publish_the_same_unique_seed_identity(client):
    response = await client.get("/api/v1/places?limit=200")
    assert response.status_code == 200
    items = response.json()["items"]
    expected = {item["catalog_key"] for item in SPB_SEED_PLACES}
    assert {item["catalog_key"] for item in items} == expected
    assert len(items) == len(expected)
    first = items[0]
    detail = await client.get(f'/api/v1/places/{first["id"]}')
    assert detail.status_code == 200
    assert detail.json()["catalog_key"] == first["catalog_key"]


@pytest.mark.asyncio
async def test_key_survives_display_changes_and_a_database_id_change(client, db_session):
    seed = SPB_SEED_PLACES[0]
    place = await db_session.scalar(select(Place).where(Place.catalog_key == seed["catalog_key"]))
    place.id = 9001
    place.title = "Уточнённое название"
    place.address = "Уточнённый адрес"
    await db_session.commit()
    response = await client.get("/api/v1/places/9001")
    assert response.status_code == 200
    assert response.json()["catalog_key"] == seed["catalog_key"]
    assert response.json()["title"] == "Уточнённое название"


@pytest.mark.asyncio
async def test_unkeyed_and_malformed_legacy_rows_remain_readable(client, db_session):
    seed = SPB_SEED_PLACES[0]
    for key in [None, "INVALID-legacy-key"]:
        place = Place(
            catalog_key=key, title="Сторонний объект", short_description="Описание",
            full_description="Описание стороннего объекта", category=seed["category"],
            latitude=59.93, longitude=30.31, address="Санкт-Петербург",
        )
        db_session.add(place)
        await db_session.commit()
        response = await client.get(f"/api/v1/places/{place.id}")
        assert response.status_code == 200
        assert response.json()["catalog_key"] is None
    response = await client.get("/api/v1/places?limit=200")
    assert response.status_code == 200
    assert sum(item["catalog_key"] is None for item in response.json()["items"]) == 2


@pytest.mark.asyncio
async def test_public_identity_does_not_expand_private_catalog_metadata(client):
    public_fields = {
        "id", "catalog_key", "title", "short_description", "category", "is_unusual",
        "latitude", "longitude", "address", "photo_url", "rating", "recommended_visit_minutes",
        "tags", "source_url", "coordinate_kind", "address_status", "why_visit",
        "display_latitude", "display_longitude", "routing_latitude", "routing_longitude",
        "routing_point_source", "routing_point_verified_on", "routing_access_status",
    }
    assert set(PlaceRead.model_fields) == public_fields
    response = await client.get("/api/v1/places?limit=1")
    item = response.json()["items"][0]
    assert set(item) == public_fields
    detail = (await client.get(f'/api/v1/places/{item["id"]}')).json()
    assert set(detail) == public_fields | {
        "full_description", "working_hours", "architectural_style", "year_built",
        "visit_note", "official_website", "wikipedia_url",
    }
    assert "source_metadata" not in detail
    assert "legacy_titles" not in detail
    assert "legacy_coordinates" not in detail
