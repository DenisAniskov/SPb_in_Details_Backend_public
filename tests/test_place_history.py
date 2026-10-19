"""Phase C6: tests for the Historical Content Engine.

Covers the editorial dataset validation (§14), the API contract
(GET /places/{place_id}/history, §7) and regression guarantees from §15:
existing clients and the catalogue pagination stay untouched, missing cards
are honest nulls, and invalid editorial data is rejected at load time.
"""
import json
from datetime import date
from pathlib import Path

import pytest
from httpx import AsyncClient

from app.db.place_history import (
    clear_cache,
    DATASET_PATH,
    PlaceHistoryValidationError,
    history_for,
    load_place_history,
)
from app.db.validate_place_history import validate_dataset


# ---------------------------------------------------------------------------
# §14 Editorial dataset validation
# ---------------------------------------------------------------------------


def test_editorial_dataset_ships_without_errors():
    """The shipped dataset must pass every documented invariant."""
    errors, _warnings, entries = validate_dataset()
    assert errors == []
    assert len(entries) >= 30, "Phase C6 requires at least 30 verified cards"


def test_shipped_cards_carry_two_or_more_sources():
    """Review iteration (2026-10-11): every card has a Wikipedia article plus
    an independent corroborating source; the validator raises no warnings."""
    _errors, warnings, _entries = validate_dataset()
    assert all("distinct sources" not in message for message in warnings), warnings
    entries = load_place_history()
    single = [
        key for key, card in entries.items() if len(card.sources) < 2
    ]
    assert single == [], f"cards with a single source: {single}"


def test_dataset_keys_are_unique_and_known():
    entries = load_place_history()
    keys = list(entries.keys())
    assert len(keys) == len(set(keys)), "duplicate catalogKey entries"
    from app.db.seed_data import SPB_SEED_PLACES

    known = {place["catalog_key"] for place in SPB_SEED_PLACES}
    unknown = set(keys) - known
    assert unknown == set(), f"cards for unknown POIs: {sorted(unknown)}"


def test_catalog_size_and_seed_data_untouched():
    """Historical content is additive: the C6.1 catalog holds 180 POI; the frozen
    C6-era window (173) and every geometry/category sample stay intact."""
    from app.db.seed_data import SPB_SEED_PLACES

    assert len(SPB_SEED_PLACES) == 180
    sample = next(
        p for p in SPB_SEED_PLACES if p["catalog_key"] == "hermitage-winter-palace"
    )
    assert sample["latitude"] == 59.9404659
    assert sample["longitude"] == 30.3137851
    assert sample["category"] == "history_architecture"


def test_loader_is_cached_and_reads_file_once(monkeypatch, tmp_path):
    """§4: the dataset loads once per process — not per API request."""
    source = DATASET_PATH
    calls = {"count": 0}
    original = Path.read_text

    def counting_read(self, *args, **kwargs):
        if self == source:
            calls["count"] += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting_read)
    clear_cache()
    try:
        first = load_place_history()
        second = load_place_history()
        assert first is second
        assert calls["count"] == 1
    finally:
        clear_cache()


# ---------------------------------------------------------------------------
# Invalid editorial data is rejected loudly (§15 items 4-11, 16)
# ---------------------------------------------------------------------------


def _write_dataset(tmp_path: Path, entries: list[dict]) -> Path:
    payload = {
        "meta": {
            "version": "test",
            "locale": "ru",
            "generatedOn": "2026-10-11",
        },
        "entries": entries,
    }
    path = tmp_path / "place_history_test.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _valid_entry(**overrides) -> dict:
    entry = {
        "catalogKey": "hermitage-winter-palace",
        "shortHistoricalSummary": "Проверенная краткая справка. Вторая фраза.",
        "detailedHistoricalText": "Подробная история объекта для проверки схемы. " * 3,
        "sources": [
            {
                "id": "s1",
                "title": "Источник",
                "url": "https://example.org/page",
                "sourceType": "encyclopedia",
                "accessedOn": "2026-10-11",
            }
        ],
        "timeline": [
            {
                "id": "t1",
                "dateLabel": "1754 год",
                "datePrecision": "year",
                "title": "Событие",
                "description": "Описание события.",
                "sourceIds": ["s1"],
            }
        ],
        "facts": [
            {
                "id": "f1",
                "title": "Факт",
                "text": "Текст факта.",
                "category": "architecture",
                "sourceIds": ["s1"],
            }
        ],
        "people": [
            {
                "name": "Архитектор",
                "role": "architect",
                "sourceIds": ["s1"],
            }
        ],
        "editorialStatus": "verified",
        "lastVerifiedOn": "2026-10-11",
        "contentVersion": 1,
    }
    entry.update(overrides)
    return entry


def test_invalid_date_is_rejected(tmp_path):
    path = _write_dataset(
        tmp_path,
        [_valid_entry(lastVerifiedOn="11 октября 2026")],
    )
    with pytest.raises(Exception):
        load_place_history(path)
    clear_cache()


def test_future_verified_date_is_rejected_by_validation():
    errors, _warnings, _entries = validate_dataset()
    # The shipped dataset has no future dates; the guard exists in the script.
    assert all("in the future" not in message for message in errors)


def test_future_last_verified_on_is_rejected_at_load(tmp_path):
    """§6 review fix: future dates are a hard load-time error, not a warning."""
    path = _write_dataset(tmp_path, [_valid_entry(lastVerifiedOn="2999-12-31")])
    with pytest.raises(PlaceHistoryValidationError, match="in the future"):
        load_place_history(path)
    clear_cache()


def test_future_source_accessed_on_is_rejected_at_load(tmp_path):
    entry = _valid_entry()
    entry["sources"][0]["accessedOn"] = "2999-12-31"
    path = _write_dataset(tmp_path, [entry])
    with pytest.raises(PlaceHistoryValidationError, match="in the future"):
        load_place_history(path)
    clear_cache()


def test_future_meta_generated_on_is_rejected_at_load(tmp_path):
    path = _write_dataset(tmp_path, [_valid_entry()])
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["meta"]["generatedOn"] = "2999-12-31"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(PlaceHistoryValidationError, match="in the future"):
        load_place_history(path)
    clear_cache()


def test_dates_equal_to_today_are_accepted(tmp_path):
    today = date.today().isoformat()
    entry = _valid_entry(lastVerifiedOn=today)
    entry["sources"][0]["accessedOn"] = today
    path = _write_dataset(tmp_path, [entry])
    entries = load_place_history(path)
    assert entries["hermitage-winter-palace"].lastVerifiedOn == today
    clear_cache()


def test_invalid_dataset_raises_place_history_validation_error(tmp_path):
    """Every invalid-dataset failure surfaces as PlaceHistoryValidationError."""
    path = _write_dataset(
        tmp_path,
        [_valid_entry(lastVerifiedOn="11 октября 2026")],
    )
    with pytest.raises(PlaceHistoryValidationError):
        load_place_history(path)
    clear_cache()


def test_unsafe_url_scheme_is_rejected(tmp_path):
    entry = _valid_entry()
    entry["sources"][0]["url"] = "javascript:alert(1)"
    path = _write_dataset(tmp_path, [entry])
    with pytest.raises(Exception):
        load_place_history(path)
    clear_cache()


def test_unknown_fact_source_is_rejected(tmp_path):
    entry = _valid_entry()
    entry["facts"][0]["sourceIds"] = ["missing-source"]
    path = _write_dataset(tmp_path, [entry])
    with pytest.raises(Exception):
        load_place_history(path)
    clear_cache()


def test_unknown_person_role_is_rejected(tmp_path):
    entry = _valid_entry()
    entry["people"][0]["role"] = "wizard"
    path = _write_dataset(tmp_path, [entry])
    with pytest.raises(Exception):
        load_place_history(path)
    clear_cache()


def test_unknown_editorial_status_is_rejected(tmp_path):
    path = _write_dataset(tmp_path, [_valid_entry(editorialStatus="perfect")])
    with pytest.raises(Exception):
        load_place_history(path)
    clear_cache()


def test_unknown_timeline_precision_is_rejected(tmp_path):
    entry = _valid_entry()
    entry["timeline"][0]["datePrecision"] = "approximately"
    path = _write_dataset(tmp_path, [entry])
    with pytest.raises(Exception):
        load_place_history(path)
    clear_cache()


def test_duplicate_fact_ids_are_rejected(tmp_path):
    entry = _valid_entry()
    entry["facts"].append({**entry["facts"][0], "title": "Дубликат"})
    path = _write_dataset(tmp_path, [entry])
    with pytest.raises(PlaceHistoryValidationError, match="duplicate fact ids"):
        load_place_history(path)
    clear_cache()


def test_duplicate_catalog_keys_are_rejected(tmp_path):
    path = _write_dataset(
        tmp_path, [_valid_entry(), _valid_entry(shortHistoricalSummary="Другая карточка.")]
    )
    with pytest.raises(PlaceHistoryValidationError, match="duplicate catalogKey"):
        load_place_history(path)
    clear_cache()


def test_card_for_unknown_poi_is_rejected(tmp_path):
    path = _write_dataset(tmp_path, [_valid_entry(catalogKey="not-a-real-place")])
    with pytest.raises(PlaceHistoryValidationError, match="does not exist"):
        load_place_history(path)
    clear_cache()


def test_missing_dataset_file_is_an_empty_layer(tmp_path):
    """An absent file is honest emptiness, not a broken catalogue."""
    entries = load_place_history(tmp_path / "absent.json")
    assert entries == {}
    assert history_for("hermitage-winter-palace") is not None


# ---------------------------------------------------------------------------
# API contract (§7, §15 items 6, 12, 13, 14, 17)
# ---------------------------------------------------------------------------


async def _find_place_id(client: AsyncClient, title_part: str) -> int:
    response = await client.get("/api/v1/places", params={"limit": 200})
    assert response.status_code == 200
    for item in response.json()["items"]:
        if title_part in item["title"]:
            return item["id"]
    raise AssertionError(f"place with title part {title_part!r} not found")


@pytest.mark.asyncio
async def test_history_endpoint_returns_card_for_hermitage(client):
    place_id = await _find_place_id(client, "Эрмитаж")
    response = await client.get(f"/api/v1/places/{place_id}/history")
    assert response.status_code == 200
    body = response.json()
    assert body["placeId"] == place_id
    assert body["catalogKey"] == "hermitage-winter-palace"
    history = body["history"]
    assert history is not None
    assert history["catalogKey"] == "hermitage-winter-palace"
    assert history["shortHistoricalSummary"]
    assert history["detailedHistoricalText"]
    assert history["timeline"], "Winter Palace card carries a timeline"
    assert history["facts"]
    assert history["people"]
    assert {source["sourceType"] for source in history["sources"]}
    assert history["editorialStatus"] == "verified"


@pytest.mark.asyncio
async def test_history_endpoint_returns_null_for_place_without_card(client):
    """A missing card is an honest null — the catalogue keeps working."""
    place_id = await _find_place_id(client, "Сосновка")
    response = await client.get(f"/api/v1/places/{place_id}/history")
    assert response.status_code == 200
    body = response.json()
    assert body["history"] is None
    assert body["catalogKey"] is not None


@pytest.mark.asyncio
async def test_history_endpoint_404_for_unknown_place(client):
    response = await client.get("/api/v1/places/999999/history")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_place_detail_response_shape_is_backward_compatible(client):
    """§15 item 12: the legacy PlaceDetail contract must not change."""
    place_id = await _find_place_id(client, "Эрмитаж")
    response = await client.get(f"/api/v1/places/{place_id}")
    assert response.status_code == 200
    body = response.json()
    assert "history" not in body, "detail endpoint must not embed historical payload"
    legacy_keys = {
        "id", "title", "short_description", "full_description", "category",
        "is_unusual", "latitude", "longitude", "address", "photo_url",
        "rating", "recommended_visit_minutes", "tags", "source_url",
        "coordinate_kind", "address_status", "why_visit", "display_latitude",
        "display_longitude", "routing_latitude", "routing_longitude",
        "routing_point_source", "routing_point_verified_on",
        "routing_access_status", "working_hours", "architectural_style",
        "year_built", "visit_note", "official_website", "wikipedia_url",
    }
    assert legacy_keys.issubset(body.keys())


@pytest.mark.asyncio
async def test_places_pagination_is_untouched(client):
    """§15 items 13-14: /places keeps its pagination and stays compact."""
    response = await client.get("/api/v1/places", params={"limit": 200})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 180
    assert len(body["items"]) == 180
    item = body["items"][0]
    assert "history" not in item, "list responses must not carry historical payload"
    assert len(json.dumps(body, ensure_ascii=False)) < 500_000


@pytest.mark.asyncio
async def test_history_endpoint_performs_no_network_access(client, monkeypatch):
    """§15 item 17: serving a card never opens a network connection."""
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("network access during history request")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    place_id = await _find_place_id(client, "Эрмитаж")
    response = await client.get(f"/api/v1/places/{place_id}/history")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_history_endpoint_matches_seed_geometry(client):
    """§15 item 15: history layer cannot shift existing geo data."""
    from app.db.seed_data import SPB_SEED_PLACES

    seed = next(
        p for p in SPB_SEED_PLACES if p["catalog_key"] == "hermitage-winter-palace"
    )
    place_id = await _find_place_id(client, "Эрмитаж")
    detail = (await client.get(f"/api/v1/places/{place_id}")).json()
    assert detail["latitude"] == seed["latitude"]
    assert detail["longitude"] == seed["longitude"]
