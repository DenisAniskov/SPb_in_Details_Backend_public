"""C2 additive evidence and upgrades exercised only in temporary SQLite files."""
from copy import deepcopy
import json
import sqlite3

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.seed_data import SPB_SEED_PLACES
from app.models.enums import PlaceCategory
from app.models.place import Place
from scripts.validate_phase_c2_catalog import (
    ROOT, ORIGINAL_COUNT, audit, catalog_digest, read_json,
    validate_acquisition, validate_catalog, file_sha256,
)


def inputs():
    # Phase C3 appends after the frozen C2 window; C2 tests keep exercising
    # exactly the 126-record C2 catalog.
    return (read_json(ROOT / "app/db/seed_places.json")[:126],
            read_json(ROOT / "docs/phase_c2_osm_source_snapshot.json"))


def test_manifest_replays_original_77_and_every_source_and_district():
    result = audit()
    assert result == read_json(ROOT / "docs/phase_c2_catalog_manifest.json")
    items, _ = inputs()
    original = read_json(ROOT / "docs/phase_c1_catalog_baseline.json")
    assert items[:77] == original
    assert result["original_catalog_sha256"] == catalog_digest(original)
    assert result["final_count"] == 126 and result["added_count"] == 49
    assert len(result["district_counts"]) == 13
    assert result["new_district_counts"] == {"Красногвардейский район":6, "Невский район":25, "Фрунзенский район":18}
    assert result["new_coordinate_kind_counts"] == {"osm_bounds_center":35, "osm_node":14}
    assert len(result["nearby_pair_reviews"]) == 1
    assert len({i["catalog_key"] for i in items}) == len(items)


@pytest.mark.parametrize("field,value,error", [
    ("recommended_visit_minutes", 1, "5..180"),
    ("recommended_visit_minutes", True, "5..180"),
    ("latitude", float("nan"), "finite numbers"),
    ("longitude", True, "finite numbers"),
    ("category", "unknown", "category"),
    ("access_point_status", "verified_main_entrance", "falsely verified"),
    ("coordinate_kind", "osm_entrance", "OSM reference"),
    ("coordinate_uncertainty", "", "coordinate_uncertainty"),
    ("short_description", "TODO", "placeholder"),
    ("source_url", "https://example.org/way/1721304", "OSM reference"),
    ("visit_note", "Entrance confirmed", "Russian editorial"),
])
def test_new_catalog_record_rejects_corrupt_or_invented_claims(field, value, error):
    items, snapshot = inputs()
    changed = deepcopy(items)
    changed[77][field] = value
    with pytest.raises(ValueError, match=error):
        validate_catalog(changed, snapshot)


@pytest.mark.parametrize("tag,value", [
    ("access", "private"), ("access", "no"), ("access", "permit"),
    ("access", "customers"), ("access", "restricted"),
    ("foot", "private"), ("foot", "no"), ("military", "barracks"),
    ("building", "collapsed"), ("amenity", "school"),
])
def test_restricted_or_institutional_new_sources_are_not_assumed_public(tag, value):
    items, snapshot = inputs()
    changed = deepcopy(snapshot)
    changed["elements"][0]["tags"][tag] = value
    with pytest.raises(ValueError, match="restricted|inactive"):
        validate_catalog(items, changed)


def test_original_77_cannot_change_content_order_identity_or_duration():
    items, snapshot = inputs()
    for field, value in (("catalog_key", "renamed-original"), ("recommended_visit_minutes", 5),
                         ("why_visit", "Новое описание"), ("latitude", 59.90)):
        changed = deepcopy(items)
        changed[30][field] = value
        with pytest.raises(ValueError, match="Original 77"):
            validate_catalog(changed, snapshot)
    changed = deepcopy(items)
    changed[1], changed[2] = changed[2], changed[1]
    with pytest.raises(ValueError, match="Original 77"):
        validate_catalog(changed, snapshot)


def test_canonical_and_semantic_duplicate_rejection():
    items, snapshot = inputs()
    changed = deepcopy(items)
    changed[78]["source_url"] = changed[77]["source_url"]
    with pytest.raises(ValueError, match="duplicate canonical OSM"):
        validate_catalog(changed, snapshot)
    changed = deepcopy(items)
    changed[78]["title"] = changed[77]["title"]
    with pytest.raises(ValueError, match="Duplicate normalized visitor title"):
        validate_catalog(changed, snapshot)
    changed = deepcopy(snapshot)
    changed["elements"].append(deepcopy(changed["elements"][0]))
    with pytest.raises(ValueError, match="Duplicate OSM|Duplicate additive"):
        validate_catalog(items, changed)


def test_raw_acquisition_hash_version_and_source_content_are_binding():
    _, snapshot = inputs()
    requests = read_json(ROOT / "docs/phase_c2_acquisition/requests.json")
    path = ROOT / "docs/phase_c2_acquisition/east_south_kumi.json"
    raw = read_json(path)
    with pytest.raises(ValueError, match="binding invalid"):
        validate_acquisition(snapshot, requests, raw, "0"*64)
    changed = deepcopy(snapshot)
    changed["elements"][0]["tags"]["name"] = "Придуманное название"
    with pytest.raises(ValueError, match="not identical"):
        validate_acquisition(changed, requests, raw, file_sha256(path))
    changed = deepcopy(snapshot)
    changed["license"] = "public domain"
    with pytest.raises(ValueError, match="licence"):
        validate_acquisition(changed, requests, raw, file_sha256(path))


FIELDS = ("catalog_key", "title", "short_description", "full_description", "category", "is_unusual",
          "latitude", "longitude", "address", "photo_url", "working_hours", "tags", "rating",
          "architectural_style", "year_built", "recommended_visit_minutes")


@pytest.mark.asyncio
async def test_fresh_c2_database_starts_idempotently_without_live_data(tmp_path, monkeypatch):
    from app.db import session as lifecycle
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}")
    lifecycle.configure_sqlite_unicode(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(lifecycle, "engine", engine)
    monkeypatch.setattr(lifecycle, "async_session_factory", factory)
    try:
        await lifecycle.init_db()
        async with factory() as session:
            first = (await session.scalars(select(Place).order_by(Place.id))).all()
            ids = {p.catalog_key:p.id for p in first}
            assert len(ids) == len(SPB_SEED_PLACES)
        await lifecycle.init_db()
        async with factory() as session:
            second = (await session.scalars(select(Place).order_by(Place.id))).all()
            assert {p.catalog_key:p.id for p in second} == ids
            assert (await session.execute(text("PRAGMA foreign_key_check"))).all() == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_upgrade_exactly_77_noncontiguous_ids_preserves_user_rows_references_and_legacy(tmp_path, monkeypatch, legacy):
    from app.db import session as lifecycle
    path = tmp_path / f"upgrade-{legacy}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    lifecycle.configure_sqlite_unicode(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(lifecycle, "engine", engine)
    monkeypatch.setattr(lifecycle, "async_session_factory", factory)
    originals = SPB_SEED_PLACES[:77]
    expected_ids = {p["catalog_key"]:1000+index*7 for index,p in enumerate(originals)}
    try:
        if legacy:
            connection = sqlite3.connect(path)
            connection.execute("""CREATE TABLE places (
                id INTEGER PRIMARY KEY, title VARCHAR(200) NOT NULL,
                short_description VARCHAR(500) NOT NULL, full_description TEXT NOT NULL,
                category VARCHAR(30) NOT NULL, is_unusual BOOLEAN,
                latitude FLOAT NOT NULL, longitude FLOAT NOT NULL, address VARCHAR(300) NOT NULL,
                photo_url VARCHAR(500), working_hours VARCHAR(200), tags VARCHAR(300), rating FLOAT,
                architectural_style VARCHAR(150), year_built VARCHAR(50))""")
            for index, item in enumerate(originals):
                coords = item.get("legacy_coordinates", [[item["latitude"],item["longitude"]]])[0]
                connection.execute("INSERT INTO places(id,title,short_description,full_description,category,is_unusual,latitude,longitude,address) VALUES(?,?,?,?,?,?,?,?,?)",
                                   (expected_ids[item["catalog_key"]], item.get("legacy_titles",[item["title"]])[0],
                                    item["short_description"],item["full_description"], item["category"].name,
                                    item["is_unusual"], *coords, item["address"]))
            connection.commit()
            connection.close()
        else:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with factory() as session:
                session.add_all([Place(id=expected_ids[item["catalog_key"]], **{f:item.get(f) for f in FIELDS}) for item in originals])
                await session.commit()
        # Both fixtures have exactly 77 reviewed records plus two unrelated user
        # records. The same-title lookalike has different coordinates and is not
        # claimed; saved references point to original and user IDs.
        connection = sqlite3.connect(path)
        connection.execute("INSERT INTO places(id,title,short_description,full_description,category,is_unusual,latitude,longitude,address) VALUES(41,?,'Авторское место','Сохранённая пользователем запись','NATURE',0,59.91,30.49,'Адрес пользователя')", (originals[0]["title"],))
        connection.execute("INSERT INTO places(id,title,short_description,full_description,category,is_unusual,latitude,longitude,address) VALUES(42,'Мой вид на город','Мой текст','Другой пользовательский текст','ART',1,59.92,30.48,'Авторский адрес')")
        connection.execute("CREATE TABLE saved_walk_references(place_id INTEGER NOT NULL REFERENCES places(id))")
        refs = [41,42,1000,1000+76*7]
        connection.executemany("INSERT INTO saved_walk_references VALUES (?)",[(id,) for id in refs])
        users_before = connection.execute("SELECT id,title,short_description,full_description,category,is_unusual,latitude,longitude,address FROM places WHERE id IN (41,42) ORDER BY id").fetchall()
        connection.commit()
        connection.close()
        await lifecycle.init_db()
        await lifecycle.init_db()
        async with factory() as session:
            places = (await session.scalars(select(Place).order_by(Place.id))).all()
            keyed = {p.catalog_key:p for p in places if p.catalog_key}
            assert len(places) == len(SPB_SEED_PLACES) + 2 and len(keyed) == len(SPB_SEED_PLACES)
            for item in originals:
                place = keyed[item["catalog_key"]]
                assert place.id == expected_ids[item["catalog_key"]]
                for field in FIELDS:
                    assert getattr(place,field) == item.get(field)
            users_after = (await session.execute(text("SELECT id,title,short_description,full_description,category,is_unusual,latitude,longitude,address FROM places WHERE id IN (41,42) ORDER BY id"))).all()
            assert [tuple(row) for row in users_after] == users_before
            for id in [41,42]:
                assert (await session.get(Place,id)).catalog_key is None
            assert (await session.scalars(text("SELECT place_id FROM saved_walk_references ORDER BY place_id"))).all() == sorted(refs)
            assert (await session.execute(text("PRAGMA foreign_key_check"))).all() == []
            first_ids = {key:p.id for key,p in keyed.items()}
        await lifecycle.init_db()
        async with factory() as session:
            assert {p.catalog_key:p.id for p in (await session.scalars(select(Place).where(Place.catalog_key.is_not(None)))).all()} == first_ids
    finally:
        await engine.dispose()
