"""C7.1 Shade API contract tests (§11, §17).

MIGRATION NOTE (documented in PHASE_C7_1_API_CONTRACT.md): this file replaces
the v1 prototype API test. The endpoint moved from a single-instant
``route + evaluated_at`` request to ``departure_at + legs[]`` with native
walking durations and POI visit durations, and the response gained the
three-state lengths, coverage/provenance metrics and confidence. All v1
honesty assertions are preserved:

* a degenerate route is rejected with 422;
* a route outside the SPb region is rejected with 422;
* without verified building data the endpoint returns ``unavailable`` and a
  reason — it NEVER fabricates a shade percentage;
* synthetic fixtures are injected ONLY through a dependency override.

The synthetic provider is used here so the tests are offline and
deterministic; the production wiring is exercised in
``test_c7_1_building_dataset.py`` against the real pilot dataset.
"""

from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.v1.endpoints.environment import get_shade_engine
from app.main import app
from app.services.shade_engine.buildings import (
    BuildingFootprint,
    HeightProvenance,
    SyntheticBuildingProvider,
)
from app.services.shade_engine.geo import LocalFrame
from app.services.shade_engine.time_aware import TimeAwareShadeEngine

MSK = timezone(timedelta(hours=3))
DEPARTURE = datetime(2026, 7, 1, 14, 0, tzinfo=MSK)

FRAME = LocalFrame(lat0=59.9398, lon0=30.3146)


def _rect(x: float, y: float, width: float, depth: float, height: float | None,
          provenance: HeightProvenance) -> BuildingFootprint:
    corners = [(x - width / 2, y - depth / 2), (x + width / 2, y - depth / 2),
               (x + width / 2, y + depth / 2), (x - width / 2, y + depth / 2)]
    return BuildingFootprint(
        polygon_latlon=tuple(FRAME.to_latlon(px, py) for px, py in corners),
        height_m=height, provenance=provenance,
        source="SYNTHETIC TEST FIXTURE — NOT REAL SPb DATA")


SYNTHETIC = SyntheticBuildingProvider(footprints=(
    _rect(0.0, 0.0, 40.0, 20.0, 25.0, HeightProvenance.OSM_DECLARED),
    _rect(0.0, 60.0, 40.0, 20.0, 16.5, HeightProvenance.LEVELS_ESTIMATE),
    _rect(0.0, 120.0, 40.0, 20.0, 20.0, HeightProvenance.UNAVAILABLE),
))


def _engine() -> TimeAwareShadeEngine:
    return TimeAwareShadeEngine(building_provider=SYNTHETIC)


@pytest.fixture()
async def client():
    app.dependency_overrides[get_shade_engine] = _engine
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


def _ns_leg(y_from: float, y_to: float, seconds: float, visit: float = 0.0,
            x: float = -40.0) -> dict:
    return {
        "geometry": [
            {"latitude": FRAME.to_latlon(x, y_from)[0], "longitude": FRAME.to_latlon(x, y_from)[1]},
            {"latitude": FRAME.to_latlon(x, y_to)[0], "longitude": FRAME.to_latlon(x, y_to)[1]},
        ],
        "walking_duration_seconds": seconds,
        "visit_duration_seconds_after_leg": visit,
    }


def _ew_leg(x_from: float, x_to: float, y: float, seconds: float) -> dict:
    return {
        "geometry": [
            {"latitude": FRAME.to_latlon(x_from, y)[0], "longitude": FRAME.to_latlon(x_from, y)[1]},
            {"latitude": FRAME.to_latlon(x_to, y)[0], "longitude": FRAME.to_latlon(x_to, y)[1]},
        ],
        "walking_duration_seconds": seconds,
        "visit_duration_seconds_after_leg": 0.0,
    }


async def test_naive_departure_is_rejected(client):
    response = await client.post("/api/v1/environment/shade/analyze", json={
        "departure_at": "2026-07-01T14:00:00",  # no timezone
        "legs": [_ns_leg(0.0, 100.0, 120.0)],
    })
    assert response.status_code == 422


async def test_degenerate_route_is_rejected(client):
    lat, lon = FRAME.to_latlon(-40.0, 10.0)
    response = await client.post("/api/v1/environment/shade/analyze", json={
        "departure_at": DEPARTURE.isoformat(),
        "legs": [{"geometry": [{"latitude": lat, "longitude": lon},
                               {"latitude": lat, "longitude": lon}],
                  "walking_duration_seconds": 100.0}],
    })
    assert response.status_code == 422
    assert "вырожден" in response.json()["detail"]


async def test_route_outside_spb_is_rejected(client):
    response = await client.post("/api/v1/environment/shade/analyze", json={
        "departure_at": DEPARTURE.isoformat(),
        "legs": [{"geometry": [{"latitude": 55.75, "longitude": 37.61},
                               {"latitude": 55.76, "longitude": 37.62}],
                  "walking_duration_seconds": 100.0}],
    })
    assert response.status_code == 422
    assert "вне зоны" in response.json()["detail"]


async def test_no_walking_duration_is_rejected(client):
    response = await client.post("/api/v1/environment/shade/analyze", json={
        "departure_at": DEPARTURE.isoformat(),
        "legs": [{"geometry": [
            {"latitude": FRAME.to_latlon(-40.0, 0.0)[0], "longitude": FRAME.to_latlon(-40.0, 0.0)[1]},
            {"latitude": FRAME.to_latlon(-40.0, 100.0)[0], "longitude": FRAME.to_latlon(-40.0, 100.0)[1]}],
            "walking_duration_seconds": 0}],
    })
    assert response.status_code == 422


async def test_available_analysis_reports_three_states_and_invariant(client):
    # At 14:00 MSK on 1 July the sun is SSW (azimuth ≈ 203°, elevation ≈ 51.6°),
    # so the 25 m block at x≈0 casts its shadow NORTH-EAST reaching y ≈ 28 m
    # (L = 25/tan 51.6° ≈ 19.8 m along the anti-solar direction). An east-west
    # route at y = +27 crosses that band and is partly shaded, partly sunlit.
    response = await client.post("/api/v1/environment/shade/analyze", json={
        "departure_at": DEPARTURE.isoformat(),
        "legs": [_ew_leg(-60.0, 60.0, 27.0, 300.0)],
        "include_segments": True,
    })
    assert response.status_code == 200
    body = response.json()
    assert body["status"] in ("available", "partial")
    assert body["estimate"] is True
    assert body["shade_fraction"] is not None
    total = body["route_length_meters"]
    shaded = body["estimated_shaded_meters"]
    exposed = body["estimated_exposed_meters"]
    unknown = body["unknown_meters"]
    assert abs((shaded + exposed + unknown) - total) <= max(1.0, total * 0.01)
    assert body["confidence"] in ("medium", "low")
    provenance = body["height_provenance_metrics"]
    assert provenance["buildings_considered"] >= 1
    assert set(provenance) == {"externally_verified", "osm_declared", "levels_estimate",
                               "unavailable", "buildings_considered"}
    assert provenance["externally_verified"] == 0  # synthetic fixtures are not "verified"
    assert body["solar_summary"]["max_elevation_deg"] > 0
    states = [s["state"] for s in body["classified_segments"]]
    assert set(states) <= {"estimated_shaded", "estimated_exposed", "unknown", "mixed"}
    assert any(s in ("estimated_shaded", "mixed") for s in states)
    assert any(s == "estimated_exposed" for s in states)


async def test_classified_segments_carry_real_geometry_and_measured_spans(client):
    """C7.1.1: segments report the REAL polyline and WHERE the shade is.

    Every segment carries its own geometry (never a chord the client would have
    to guess), and the shaded/unknown intervals are positions along it: their
    totals match the reported lengths, they stay inside the segment, and a
    `mixed` segment measures less than its whole length in either state.
    """
    response = await client.post("/api/v1/environment/shade/analyze", json={
        "departure_at": DEPARTURE.isoformat(),
        "legs": [_ew_leg(-60.0, 60.0, 27.0, 300.0)],
        "include_segments": True,
    })
    assert response.status_code == 200
    segments = response.json()["classified_segments"]
    assert segments
    for segment in segments:
        geometry = segment["geometry"]
        assert len(geometry) >= 2, "участок несёт реальную полилинию, а не хорду"
        length = segment["length_meters"]
        assert segment["ends_at_seconds"] > segment["starts_at_seconds"]
        spans = [*segment["shaded_spans"], *segment["unknown_spans"]]
        for start, end in spans:
            assert 0.0 <= start <= end <= length + 0.2
        ordered = sorted(spans)
        for (_a0, a1), (b0, _b1) in zip(ordered, ordered[1:]):
            assert a1 <= b0 + 0.2, "тень и «неизвестно» не пересекаются"
        shaded_span_total = sum(b - a for a, b in segment["shaded_spans"])
        unknown_span_total = sum(b - a for a, b in segment["unknown_spans"])
        assert abs(shaded_span_total - segment["shaded_meters"]) <= 0.2
        assert abs(unknown_span_total - segment["unknown_meters"]) <= 0.2
        if segment["state"] == "mixed":
            assert segment["shaded_meters"] + segment["unknown_meters"] < length
    assert any(s["state"] == "mixed" and s["shaded_spans"] for s in segments), (
        "хотя бы один участок измеряет положение тени, а не только её длину")


async def test_unknown_height_buildings_make_ground_unknown_not_sun(client):
    """A point reachable only by the unknown-height building must be unknown."""
    # The third synthetic building (no height) is at y = 120 with a 40 × 20 m
    # footprint; a route crossing y = 100..145 sits in its shadow envelope.
    response = await client.post("/api/v1/environment/shade/analyze", json={
        "departure_at": DEPARTURE.isoformat(),
        "legs": [_ns_leg(95.0, 150.0, 120.0, x=0.0)],
        "include_segments": True,
    })
    assert response.status_code == 200
    body = response.json()
    assert body["unknown_meters"] > 0
    states = {s["state"] for s in body["classified_segments"]}
    assert "unknown" in states or "mixed" in states


async def test_night_departure_reports_sun_below_horizon_without_percentages(client):
    response = await client.post("/api/v1/environment/shade/analyze", json={
        "departure_at": datetime(2026, 7, 1, 1, 30, tzinfo=MSK).isoformat(),
        "legs": [_ns_leg(-60.0, 260.0, 600.0)],
    })
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "sun_below_horizon"
    assert body["shade_fraction"] is None
    assert body["estimated_shaded_meters"] in (0.0, None)
    assert body["estimated_exposed_meters"] in (0.0, None)
    assert "ниже горизонта" in body["reason"]


async def test_weather_context_never_changes_the_estimate_only_relevance(client):
    """Cloud cover must not change the geometry — only the relevance label."""
    payload_clear = {"departure_at": DEPARTURE.isoformat(),
                     "legs": [_ns_leg(-60.0, 260.0, 300.0)],
                     "weather": {"cloud_cover_fraction": 0.05, "source": "test"}}
    payload_overcast = {**payload_clear, "weather": {"cloud_cover_fraction": 0.95, "source": "test"}}
    clear = (await client.post("/api/v1/environment/shade/analyze", json=payload_clear)).json()
    overcast = (await client.post("/api/v1/environment/shade/analyze", json=payload_overcast)).json()
    assert clear["estimated_shaded_meters"] == overcast["estimated_shaded_meters"]
    assert clear["weather_relevance"] == "high"
    assert overcast["weather_relevance"] == "low"


async def test_unavailable_without_verified_data_never_fabricates_percentages():
    """An unusable provider must produce status=unavailable and no numbers."""
    from app.services.shade_engine.buildings import EmptyBuildingProvider

    app.dependency_overrides[get_shade_engine] = lambda: TimeAwareShadeEngine(
        building_provider=EmptyBuildingProvider())
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.post("/api/v1/environment/shade/analyze", json={
                "departure_at": DEPARTURE.isoformat(),
                "legs": [_ns_leg(-60.0, 260.0, 300.0)],
            })
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "unavailable"
        assert body["shade_fraction"] is None
        assert body["estimated_shaded_meters"] is None
        assert "недоступ" in body["reason"] or "недоступ" in body["dataset_provenance"]
    finally:
        app.dependency_overrides.clear()


async def test_no_public_building_geometry_upload_surface(client):
    """§26: the request schema has no field that could carry building geometry."""
    from app.schemas.shade import ShadeAnalyzeRequest

    fields = set(ShadeAnalyzeRequest.model_fields)
    assert fields == {"departure_at", "legs", "include_segments", "weather"}
    leg_fields = set(ShadeAnalyzeRequest.model_fields["legs"].annotation.__args__[0]
                     .model_fields) if hasattr(
                         ShadeAnalyzeRequest.model_fields["legs"].annotation, "__args__") else None
    from app.schemas.shade import ShadeWalkLegIn
    assert set(ShadeWalkLegIn.model_fields) == {
        "geometry", "walking_duration_seconds", "visit_duration_seconds_after_leg"}

async def test_client_contract_fixture_matches_the_live_response():
    """C7.1.1: the Flutter fixture IS the real response — drift fails here.

    The Dart test parses `frontend/test/fixtures/c7_1_1_shade_response.json`
    with the production wiring; regenerating it must reproduce the committed
    bytes, so a backend change that alters the contract cannot leave the
    client-side fixture stale.
    """
    import json

    from scripts.extract_c7_1_1_response_fixture import OUT, _fetch

    committed = json.loads(OUT.read_text(encoding="utf-8"))
    live = await _fetch()
    assert committed["response"] == live, (
        "fixture разошёлся с реальным ответом: перегенерируйте его "
        "(python -m scripts.extract_c7_1_1_response_fixture)")
