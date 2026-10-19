"""Phase C6.1: seven reviewed additions with their own acquisition provenance.

The extension appends records after the frozen 173-record C6 catalog. Every
addition keeps the project's honesty rules: representative point coordinates
from an OSM source snapshot, no invented entrance, official-source checks for
the name/address/operating status, and Russian editorial content.
"""
import re

import pytest

from app.db.seed_data import seed_metadata_for
from app.models.enums import PlaceCategory
from app.models.place import Place
from app.schemas.place import PlaceDetail, PlaceRead
from scripts._phase_c6_1_candidate_review import CHECKED_ON, SELECTED
from scripts.validate_phase_b_catalog import catalog_digest, read_json
from scripts.validate_phase_c6_1_catalog import ROOT, audit

PRIOR_COUNT = 173
ROUTING_FIELDS = ("routing_latitude", "routing_longitude", "routing_point_source",
                  "routing_point_verified_on", "routing_access_status")


def catalog():
    return read_json(ROOT / "app/db/seed_places.json")


def additions():
    return catalog()[PRIOR_COUNT:]


def test_c6_1_manifest_replays_frozen_windows_and_additions():
    result = audit()
    assert result == read_json(ROOT / "docs/phase_c6_1_catalog_manifest.json")
    items = catalog()
    assert len(items) == 180
    assert result["original_count"] == 173 and result["added_count"] == 7
    assert result["new_catalog_keys"] == [item["catalog_key"] for item in items[PRIOR_COUNT:]]
    # The frozen C3 window is reproduced by digest, not merely by length.
    assert catalog_digest(items[:PRIOR_COUNT]) == result["frozen_window_sha256"]["173"]
    assert result["native_access_and_mapkit_validation"] == "NOT_TESTED"


def test_every_c6_1_addition_keeps_honest_access_and_editorial_content():
    items = additions()
    assert len(items) == 7
    assert {item["catalog_key"] for item in items} == set(SELECTED)
    for item in items:
        assert item["access_point_status"] == "representative_point_not_verified"
        assert "не проверен" in item["visit_note"].lower()
        assert item["source_checked_on"] == CHECKED_ON
        assert item["current_object_checked_on"] == CHECKED_ON
        assert item["coordinate_snapshot_timestamp"]
        assert item["source_url"] == item["coordinate_source_url"]
        assert item["source_url"].startswith("https://www.openstreetmap.org/")
        assert item["category"] in {member.value for member in PlaceCategory}
        assert type(item["is_unusual"]) is bool and 5 <= item["recommended_visit_minutes"] <= 180
        for field in ("title", "short_description", "full_description", "why_visit", "visit_note", "tags"):
            assert re.search("[А-Яа-яЁё]", item[field]), (item["catalog_key"], field)
        if item["visit_mode"] == "park_walk":
            assert "част" in item["visit_note"]
        if item["visit_mode"] == "indoor_museum":
            assert item["recommended_visit_minutes"] >= 45
        # Routing schema ships unfilled: no C6.1 record may invent an entrance.
        for field in ROUTING_FIELDS:
            assert item.get(field) is None


def test_c6_1_address_provenance_is_explicit_and_documented():
    verification = read_json(ROOT / "docs/phase_c6_1_source_verification.json")
    attractions = verification["attractions"]
    assert verification["checked_on"] == CHECKED_ON
    assert set(attractions) == set(SELECTED)
    snapshot = read_json(ROOT / "docs/phase_c6_1_osm_source_snapshot.json")
    elements = {(element["type"], element["id"]): element for element in snapshot["elements"]}
    for item in additions():
        entry = attractions[item["catalog_key"]]
        assert entry["checked_on"] == CHECKED_ON
        assert "name" in entry["confirms"] and "operating_status" in entry["confirms"]
        assert entry["official_source"] in item["verification_evidence"]
        kind, osm_id = item["source_url"].rsplit("/", 2)[1:]
        tags = elements[(kind, int(osm_id))]["tags"]
        if item["address_status"] == "official_source_verified":
            assert "address" in entry["confirms"], item["catalog_key"]
        else:
            assert item["address_status"] == "osm_tags", item["catalog_key"]
            assert tags.get("addr:street") and tags.get("addr:housenumber"), item["catalog_key"]
    # The two addresses that OSM already tags are the ones classified osm_tags.
    assert {item["catalog_key"] for item in additions() if item["address_status"] == "osm_tags"} == {
        "etagi-loft-project", "volshebnaya-milya-park"}


@pytest.mark.asyncio
async def test_c6_1_additions_are_served_with_representative_point_semantics(client):
    response = await client.get("/api/v1/places", params={"limit": 200})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 180
    titles = {item["title"] for item in body["items"]}
    expected = {item["title"] for item in additions()}
    assert expected <= titles
    new_key = "museum-of-optics-itmo"
    listed = next(item for item in body["items"] if item["title"] == seed_metadata_for(new_key)["title"])
    detail = await client.get(f"/api/v1/places/{listed['id']}")
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["routing_access_status"] == "representative_point_only"
    assert payload["routing_latitude"] is None and payload["routing_longitude"] is None
    # The optional historical layer stays absent, not broken, for new records.
    history = await client.get(f"/api/v1/places/{listed['id']}/history")
    assert history.status_code == 200
    assert history.json()["history"] is None


def test_c6_1_records_read_as_representative_points_through_the_schemas():
    items = additions()
    places = [Place(id=PRIOR_COUNT + index + 1, catalog_key=item["catalog_key"], title=item["title"],
                    short_description=item["short_description"], full_description=item["full_description"],
                    category=PlaceCategory(item["category"]), is_unusual=item["is_unusual"],
                    latitude=item["latitude"], longitude=item["longitude"], address=item["address"],
                    recommended_visit_minutes=item["recommended_visit_minutes"])
              for index, item in enumerate(items)]
    for place, item in zip(places, items):
        model = PlaceRead.model_validate(place)
        assert model.display_latitude == model.latitude == item["latitude"]
        assert model.display_longitude == model.longitude == item["longitude"]
        assert model.routing_access_status == "representative_point_only"
        assert model.routing_latitude is None and model.routing_longitude is None
        assert PlaceDetail.model_validate(place).routing_access_status == "representative_point_only"


@pytest.mark.asyncio
async def test_upgrade_from_c6_173_database_adds_c6_1_records_without_touching_ids(tmp_path, monkeypatch):
    """A C6-era database (173 non-contiguous ids) upgrades additively to 180."""
    import sqlite3

    from sqlalchemy import select, text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db import session as lifecycle
    from app.db.base import Base

    fields = ("catalog_key", "title", "short_description", "full_description", "category",
              "is_unusual", "latitude", "longitude", "address", "photo_url", "working_hours",
              "tags", "rating", "architectural_style", "year_built", "recommended_visit_minutes")
    path = tmp_path / "c6-to-c6-1.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    lifecycle.configure_sqlite_unicode(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(lifecycle, "engine", engine)
    monkeypatch.setattr(lifecycle, "async_session_factory", factory)
    prior = catalog()[:PRIOR_COUNT]
    expected_ids = {item["catalog_key"]: 500 + index * 3 for index, item in enumerate(prior)}
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as session:
            session.add_all([Place(id=expected_ids[item["catalog_key"]],
                                   **{field: item.get(field) for field in fields}) for item in prior])
            await session.commit()
        connection = sqlite3.connect(path)
        connection.execute("INSERT INTO places(id,title,short_description,full_description,category,"
                           "is_unusual,latitude,longitude,address) VALUES(777,'Мой дворик','Мой текст',"
                           "'Пользовательская запись','ART',1,59.95,30.35,'Адрес пользователя')")
        connection.commit()
        connection.close()

        await lifecycle.init_db()
        async with factory() as session:
            places = (await session.scalars(select(Place).order_by(Place.id))).all()
            keyed = {place.catalog_key: place for place in places if place.catalog_key}
            assert len(places) == 180 + 1 and len(keyed) == 180
            for item in prior:
                assert keyed[item["catalog_key"]].id == expected_ids[item["catalog_key"]]
            for item in catalog()[PRIOR_COUNT:]:
                assert keyed[item["catalog_key"]].latitude == item["latitude"]
            assert (await session.get(Place, 777)).catalog_key is None
            assert (await session.execute(text("PRAGMA foreign_key_check"))).all() == []
    finally:
        await engine.dispose()
