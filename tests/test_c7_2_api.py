"""C7.2 API contract: POST /api/v1/environment/puddle/analyze.

The endpoint's honesty rules are asserted at the HTTP boundary:

* a request the engine cannot process honestly is refused (422) or refused
  for size by the middleware (413) — it is never "fixed" silently;
* an unavailable dataset / weather / model returns 200 with
  ``status: "unavailable"``, ``risk_level: "unknown"`` and NO numbers;
* the response lengths the client verifies (known+unknown=total,
  low+moderate+high=known, coverage=known/total) are EXACTLY consistent
  after the 0.1 m rounding;
* the weather dependency is injected, so these tests are offline.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.api.v1.endpoints.puddle import (
    get_puddle_engine,
    get_puddle_weather_provider,
)
from app.main import app
from app.services.puddle_engine.engine import PuddleRiskEngine
from app.services.puddle_engine.weather import WeatherUnavailable
from tests.puddle_fixtures import (
    PILOT_BBOX,
    SyntheticWaysProvider,
    latlon,
    series,
    way,
)

import datetime as dt

MSK = dt.timezone(dt.timedelta(hours=3))
DEPARTURE = dt.datetime(2026, 6, 1, 14, 0, tzinfo=MSK)  # == 11:00 UTC

ASPHALT = way(1, 0.0, surface="asphalt")
STONES = way(2, 0.0, surface="paving_stones")
BARE = way(3, 0.0)


def _leg(way_geometry, seconds=300.0, visit=0.0):
    return {
        "geometry": [{"latitude": lat, "longitude": lon}
                     for lat, lon in way_geometry],
        "walking_duration_seconds": seconds,
        "visit_duration_seconds_after_leg": visit,
    }


class _FakeWeatherProvider:
    def __init__(self, result=None, error: str | None = None):
        self.result = result
        self.error = error
        self.calls = 0

    async def fetch(self):
        self.calls += 1
        if self.error is not None:
            raise WeatherUnavailable(self.error)
        return self.result


class _SlowEngine:
    """Duck-typed engine whose analysis outlives the endpoint timeout."""

    def analyze(self, *args, **kwargs):
        import time
        time.sleep(1.5)
        raise AssertionError("never reached")

    def dataset_version(self):
        return "slow"

    def provider_provenance(self):
        return {"available": True}


def _app_client(engine, weather) -> AsyncClient:
    app.dependency_overrides[get_puddle_engine] = lambda: engine
    app.dependency_overrides[get_puddle_weather_provider] = lambda: weather
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture()
async def wet_client():
    """Weather available, synthetic pilot ways, clock pinned to the departure."""
    engine = PuddleRiskEngine(ways_provider=SyntheticWaysProvider((ASPHALT,)),
                              clock=lambda: DEPARTURE.astimezone(dt.timezone.utc))
    weather = _FakeWeatherProvider(series(
        DEPARTURE.astimezone(dt.timezone.utc),
        rain=lambda age: 1.5 if 0 <= age < 2 else 0.0))
    async with _app_client(engine, weather) as client:
        yield client
    app.dependency_overrides.clear()


async def test_naive_departure_is_rejected(wet_client):
    response = await wet_client.post("/api/v1/environment/puddle/analyze", json={
        "departure_at": "2026-06-01T14:00:00",  # no timezone
        "legs": [_leg(ASPHALT.geometry)],
    })
    assert response.status_code == 422


async def test_degenerate_route_is_rejected(wet_client):
    lat, lon = latlon(0.0, 0.0)
    response = await wet_client.post("/api/v1/environment/puddle/analyze", json={
        "departure_at": DEPARTURE.isoformat(),
        "legs": [{"geometry": [{"latitude": lat, "longitude": lon},
                               {"latitude": lat, "longitude": lon}],
                  "walking_duration_seconds": 100.0}],
    })
    assert response.status_code == 422
    assert "вырожден" in response.json()["detail"]


async def test_route_outside_spb_is_rejected(wet_client):
    response = await wet_client.post("/api/v1/environment/puddle/analyze", json={
        "departure_at": DEPARTURE.isoformat(),
        "legs": [{"geometry": [{"latitude": 55.75, "longitude": 37.61},
                               {"latitude": 55.76, "longitude": 37.62}],
                  "walking_duration_seconds": 100.0}],
    })
    assert response.status_code == 422
    assert "вне зоны" in response.json()["detail"]


async def test_route_longer_than_limit_is_rejected(wet_client):
    # ~28 km east-west along the SPb coverage band, inside the sane duration.
    response = await wet_client.post("/api/v1/environment/puddle/analyze", json={
        "departure_at": DEPARTURE.isoformat(),
        "legs": [{"geometry": [{"latitude": 59.9, "longitude": 30.0},
                               {"latitude": 59.9, "longitude": 30.5}],
                  "walking_duration_seconds": 20000.0}],
    })
    assert response.status_code == 422
    assert "длиннее лимита" in response.json()["detail"]


async def test_oversize_body_is_rejected_by_the_request_cap():
    engine = PuddleRiskEngine(ways_provider=SyntheticWaysProvider(),
                              clock=lambda: DEPARTURE)
    async with _app_client(engine, _FakeWeatherProvider(error="unused")) as client:
        points = [{"latitude": 59.94 + index * 1e-6, "longitude": 30.31 + index * 1e-6}
                  for index in range(1500)]
        body = json.dumps({
            "departure_at": DEPARTURE.isoformat(),
            "legs": [{"geometry": points, "walking_duration_seconds": 900.0}],
        })
        assert len(body.encode()) > 16384
        response = await client.post("/api/v1/environment/puddle/analyze",
                                     content=body.encode(),
                                     headers={"Content-Type": "application/json"})
    app.dependency_overrides.clear()
    assert response.status_code == 413


async def test_wet_walk_returns_consistent_rounded_numbers(wet_client):
    response = await wet_client.post("/api/v1/environment/puddle/analyze", json={
        "departure_at": DEPARTURE.isoformat(),
        "legs": [_leg(ASPHALT.geometry)],
    })
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "available"
    assert body["estimate"] is True
    assert body["risk_level"] in {"low", "moderate", "high"}
    assert body["confidence"] == "low"
    assert body["risk_index"] is not None and 0.0 <= body["risk_index"] <= 1.0
    # The sums the client verifies must hold EXACTLY after rounding.
    assert body["known_meters"] + body["unknown_meters"] == pytest.approx(
        body["route_length_meters"], abs=1e-9)
    assert (body["low_meters"] + body["moderate_meters"]
            + body["high_meters"]) == pytest.approx(body["known_meters"], abs=1e-9)
    assert body["coverage_share"] == pytest.approx(
        body["known_meters"] / body["route_length_meters"], abs=1e-12)
    assert body["method"] == "experimental_precipitation_surface_index"
    assert body["method_version"] == "c7_2.4"
    assert body["dataset_version"] == "test_fixture"
    assert body["weather_provenance"]["available"] is True
    assert body["weather_provenance"]["retrieved_at"].endswith("+00:00")
    assert body["evaluated_period"]["timezone"]  # "MSK" style offset name
    assert "model_precipitation_history" in body["reasons"]
    assert "experimental_uncalibrated" in body["reasons"]
    assert body["limitations"]
    assert body["sensitivity"]["baseline"] == body["risk_level"]


async def test_no_segments_are_returned_unless_requested(wet_client):
    response = await wet_client.post("/api/v1/environment/puddle/analyze", json={
        "departure_at": DEPARTURE.isoformat(),
        "legs": [_leg(ASPHALT.geometry)],
    })
    assert response.json()["classified_segments"] is None

    response = await wet_client.post("/api/v1/environment/puddle/analyze", json={
        "departure_at": DEPARTURE.isoformat(),
        "legs": [_leg(ASPHALT.geometry)],
        "include_segments": True,
    })
    segments = response.json()["classified_segments"]
    assert segments and all(
        segment["state"] in {"low", "moderate", "high", "unknown"}
        for segment in segments)


async def test_ambiguous_parallels_are_reported_as_unknown():
    engine = PuddleRiskEngine(
        ways_provider=SyntheticWaysProvider((ASPHALT, STONES)),
        clock=lambda: DEPARTURE)
    weather = _FakeWeatherProvider(series(DEPARTURE,
                                          rain=lambda age: 1.0 if 0 <= age < 2 else 0.0))
    async with _app_client(engine, weather) as client:
        response = await client.post("/api/v1/environment/puddle/analyze", json={
            "departure_at": DEPARTURE.isoformat(),
            "legs": [_leg(ASPHALT.geometry)],
        })
    app.dependency_overrides.clear()
    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "unavailable"
    assert body["risk_level"] == "unknown"
    assert body["known_meters"] == 0.0
    assert body["unknown_meters"] == pytest.approx(body["route_length_meters"], abs=1e-9)
    assert body["risk_index"] is None
    assert "ambiguous_osm_way" in body["reasons"]


async def test_route_outside_the_pilot_box_is_outside_coverage():
    engine = PuddleRiskEngine(ways_provider=SyntheticWaysProvider((ASPHALT,)),
                              clock=lambda: DEPARTURE)
    weather = _FakeWeatherProvider(series(DEPARTURE))
    outside = ((latlon(2000.0, 2000.0)), (latlon(2100.0, 2000.0)))
    async with _app_client(engine, weather) as client:
        response = await client.post("/api/v1/environment/puddle/analyze", json={
            "departure_at": DEPARTURE.isoformat(),
            "legs": [_leg(outside)],
        })
    app.dependency_overrides.clear()
    body = response.json()
    assert body["status"] == "outside_coverage"
    assert body["risk_level"] == "unknown"
    assert body["known_meters"] == 0.0
    assert "outside_pilot" in body["reasons"]


async def test_unconfigured_weather_returns_unavailable_with_reason():
    engine = PuddleRiskEngine(ways_provider=SyntheticWaysProvider((ASPHALT,)),
                              clock=lambda: DEPARTURE)
    weather = _FakeWeatherProvider(error="weather_not_configured")
    async with _app_client(engine, weather) as client:
        response = await client.post("/api/v1/environment/puddle/analyze", json={
            "departure_at": DEPARTURE.isoformat(),
            "legs": [_leg(ASPHALT.geometry)],
        })
    app.dependency_overrides.clear()
    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "unavailable"
    assert body["risk_level"] == "unknown"
    assert body["known_meters"] == 0.0
    assert body["reasons"] == ["weather_not_configured"]
    assert body["weather_provenance"]["available"] is False
    assert body["weather_provenance"]["reason"] == "weather_not_configured"


async def test_snow_or_freezing_returns_unavailable_not_low():
    engine = PuddleRiskEngine(ways_provider=SyntheticWaysProvider((ASPHALT,)),
                              clock=lambda: DEPARTURE)
    weather = _FakeWeatherProvider(series(DEPARTURE, temperature=-4.0))
    async with _app_client(engine, weather) as client:
        response = await client.post("/api/v1/environment/puddle/analyze", json={
            "departure_at": DEPARTURE.isoformat(),
            "legs": [_leg(ASPHALT.geometry)],
        })
    app.dependency_overrides.clear()
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["risk_level"] == "unknown"
    assert "weather_snow_or_freezing" in body["reasons"]


async def test_analysis_timeout_is_503_and_never_fake_numbers(monkeypatch):
    from app.api.v1.endpoints import puddle as endpoint
    monkeypatch.setattr(endpoint.settings, "PUDDLE_ANALYSIS_TIMEOUT_SECONDS", 0.2)
    weather = _FakeWeatherProvider(series(DEPARTURE))
    async with _app_client(_SlowEngine(), weather) as client:
        response = await client.post("/api/v1/environment/puddle/analyze", json={
            "departure_at": DEPARTURE.isoformat(),
            "legs": [_leg(ASPHALT.geometry)],
        })
    app.dependency_overrides.clear()
    assert response.status_code == 503
    assert "времени" in response.json()["detail"]
    assert "risk_level" not in response.json()


async def test_weather_timeout_degrades_to_unavailable(monkeypatch):
    from app.api.v1.endpoints import puddle as endpoint
    monkeypatch.setattr(endpoint.settings, "PUDDLE_WEATHER_TIMEOUT_SECONDS", 0.1)

    class _HangingWeather:
        async def fetch(self):
            await asyncio.sleep(2.0)
            raise AssertionError("never reached")

    engine = PuddleRiskEngine(ways_provider=SyntheticWaysProvider((ASPHALT,)),
                              clock=lambda: DEPARTURE)
    async with _app_client(engine, _HangingWeather()) as client:
        response = await client.post("/api/v1/environment/puddle/analyze", json={
            "departure_at": DEPARTURE.isoformat(),
            "legs": [_leg(ASPHALT.geometry)],
        })
    app.dependency_overrides.clear()
    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "unavailable"
    assert body["reasons"] == ["weather_unavailable"]


def _open_meteo_payload(anchor: dt.datetime) -> dict:
    """Hourly bins (bin end = ``time``) from anchor-48h to anchor+12h, dry
    except 1.5 mm/h in the two hours before the anchor."""
    offsets = list(range(-48, 13))
    return {
        "utc_offset_seconds": 0,
        "hourly_units": {"time": "unixtime", "precipitation": "mm",
                         "snowfall": "cm", "temperature_2m": "°C"},
        "hourly": {
            "time": [int((anchor + dt.timedelta(hours=offset)).timestamp())
                     for offset in offsets],
            "precipitation": [1.5 if offset in (-1, -2) else 0.0
                              for offset in offsets],
            "temperature_2m": [12.0] * len(offsets),
            "snowfall": [0.0] * len(offsets),
        },
    }


async def test_one_weather_fetch_is_shared_by_consecutive_requests(monkeypatch):
    """Review C7.2.1 #1: the 15-minute cache must outlive a single request.

    This exercises the PRODUCTION wiring end to end — only the HTTP transport
    is substituted. The dependency is resolved on EVERY request, so three
    consecutive analyses must produce exactly one upstream fetch; crossing the
    validity window must produce exactly one more."""
    from app.api.v1.endpoints import puddle as endpoint
    from app.services.puddle_engine import wiring
    from app.services.puddle_engine.weather import OpenMeteoWeatherProvider

    upstream_calls = []
    now = {"value": DEPARTURE.astimezone(dt.timezone.utc)}

    def handler(request: httpx.Request) -> httpx.Response:
        upstream_calls.append(request)
        return httpx.Response(200, json=_open_meteo_payload(now["value"]))

    monkeypatch.setattr(
        wiring, "_build_weather_provider",
        lambda latitude, longitude: OpenMeteoWeatherProvider(
            latitude=latitude, longitude=longitude,
            clock=lambda: now["value"],
            transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(endpoint.settings, "PUDDLE_WEATHER_ENABLED", True)
    wiring.clear_provider_cache()

    engine = PuddleRiskEngine(ways_provider=SyntheticWaysProvider((ASPHALT,)),
                              clock=lambda: now["value"])
    app.dependency_overrides[get_puddle_engine] = lambda: engine
    body_request = {
        "departure_at": DEPARTURE.isoformat(),
        "legs": [_leg(ASPHALT.geometry)],
    }
    try:
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as client:
            for _ in range(3):
                response = await client.post(
                    "/api/v1/environment/puddle/analyze", json=body_request)
                assert response.status_code == 200
                assert response.json()["status"] == "available"
            assert len(upstream_calls) == 1

            now["value"] += dt.timedelta(minutes=16)
            response = await client.post(
                "/api/v1/environment/puddle/analyze", json=body_request)
            assert response.status_code == 200
            assert response.json()["status"] == "available"
            assert len(upstream_calls) == 2
    finally:
        app.dependency_overrides.pop(get_puddle_engine, None)
        wiring.clear_provider_cache()


async def test_incomplete_precipitation_history_yields_no_numbers():
    """Review C7.2.1 #2 at the HTTP boundary: a lookback window with missing
    hours must stay unknown — never a numeric estimate that could understate
    the risk."""
    engine = PuddleRiskEngine(ways_provider=SyntheticWaysProvider((ASPHALT,)),
                              clock=lambda: DEPARTURE)
    weather = _FakeWeatherProvider(series(
        DEPARTURE, hours_before=6,
        rain=lambda age: 1.0 if 0 <= age < 2 else 0.0))
    async with _app_client(engine, weather) as client:
        response = await client.post("/api/v1/environment/puddle/analyze", json={
            "departure_at": DEPARTURE.isoformat(),
            "legs": [_leg(ASPHALT.geometry)],
        })
    app.dependency_overrides.clear()
    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "unavailable"
    assert body["risk_level"] == "unknown"
    assert body["known_meters"] == 0.0
    assert body["risk_index"] is None
    assert body["sensitivity"] == {}
    assert "weather_incomplete" in body["reasons"]


async def test_pilot_bbox_covers_the_declared_test_area():
    # The fixture provider declares the REAL pilot box; a route inside it must
    # never be misreported as outside coverage.
    assert PILOT_BBOX[0] < 59.9405 < PILOT_BBOX[2]
    assert PILOT_BBOX[1] < 30.3165 < PILOT_BBOX[3]
