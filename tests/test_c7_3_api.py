"""C7.3 API contract: POST /api/v1/environment/pollen/analyze.

The endpoint's honesty rules are asserted at the HTTP boundary:

* an unconfigured source returns 200 with ``status: "unavailable"`` and NO
  numbers — never archived or invented concentrations;
* a request the endpoint cannot process honestly is refused (422) or refused
  for size by the middleware (413) — it is never "fixed" silently;
* the response carries the source's units and attribution, and contains no
  threshold, risk-level or medical field at all;
* the response lengths the client verifies (known+unknown=total,
  coverage=known/total) are EXACTLY consistent after the 0.1 m rounding;
* the source dependency is injected, so these tests are offline — except the
  one production-wiring test, which uses a mocked transport to prove that a
  single upstream fetch serves consecutive requests.
"""

from __future__ import annotations

import contextlib
import datetime as dt

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.api.v1.endpoints.pollen import (
    get_pollen_engine,
    get_pollen_source,
)
from app.main import app
from app.services.pollen_engine.engine import PollenEngine
from app.services.pollen_engine.source import (
    UNIT,
    PollenUnavailable,
    PollenSeries,
)
from tests.pollen_fixtures import (
    ORIGIN_LAT,
    ORIGIN_LON,
    constant,
    hour_floor,
    latlon,
    series,
    straight,
)

MSK = dt.timezone(dt.timedelta(hours=3))
DEPARTURE = dt.datetime(2026, 7, 17, 11, 0, tzinfo=MSK)  # == 08:00 UTC
DEPARTURE_UTC = DEPARTURE.astimezone(dt.timezone.utc)


def _leg(geometry, seconds=300.0, visit=0.0):
    return {
        "geometry": [{"latitude": lat, "longitude": lon}
                     for lat, lon in geometry],
        "walking_duration_seconds": seconds,
        "visit_duration_seconds_after_leg": visit,
    }


class _FakePollenSource:
    def __init__(self, result: PollenSeries | None = None,
                 error: str | None = None):
        self.result = result
        self.error = error
        self.calls = 0

    async def fetch(self) -> PollenSeries:
        self.calls += 1
        if self.error is not None:
            raise PollenUnavailable(self.error)
        assert self.result is not None
        return self.result


class _SlowEngine:
    """Duck-typed engine whose analysis outlives the endpoint timeout."""

    def analyze(self, *args, **kwargs):
        import time
        time.sleep(1.5)
        raise AssertionError("never reached")


@contextlib.asynccontextmanager
async def _app_client(engine, source):
    """Inject the engine and the source, and REMOVE BOTH on the way out.

    Leaking an override here would silently serve a later test with a previous
    test's fake — including the production-wiring tests, which must reach the
    real provider factory.
    """
    app.dependency_overrides[get_pollen_engine] = lambda: engine
    app.dependency_overrides[get_pollen_source] = lambda: source
    try:
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.clear()


def _engine(**kwargs) -> PollenEngine:
    return PollenEngine(clock=lambda: DEPARTURE_UTC, **kwargs)


@pytest.fixture()
async def pollen_client():
    """A working source, a synthetic series and a pinned clock."""
    source = _FakePollenSource(series(
        DEPARTURE_UTC, hours_before=1, hours_after=6,
        concentration=lambda code, starts_at: (
            348.6 if code == "ragweed_pollen" else 0.0)))
    async with _app_client(_engine(), source) as client:
        yield client
    app.dependency_overrides.clear()


async def test_an_unconfigured_source_returns_unavailable_and_no_numbers():
    """The production default: no opt-in, no numbers, an explicit reason."""
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as client:
        response = await client.post("/api/v1/environment/pollen/analyze", json={
            "departure_at": DEPARTURE.isoformat(),
            "legs": [_leg(straight(100.0))],
        })
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["confidence"] == "unavailable"
    assert body["reasons"] == ["pollen_not_configured"]
    assert body["known_meters"] == 0.0
    assert body["unknown_meters"] == body["route_length_meters"]
    assert body["source_provenance"]["available"] is False
    assert body["source_provenance"]["reason"] == "pollen_not_configured"
    assert all(item["mean_concentration"] is None and item["status"] == "unknown"
               for item in body["species"])
    # No read source means no coverage to withhold: the client reads exactly
    # this to tell a false "no data" from a withheld complete panel (C7.3.1).
    assert all(item["known_share"] == 0.0 for item in body["species"])
    assert body["diagnostics"]["species_withheld"] == 0


async def test_the_endpoint_reports_concentrations_units_and_attribution(pollen_client):
    response = await pollen_client.post("/api/v1/environment/pollen/analyze", json={
        "departure_at": DEPARTURE.isoformat(),
        "legs": [_leg(straight(200.0), seconds=1800.0)],
    })
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "available"
    assert body["estimate"] is True
    assert body["method"] == "experimental_pollen_exposure"
    assert body["method_version"] == "c7_3.1"
    assert body["confidence"] == "low"
    assert body["concentration_unit"] == UNIT == "grains/m³"
    assert len(body["species"]) == 6
    names = {item["code"]: item["name"] for item in body["species"]}
    assert names["birch_pollen"] == "Берёза"
    assert names["ragweed_pollen"] == "Амброзия"
    ragweed = [item for item in body["species"]
               if item["code"] == "ragweed_pollen"][0]
    assert ragweed["status"] == "available"
    assert ragweed["unit"] == "grains/m³"
    assert ragweed["mean_concentration"] == pytest.approx(348.6)
    assert ragweed["peak_concentration"] == pytest.approx(348.6)
    assert ragweed["exposure_grains_hours_per_m3"] == pytest.approx(174.3)
    assert body["evaluated_period"]["start"] == DEPARTURE.isoformat()
    assert "+03:00" in body["evaluated_period"]["timezone"]
    provenance = body["source_provenance"]
    assert provenance["available"] is True
    assert provenance["model"] == "cams_europe"
    assert provenance["license"] == "CC-BY-4.0"
    assert "Open-Meteo" in provenance["attribution"]
    assert "CAMS" in provenance["attribution"]
    assert provenance["observed_at"] is None
    assert provenance["cell_bounds"] == [59.85, 30.25, 59.95, 30.35]


async def test_the_response_has_no_threshold_or_medical_field(pollen_client):
    """ТЗ C7.3 §4: concentrations, not danger categories."""
    response = await pollen_client.post("/api/v1/environment/pollen/analyze", json={
        "departure_at": DEPARTURE.isoformat(),
        "legs": [_leg(straight(200.0), seconds=1800.0)],
    })
    body = response.json()
    for forbidden in ("risk_level", "risk_index", "low_meters", "moderate_meters",
                      "high_meters", "allergy", "severity", "advice",
                      "recommendation"):
        assert forbidden not in body
    for item in body["species"]:
        assert item["status"] in {"available", "partial", "unknown"}
        for forbidden in ("risk_level", "risk_index", "threshold", "advice"):
            assert forbidden not in item
    assert body["assumptions"]
    assert body["limitations"]


async def test_unknown_hours_are_never_zero_filled_over_http():
    missing = DEPARTURE_UTC.replace(minute=0, second=0, microsecond=0) \
        + dt.timedelta(hours=1)
    source = _FakePollenSource(series(
        DEPARTURE_UTC, hours_before=1, hours_after=6, missing={missing},
        concentration=constant(20.0)))
    async with _app_client(_engine(), source) as client:
        response = await client.post("/api/v1/environment/pollen/analyze", json={
            "departure_at": DEPARTURE.isoformat(),
            # The visit pushes the second leg out of the missing bin's
            # predecessor and INTO the missing bin: half the walk is known.
            "legs": [_leg(straight(200.0), seconds=1800.0, visit=1800.0),
                     _leg(straight(200.0, x_from=400.0), seconds=1800.0)],
        })
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "partial"
    assert body["coverage_share"] == pytest.approx(0.5)
    assert "pollen_incomplete" in body["reasons"]
    item = body["species"][0]
    assert item["status"] == "partial"
    assert item["known_share"] == pytest.approx(0.5)
    # The unknown hour contributes nothing and is NOT read as clean air.
    assert item["mean_concentration"] == pytest.approx(20.0)
    assert item["mean_concentration"] != 0.0


async def test_legs_appear_only_when_requested(pollen_client):
    body_request = {
        "departure_at": DEPARTURE.isoformat(),
        "legs": [_leg(straight(200.0), seconds=1800.0, visit=600.0),
                 _leg(straight(200.0, x_from=400.0), seconds=1800.0)],
    }
    plain = await pollen_client.post("/api/v1/environment/pollen/analyze",
                                     json=body_request)
    assert plain.json()["legs"] is None

    detailed = await pollen_client.post("/api/v1/environment/pollen/analyze",
                                        json={**body_request, "include_legs": True})
    legs = detailed.json()["legs"]
    assert len(legs) == 2
    assert legs[0]["starts_at_seconds"] == pytest.approx(0.0)
    assert legs[0]["ends_at_seconds"] == pytest.approx(1800.0)
    # The POI visit shifts the second leg by exactly 600 s.
    assert legs[1]["starts_at_seconds"] == pytest.approx(2400.0)
    assert legs[1]["inside_source_cell"] is True
    assert len(legs[1]["species"]) == 6
    for leg in legs:
        assert leg["known_meters"] + leg["unknown_meters"] \
            == pytest.approx(leg["length_meters"], abs=0.15)


async def test_a_route_outside_the_spb_zone_is_rejected(pollen_client):
    response = await pollen_client.post("/api/v1/environment/pollen/analyze", json={
        "departure_at": DEPARTURE.isoformat(),
        "legs": [_leg(((55.75, 37.61), (55.751, 37.611)))],
    })
    assert response.status_code == 422
    assert "вне зоны оценки пыльцы" in response.json()["detail"]


async def test_a_too_long_walk_is_rejected(pollen_client):
    response = await pollen_client.post("/api/v1/environment/pollen/analyze", json={
        "departure_at": DEPARTURE.isoformat(),
        "legs": [_leg((latlon(-13_000.0, 0.0), latlon(13_000.0, 0.0)),
                      seconds=6 * 3600.0)],
    })
    assert response.status_code == 422
    assert "длиннее лимита" in response.json()["detail"]


async def test_a_degenerate_route_is_rejected(pollen_client):
    response = await pollen_client.post("/api/v1/environment/pollen/analyze", json={
        "departure_at": DEPARTURE.isoformat(),
        "legs": [_leg((latlon(0.0, 0.0), latlon(0.0, 0.0)))],
    })
    assert response.status_code == 422
    assert "вырожден" in response.json()["detail"]


@pytest.mark.parametrize("mutation, label", [
    ("naive", "departure without a timezone"),
    ("extra_field", "an unknown request field"),
    ("too_many_legs", "more legs than the contract allows"),
    ("no_legs", "no legs at all"),
    ("bad_geometry", "a single-point geometry"),
    ("long_walk", "walking time over the contract limit"),
])
async def test_a_malformed_request_is_rejected(pollen_client, mutation, label):
    body = {"departure_at": DEPARTURE.isoformat(),
            "legs": [_leg(straight(100.0))]}
    if mutation == "naive":
        body["departure_at"] = "2026-07-17T11:00:00"
    elif mutation == "extra_field":
        body["weather"] = {"rain": 3.0}
    elif mutation == "too_many_legs":
        body["legs"] = [_leg(straight(50.0))] * 25
    elif mutation == "no_legs":
        body["legs"] = []
    elif mutation == "bad_geometry":
        body["legs"] = [_leg((latlon(0.0, 0.0),))]
    else:
        body["legs"] = [_leg(straight(100.0), seconds=6 * 3600.0)] * 5
    response = await pollen_client.post("/api/v1/environment/pollen/analyze",
                                        json=body)
    assert response.status_code == 422, label


async def test_a_source_failure_degrades_to_unavailable_not_an_error():
    source = _FakePollenSource(error="pollen_source_invalid")
    async with _app_client(_engine(), source) as client:
        response = await client.post("/api/v1/environment/pollen/analyze", json={
            "departure_at": DEPARTURE.isoformat(),
            "legs": [_leg(straight(100.0))],
        })
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "unavailable"
    assert "pollen_source_invalid" in body["reasons"]
    assert body["known_meters"] == 0.0
    assert body["source_provenance"]["available"] is False


async def test_analysis_timeout_is_503_and_never_fake_numbers(monkeypatch):
    from app.api.v1.endpoints import pollen as endpoint

    monkeypatch.setattr(endpoint.settings, "POLLEN_ANALYSIS_TIMEOUT_SECONDS", 0.2)
    source = _FakePollenSource(series(DEPARTURE_UTC))
    async with _app_client(_SlowEngine(), source) as client:
        response = await client.post("/api/v1/environment/pollen/analyze", json={
            "departure_at": DEPARTURE.isoformat(),
            "legs": [_leg(straight(100.0))],
        })
    assert response.status_code == 503
    assert "слишком много времени" in response.json()["detail"]


async def test_the_source_fetch_timeout_degrades_to_unavailable(monkeypatch):
    import asyncio
    from app.api.v1.endpoints import pollen as endpoint

    class _HangingSource:
        async def fetch(self):
            await asyncio.sleep(5)

    monkeypatch.setattr(endpoint.settings, "POLLEN_SOURCE_TIMEOUT_SECONDS", 0.1)
    async with _app_client(_engine(), _HangingSource()) as client:
        response = await client.post("/api/v1/environment/pollen/analyze", json={
            "departure_at": DEPARTURE.isoformat(),
            "legs": [_leg(straight(100.0))],
        })
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "unavailable"
    assert "pollen_unavailable" in body["reasons"]


async def test_response_sums_stay_consistent_after_rounding():
    missing = DEPARTURE_UTC.replace(minute=0, second=0, microsecond=0) \
        + dt.timedelta(hours=1)
    source = _FakePollenSource(series(DEPARTURE_UTC, hours_before=1,
                                      hours_after=6, missing={missing},
                                      concentration=constant(4.0)))
    async with _app_client(_engine(), source) as client:
        response = await client.post("/api/v1/environment/pollen/analyze", json={
            "departure_at": DEPARTURE.isoformat(),
            "legs": [_leg(straight(1234.0), seconds=1800.0),
                     _leg(straight(567.0, x_from=-3000.0), seconds=1800.0)],
        })
    body = response.json()
    total, known, unknown = (body["route_length_meters"], body["known_meters"],
                             body["unknown_meters"])
    assert known + unknown == pytest.approx(total, abs=1e-9)
    assert body["coverage_share"] == pytest.approx(known / total, abs=1e-9)


def _open_meteo_payload(now: dt.datetime, *, hours: int = 6,
                        value: float = 12.0) -> dict:
    """A valid air-quality response in the real shape, for the wiring test."""
    from app.services.pollen_engine.source import SPECIES_CODES

    start = now.replace(minute=0, second=0, microsecond=0) - dt.timedelta(hours=1)
    times = [int((start + dt.timedelta(hours=index)).timestamp())
             for index in range(hours)]
    return {
        "latitude": 59.9, "longitude": 30.3, "utc_offset_seconds": 0,
        "hourly_units": {"time": "unixtime",
                         **{code: UNIT for code in SPECIES_CODES}},
        "hourly": {"time": times,
                   **{code: [value] * hours for code in SPECIES_CODES}},
    }


async def test_one_source_fetch_is_shared_by_consecutive_requests(monkeypatch):
    """The C7.2 defect must not repeat: the provider is a process singleton.

    Three HTTP requests through the PRODUCTION wiring (only the transport is
    mocked) must produce exactly one upstream fetch; a fourth request after
    the one-hour validity window produces exactly the second one.
    """
    from app.api.v1.endpoints import pollen as endpoint
    from app.services.pollen_engine import wiring
    from app.services.pollen_engine.source import OpenMeteoPollenProvider

    upstream_calls = []
    now = {"value": DEPARTURE_UTC}

    def handler(request: httpx.Request) -> httpx.Response:
        upstream_calls.append(request)
        return httpx.Response(200, json=_open_meteo_payload(now["value"]))

    monkeypatch.setattr(
        wiring, "_build_pollen_provider",
        lambda latitude, longitude: OpenMeteoPollenProvider(
            latitude=latitude, longitude=longitude,
            clock=lambda: now["value"],
            transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(endpoint.settings, "POLLEN_SOURCE_ENABLED", True)
    wiring.clear_provider_cache()
    app.dependency_overrides.clear()

    engine = _engine()
    app.dependency_overrides[get_pollen_engine] = lambda: engine
    body_request = {"departure_at": DEPARTURE.isoformat(),
                    "legs": [_leg(straight(100.0))]}
    try:
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as client:
            for _ in range(3):
                response = await client.post(
                    "/api/v1/environment/pollen/analyze", json=body_request)
                assert response.status_code == 200
                assert response.json()["status"] == "available"
            assert len(upstream_calls) == 1

            now["value"] += dt.timedelta(minutes=61)
            response = await client.post(
                "/api/v1/environment/pollen/analyze", json=body_request)
            assert response.status_code == 200
            assert response.json()["status"] == "available"
            assert len(upstream_calls) == 2
    finally:
        app.dependency_overrides.clear()
        wiring.clear_provider_cache()


async def test_the_production_request_never_carries_user_geometry(monkeypatch):
    """Only the fixed pilot centroid travels upstream."""
    from app.api.v1.endpoints import pollen as endpoint
    from app.services.pollen_engine import wiring
    from app.services.pollen_engine.source import OpenMeteoPollenProvider

    upstream_calls = []
    now = {"value": DEPARTURE_UTC}

    def handler(request: httpx.Request) -> httpx.Response:
        upstream_calls.append(request)
        return httpx.Response(200, json=_open_meteo_payload(now["value"]))

    monkeypatch.setattr(
        wiring, "_build_pollen_provider",
        lambda latitude, longitude: OpenMeteoPollenProvider(
            latitude=latitude, longitude=longitude,
            clock=lambda: now["value"],
            transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(endpoint.settings, "POLLEN_SOURCE_ENABLED", True)
    wiring.clear_provider_cache()
    app.dependency_overrides.clear()
    app.dependency_overrides[get_pollen_engine] = lambda: _engine()
    try:
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as client:
            response = await client.post(
                "/api/v1/environment/pollen/analyze",
                json={"departure_at": DEPARTURE.isoformat(),
                      "legs": [_leg((latlon(-800.0, 500.0),
                                     latlon(800.0, 500.0)), seconds=900.0)]})
            assert response.status_code == 200
        assert len(upstream_calls) == 1
        params = upstream_calls[0].url.params
        # Exactly one coordinate pair, and it is the fixed pilot centroid.
        assert params.get_list("latitude") == [str(ORIGIN_LAT)]
        assert params.get_list("longitude") == [str(ORIGIN_LON)]
        # The route's own geometry must appear nowhere in the request.
        upstream = str(upstream_calls[0].url)
        for point in (latlon(-800.0, 500.0), latlon(800.0, 500.0)):
            assert f"{point[0]}" not in upstream
            assert f"{point[1]}" not in upstream
    finally:
        app.dependency_overrides.clear()
        wiring.clear_provider_cache()


# --------------------------------------------------------------------------
# C7.3.1 — an unavailable answer withholds every number, per-species included,
# and the two no-numbers failures carry different reason codes.
# --------------------------------------------------------------------------


def _alternating_panel():
    """A two-hour panel no hour of which is complete.

    Hour 1 has ``alder_pollen`` and no ``birch_pollen``; hour 2 the other way
    round; the remaining four species are known in both hours.
    """
    first = hour_floor(DEPARTURE_UTC)

    def concentration(code: str, starts_at: dt.datetime):
        second_hour = starts_at == first + dt.timedelta(hours=1)
        if code == "alder_pollen":
            return None if second_hour else 10.0
        if code == "birch_pollen":
            return None if not second_hour else 20.0
        return 5.0
    return concentration


async def test_no_complete_panel_over_http_publishes_no_species_numbers():
    """ТЗ C7.3.1 (mandatory): two hours, alder in one and birch in the other.

    The walk has real values for five species per hour and no hour carries all
    six, so the answer is ``unavailable`` — and it must carry no number at
    all, not even for the species the source covered end to end. The same
    payload is checked against the Flutter model in
    ``frontend/test/pollen_analysis_model_test.dart``.
    """
    source = _FakePollenSource(series(
        DEPARTURE_UTC, hours_before=1, hours_after=6,
        concentration=_alternating_panel()))
    async with _app_client(_engine(), source) as client:
        response = await client.post(
            "/api/v1/environment/pollen/analyze",
            json={"departure_at": DEPARTURE.isoformat(),
                  "legs": [_leg(straight(800.0), seconds=3600.0),
                           _leg(straight(800.0, x_from=-1600.0), seconds=3600.0)],
                  "include_legs": True})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["confidence"] == "unavailable"
    assert body["known_meters"] == 0.0
    assert body["unknown_meters"] == body["route_length_meters"]
    assert body["coverage_share"] == 0.0
    assert body["walking_duration_seconds"] == pytest.approx(7200.0)
    assert "pollen_no_complete_panel" in body["reasons"]
    assert "pollen_incomplete" in body["reasons"]
    assert "pollen_no_data" not in body["reasons"]
    assert "modeled_pollen_concentration" not in body["reasons"]
    # No per-species number, and no per-leg block either.
    for item in body["species"]:
        assert item["status"] == "unknown"
        assert item["mean_concentration"] is None
        assert item["peak_concentration"] is None
        assert item["peak_at"] is None
        assert item["exposure_grains_hours_per_m3"] is None
    assert body["legs"] is None
    by_code = {item["code"]: item for item in body["species"]}
    # Withholding is not a claim that the source had nothing: the coverage the
    # source itself produced survives, so the client can say which species had
    # values instead of reporting "no data" for everything.
    assert by_code["alder_pollen"]["known_share"] == pytest.approx(0.5)
    assert by_code["birch_pollen"]["known_share"] == pytest.approx(0.5)
    assert by_code["grass_pollen"]["known_share"] == pytest.approx(1.0)
    assert body["diagnostics"]["species_withheld"] == 6
    assert body["diagnostics"]["species_partial"] == 2
    assert body["diagnostics"]["species_available"] == 4


async def test_a_complete_hour_keeps_its_numbers_over_http(pollen_client):
    """The other direction: the withholding must not reach good answers."""
    response = await pollen_client.post("/api/v1/environment/pollen/analyze", json={
        "departure_at": DEPARTURE.isoformat(),
        "legs": [_leg(straight(200.0), seconds=1800.0)],
    })
    body = response.json()
    assert body["status"] == "available"
    assert body["coverage_share"] == pytest.approx(1.0)
    assert "pollen_no_complete_panel" not in body["reasons"]
    assert "pollen_no_data" not in body["reasons"]
    assert body["diagnostics"]["species_withheld"] == 0
    assert all(item["mean_concentration"] is not None for item in body["species"])
