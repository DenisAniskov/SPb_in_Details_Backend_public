"""C11 quiet endpoint contract.

The HTTP boundary must stay honest: no numbers for unavailable/outside data,
consistent lengths after rounding, bounded requests, typed refusals, and no
route geometry in logs or provenance.
"""
from __future__ import annotations

import contextlib
import json
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.api.v1.endpoints.quiet import get_quiet_dataset, get_quiet_engine
from app.main import app
from app.services.quiet_engine.source import QuietDataset
from tests.c11_quiet_fixtures import (
    BBOX,
    dataset as synthetic_dataset,
    line,
    quiet_dataset,
    way,
)

LAT = 59.94
ROOT = Path(__file__).resolve().parents[1]
REAL_DATA = ROOT / "data/quiet_spb_pilot.json.gz"
REAL_MANIFEST = ROOT / "data/quiet_spb_pilot.manifest.json"


def _leg(points):
    return {"geometry": [{"latitude": lat, "longitude": lon}
                         for lat, lon in points]}


def _body(points, *, assessed_at="2026-10-20T12:00:00+03:00", include_legs=False):
    return {
        "assessed_at": assessed_at,
        "legs": [_leg(points)],
        "include_legs": include_legs,
    }


@contextlib.asynccontextmanager
async def _client(dataset: QuietDataset, engine=None):
    app.dependency_overrides[get_quiet_dataset] = lambda: dataset
    if engine is not None:
        app.dependency_overrides[get_quiet_engine] = lambda: engine
    try:
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.clear()


def _real_dataset() -> QuietDataset:
    dataset = QuietDataset.load(REAL_DATA, REAL_MANIFEST)
    assert dataset.available, dataset.reason
    return dataset


@pytest.mark.asyncio
async def test_available_route_returns_relative_proxy_and_coverage():
    dataset = quiet_dataset([
        way(1, "primary", line(30.30, LAT, 30.36, LAT)),
        way(2, "residential", line(30.30, 59.96, 30.36, 59.96)),
    ])
    async with _client(dataset) as client:
        response = await client.post(
            "/api/v1/environment/quiet/analyze",
            json=_body([(LAT, 30.31), (LAT, 30.35)], include_legs=True))
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "available"
    assert data["estimate"] is True
    assert data["confidence"] == "low"
    assert 0.80 <= data["road_noise_exposure_index"] <= 0.90
    assert data["coverage_share"] == 1.0
    assert data["known_meters"] + data["unknown_meters"] == pytest.approx(
        data["route_length_meters"], abs=0.1)
    assert data["traffic_time_modeled"] is False
    assert data["temporal_model"] == "static_osm_network"
    assert data["source_provenance"]["license"] == "ODbL-1.0"
    assert data["limitations"]
    assert data["legs"][0]["road_noise_exposure_index"] is not None
    assert "дБ" in " ".join(data["limitations"])


@pytest.mark.asyncio
async def test_disabled_engine_returns_no_numbers_and_a_reason():
    async with _client(QuietDataset.unavailable("quiet_engine_disabled")) as client:
        response = await client.post(
            "/api/v1/environment/quiet/analyze",
            json=_body([(LAT, 30.31), (LAT, 30.35)]))
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "unavailable"
    assert data["estimate"] is False
    assert data["confidence"] == "unavailable"
    assert data["road_noise_exposure_index"] is None
    assert data["low_exposure_share"] is None
    assert "quiet_engine_disabled" in data["reasons"]
    assert data["known_meters"] == 0.0


@pytest.mark.asyncio
async def test_route_outside_the_pilot_is_honest_unknown_not_zero():
    dataset = quiet_dataset([way(1, "primary", line(30.30, LAT, 30.36, LAT))])
    async with _client(dataset) as client:
        response = await client.post(
            "/api/v1/environment/quiet/analyze",
            json=_body([(LAT, 30.40), (LAT, 30.41)]))
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "outside_coverage"
    assert data["road_noise_exposure_index"] is None
    assert data["coverage_share"] == 0.0
    assert data["known_meters"] == 0.0
    assert data["unknown_meters"] == pytest.approx(data["route_length_meters"], abs=0.1)
    assert "route_outside_pilot" in data["reasons"]


@pytest.mark.asyncio
async def test_boundary_crossing_is_partial_with_consistent_lengths():
    dataset = quiet_dataset([way(1, "primary", line(30.30, LAT, 30.36, LAT))])
    async with _client(dataset) as client:
        response = await client.post(
            "/api/v1/environment/quiet/analyze",
            json=_body([(LAT, BBOX[2] - 0.001), (LAT, BBOX[2] + 0.001)]))
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "partial"
    assert 0.0 < data["known_meters"] < data["route_length_meters"]
    assert data["known_meters"] + data["unknown_meters"] == pytest.approx(
        data["route_length_meters"], abs=0.1)
    assert data["coverage_share"] == pytest.approx(
        data["known_meters"] / data["route_length_meters"], abs=1e-6)


@pytest.mark.asyncio
async def test_route_outside_spb_and_oversized_or_degenerate_routes_are_422():
    dataset = quiet_dataset([way(1, "primary", line(30.30, LAT, 30.36, LAT))])
    async with _client(dataset) as client:
        outside_spb = await client.post(
            "/api/v1/environment/quiet/analyze",
            json=_body([(55.75, 37.61), (55.76, 37.62)]))
        too_long = await client.post(
            "/api/v1/environment/quiet/analyze",
            json=_body([(LAT, 30.0), (LAT, 30.5)]))
        degenerate = await client.post(
            "/api/v1/environment/quiet/analyze",
            json=_body([(LAT, 30.31), (LAT, 30.31)]))
    assert outside_spb.status_code == 422
    assert too_long.status_code == 422
    assert degenerate.status_code == 422


@pytest.mark.asyncio
async def test_naive_time_is_rejected_not_silently_assumed():
    dataset = quiet_dataset([way(1, "primary", line(30.30, LAT, 30.36, LAT))])
    async with _client(dataset) as client:
        response = await client.post(
            "/api/v1/environment/quiet/analyze",
            json=_body([(LAT, 30.31), (LAT, 30.35)], assessed_at="2026-10-20T12:00:00"))
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_oversized_request_is_refused_by_the_body_cap():
    dataset = quiet_dataset([way(1, "primary", line(30.30, LAT, 30.36, LAT))])
    points = [(LAT, 30.30 + index * 0.000001) for index in range(2000)]
    body = _body(points)
    async with _client(dataset) as client:
        response = await client.post(
            "/api/v1/environment/quiet/analyze", json=body)
    assert response.status_code == 413


@pytest.mark.asyncio
async def test_analysis_timeout_is_503_and_does_not_break_the_contract(monkeypatch):
    from app.core.config import settings

    class _SlowEngine:
        def analyze(self, legs):
            import time
            time.sleep(0.5)
            raise AssertionError("must not be published")

    dataset = quiet_dataset([way(1, "primary", line(30.30, LAT, 30.36, LAT))])
    monkeypatch.setattr(settings, "QUIET_ANALYSIS_TIMEOUT_SECONDS", 0.02)
    async with _client(dataset, engine=_SlowEngine()) as client:
        response = await client.post(
            "/api/v1/environment/quiet/analyze",
            json=_body([(LAT, 30.31), (LAT, 30.35)]))
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_real_pilot_produces_a_stable_nevsky_estimate():
    dataset = _real_dataset()
    async with _client(dataset) as client:
        response = await client.post(
            "/api/v1/environment/quiet/analyze",
            json=_body([(59.9340, 30.3350), (59.9346, 30.3400),
                        (59.9352, 30.3450)], include_legs=True))
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "available"
    assert data["dataset_version"] == "c11_quiet_spb_central_v1"
    assert data["dataset_sha256"] == dataset.dataset_sha256
    assert data["road_noise_exposure_index"] is not None
    assert 0.0 <= data["road_noise_exposure_index"] <= 1.0
    assert data["coverage_share"] == 1.0
    assert data["source_provenance"]["attribution"] == "© OpenStreetMap contributors"
    assert data["max_road_class_nearby"] in {
        "primary", "secondary", "tertiary", "residential"}
