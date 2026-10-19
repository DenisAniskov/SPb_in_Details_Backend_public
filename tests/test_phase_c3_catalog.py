"""Phase C3: additive display/routing schema, entrance audit, and the C3 gate."""
from copy import deepcopy
import json

import pytest

from app.db.seed_data import seed_metadata_for
from app.models.enums import PlaceCategory
from app.models.place import Place
from app.schemas.place import PlaceDetail, PlaceRead
from scripts.validate_phase_c3_catalog import ROOT, audit, catalog_digest, read_json


def test_c3_manifest_replays_frozen_c2_and_additions():
    result = audit()
    assert result == read_json(ROOT / "docs/phase_c3_catalog_manifest.json")
    items = read_json(ROOT / "app/db/seed_places.json")
    c2_baseline = read_json(ROOT / "docs/phase_c2_catalog_baseline.json")
    # Phase C6.1 appends seven reviewed records after the C3 window (173 -> 180);
    # the C3 gate itself still validates exactly the frozen 173-record catalog.
    assert len(items) == 180 and len(c2_baseline) == 126
    assert items[:126] == c2_baseline
    assert result["original_count"] == 126 and result["added_count"] == 47
    assert result["new_district_counts"] == {
        "Выборгский район": 10, "Калининский район": 10, "Кировский район": 11,
        "Красносельский район": 8, "Приморский район": 8,
    }
    assert len(result["nearby_pair_reviews"]) == 1
    assert result["entrance_audit"]["verified_public_entrances"] == 0
    assert result["native_access_and_mapkit_validation"] == "NOT_TESTED"


def test_every_c3_addition_keeps_honest_access_and_editorial_content():
    items = read_json(ROOT / "app/db/seed_places.json")[126:173]
    assert len(items) == 47
    for item in items:
        assert item["access_point_status"] == "representative_point_not_verified"
        assert "не проверен" in item["visit_note"].lower()
        assert item["source_checked_on"] == "2026-10-10"
        if item["visit_mode"] == "park_walk":
            assert "част" in item["visit_note"]
        # Routing schema ships unfilled: no record may invent an entrance.
        for field in ("routing_latitude", "routing_longitude", "routing_point_source",
                      "routing_point_verified_on", "routing_access_status"):
            assert item.get(field) is None


def test_display_and_routing_schema_defaults_for_whole_catalog():
    items = read_json(ROOT / "app/db/seed_places.json")
    places = [Place(id=index + 1, catalog_key=i["catalog_key"], title=i["title"],
                    short_description=i["short_description"], full_description=i["full_description"],
                    category=i["category"], is_unusual=i["is_unusual"], latitude=i["latitude"],
                    longitude=i["longitude"], address=i["address"],
                    recommended_visit_minutes=i["recommended_visit_minutes"])
              for index, i in enumerate(items)]
    for place, item in zip(places, items):
        model = PlaceRead.model_validate(place)
        assert model.display_latitude == model.latitude == item["latitude"]
        assert model.display_longitude == model.longitude == item["longitude"]
        assert model.routing_latitude is None and model.routing_longitude is None
        assert model.routing_point_source is None and model.routing_point_verified_on is None
        assert model.routing_access_status == "representative_point_only"
        detail = PlaceDetail.model_validate(place)
        assert detail.routing_access_status == "representative_point_only"


def test_verified_entrance_metadata_is_exposed_and_half_filled_metadata_is_hidden():
    key = "vorontsovsky-garden"
    metadata = seed_metadata_for(key)
    assert metadata and "routing_latitude" not in metadata
    place = Place(id=1, catalog_key=key, title="Воронцовский сквер", short_description="s",
                  full_description="d", category=PlaceCategory.NATURE,
                  is_unusual=False, latitude=59.8467567, longitude=30.237876, address="x")
    # Simulate a future verified entrance recorded in the reviewed seed.
    filled = dict(metadata, routing_latitude=59.8466, routing_longitude=30.2389,
                  routing_point_source="https://www.openstreetmap.org/node/123",
                  routing_point_verified_on="2026-10-10",
                  routing_access_status="verified_public_entrance")
    import app.db.seed_data as seed_data
    original = seed_data._CATALOG_INDEX[key]
    seed_data._CATALOG_INDEX[key] = filled
    try:
        model = PlaceRead.model_validate(place)
        assert model.routing_latitude == 59.8466 and model.routing_longitude == 30.2389
        assert model.routing_point_source == "https://www.openstreetmap.org/node/123"
        assert model.routing_point_verified_on == "2026-10-10"
        assert model.routing_access_status == "verified_public_entrance"
        for broken in (dict(filled, routing_latitude=float("nan")),
                       dict(filled, routing_point_source="https://example.com/x"),
                       dict(filled, routing_access_status="access_uncertain"),
                       {k: filled[k] for k in filled if k != "routing_point_verified_on"}):
            seed_data._CATALOG_INDEX[key] = broken
            model = PlaceRead.model_validate(place)
            assert model.routing_latitude is None and model.routing_longitude is None
            assert model.routing_point_source is None
            assert model.routing_point_verified_on is None
            expected_status = ("access_uncertain" if broken is not None and broken.get("routing_access_status") == "access_uncertain"
                               else "representative_point_only")
            assert model.routing_access_status == expected_status
    finally:
        seed_data._CATALOG_INDEX[key] = original


def test_user_rows_without_catalog_identity_read_as_representative_point_only():
    place = Place(id=1, title="Пользовательское место", short_description="s", full_description="d",
                  category=PlaceCategory.ART,
                  is_unusual=True, latitude=59.9, longitude=30.3, address="y")
    model = PlaceRead.model_validate(place)
    assert model.display_latitude == 59.9
    assert model.routing_access_status == "representative_point_only"
    assert model.routing_latitude is None


def test_c2_window_stays_frozen_inside_the_c3_catalog():
    items = read_json(ROOT / "app/db/seed_places.json")
    manifest = read_json(ROOT / "docs/phase_c2_catalog_manifest.json")
    assert catalog_digest(items[:126]) == manifest["catalog_sha256"]


# --- Part D: SQLite migration and API compatibility ---

FIELDS = ("catalog_key", "title", "short_description", "full_description", "category", "is_unusual",
          "latitude", "longitude", "address", "photo_url", "working_hours", "tags", "rating",
          "architectural_style", "year_built", "recommended_visit_minutes")


@pytest.mark.asyncio
async def test_upgrade_from_c2_126_database_preserves_ids_users_and_references(tmp_path, monkeypatch):
    """A C2-era database (126 non-contiguous ids) upgrades additively to C3."""
    import sqlite3

    from sqlalchemy import select, text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db import session as lifecycle
    from app.db.base import Base

    path = tmp_path / "c2-to-c3.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    lifecycle.configure_sqlite_unicode(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(lifecycle, "engine", engine)
    monkeypatch.setattr(lifecycle, "async_session_factory", factory)
    baseline = read_json(ROOT / "docs/phase_c2_catalog_baseline.json")
    assert len(baseline) == 126
    expected_ids = {item["catalog_key"]: 500 + index * 3 for index, item in enumerate(baseline)}
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as session:
            session.add_all([Place(id=expected_ids[item["catalog_key"]],
                                   **{f: item.get(f) for f in FIELDS})
                             for item in baseline])
            await session.commit()
        connection = sqlite3.connect(path)
        connection.execute("INSERT INTO places(id,title,short_description,full_description,category,"
                           "is_unusual,latitude,longitude,address) VALUES(777,'Мой дворик','Мой текст',"
                           "'Пользовательская запись','ART',1,59.95,30.35,'Адрес пользователя')")
        connection.execute("CREATE TABLE saved_walk_references(place_id INTEGER NOT NULL REFERENCES places(id))")
        connection.executemany("INSERT INTO saved_walk_references VALUES (?)",
                               [(777,), (expected_ids[baseline[0]["catalog_key"]],),
                                (expected_ids[baseline[-1]["catalog_key"]],)])
        connection.commit()
        connection.close()

        await lifecycle.init_db()
        await lifecycle.init_db()
        async with factory() as session:
            places = (await session.scalars(select(Place).order_by(Place.id))).all()
            keyed = {p.catalog_key: p for p in places if p.catalog_key}
            # C2-era rows keep their ids; the whole current catalog (180 records
            # after the C6.1 additions) is seeded around them.
            assert len(places) == 180 + 1 and len(keyed) == 180
            for item in baseline:
                place = keyed[item["catalog_key"]]
                assert place.id == expected_ids[item["catalog_key"]]
                for field in FIELDS:
                    assert getattr(place, field) == item.get(field)
            assert (await session.get(Place, 777)).catalog_key is None
            assert (await session.scalars(text("SELECT place_id FROM saved_walk_references "
                                              "ORDER BY place_id"))).all() == sorted(
                [777, expected_ids[baseline[0]["catalog_key"]], expected_ids[baseline[-1]["catalog_key"]]])
            assert (await session.execute(text("PRAGMA foreign_key_check"))).all() == []
            # API schema reads old rows without routing metadata honestly.
            model = PlaceRead.model_validate(keyed[baseline[0]["catalog_key"]])
            assert model.display_latitude == baseline[0]["latitude"]
            assert model.routing_latitude is None
            assert model.routing_access_status == "representative_point_only"
    finally:
        await engine.dispose()
