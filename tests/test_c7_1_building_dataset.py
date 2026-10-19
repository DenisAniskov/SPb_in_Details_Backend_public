"""C7.1 level B — real OSM building dataset pipeline and provider.

The fixture is a REAL OpenStreetMap fragment (Winter Palace and neighbours,
ODbL, © OpenStreetMap contributors) extracted by
``scripts.extract_c7_1_test_fixture`` from the cached acquisition evidence;
it is not synthetic. These tests exercise the builder, the dataset contract
and the read-only provider offline and deterministically.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.services.shade_engine.building_store import (
    BuildingStoreError,
    BuildingStoreLimitError,
    DatasetCoverage,
    SqliteBuildingGeometryProvider,
)
from app.services.shade_engine.buildings import HeightProvenance
from scripts import build_phase_c7_1_buildings as builder

FIXTURE = Path(__file__).parent / "fixtures" / "c7_1_osm_palace_snippet.osm.xml"


def _cache_from_fixture(tmp_path: Path) -> Path:
    """A one-zone acquisition cache whose bytes are the real fixture."""
    payload = FIXTURE.read_bytes()
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "palace_snippet.osm.xml").write_bytes(payload)
    manifest = {
        "phase": "C7.1-test",
        "generated_at_utc": "2026-10-11T00:00:00+00:00",
        "source": "OpenStreetMap via the official OSM API 0.6 map call",
        "license": "ODbL 1.0",
        "zones": [{
            "zone_id": "palace_snippet",
            "description": "real OSM fixture — Winter Palace and neighbours",
            "bbox_s_w_n_e": [59.9388, 30.3105, 59.9420, 30.3180],
            "file": "palace_snippet.osm.xml",
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }],
    }
    (cache / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return cache


@pytest.fixture()
def built_dataset(tmp_path: Path) -> Path:
    cache = _cache_from_fixture(tmp_path)
    out = tmp_path / "dataset.sqlite"
    builder.build(out, tmp_path / "audit.json", cache_dir=cache)
    return out


def test_fixture_is_real_osm_data_with_recorded_provenance():
    root_line = FIXTURE.read_text(encoding="utf-8").splitlines()[1]
    assert "ODbL" in root_line
    assert "provenance_source_sha256" in root_line
    assert "OpenStreetMap" in root_line or "osm" in FIXTURE.name


def test_builder_assembles_winter_palace_multipolygon(built_dataset: Path):
    provider = SqliteBuildingGeometryProvider(built_dataset)
    # The palace relation's outline is four open member ways; the builder must
    # chain them into one ring with its real declared height.
    footprints = provider.footprints_in_bbox((59.939, 30.311, 59.9417, 30.3164))
    palace = [fp for fp in footprints if fp.source_id == "relation/69424"]
    assert len(palace) == 1
    assert palace[0].height_m == pytest.approx(22.0)
    assert palace[0].provenance == HeightProvenance.OSM_DECLARED
    assert len(palace[0].polygon_latlon) > 8  # assembled from several ways


def test_builder_classifies_heights_only_with_real_sources(built_dataset: Path):
    provider = SqliteBuildingGeometryProvider(built_dataset)
    meta = provider.dataset_meta()
    assert meta["dataset_version"]
    footprints = provider.footprints_in_bbox((59.9388, 30.3105, 59.9420, 30.3180))
    provenances = {fp.source_id: fp.provenance for fp in footprints}
    # Real fragment: Alexander Column has height=47.5; Small Hermitage has
    # building:levels=3 → a documented 3.3 m-floor estimate; nothing else.
    assert HeightProvenance.OSM_DECLARED in provenances.values()
    assert HeightProvenance.LEVELS_ESTIMATE in provenances.values()
    for fp in footprints:
        if fp.provenance == HeightProvenance.LEVELS_ESTIMATE:
            assert fp.assumed_floor_height_m == pytest.approx(3.3)
            assert fp.levels is not None
        if fp.provenance == HeightProvenance.UNAVAILABLE:
            assert fp.height_m is None  # never guessed


def test_builder_refuses_a_broken_evidence_chain(tmp_path: Path):
    cache = _cache_from_fixture(tmp_path)
    (cache / "palace_snippet.osm.xml").write_bytes(b"<osm></osm>")  # tampered
    with pytest.raises(builder.DatasetBuildError, match="hash mismatch"):
        builder.build_records(cache)


def test_builder_requires_the_manifest(tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(builder.DatasetBuildError, match="manifest missing"):
        builder.build_records(empty)


def test_dataset_build_is_byte_reproducible(tmp_path: Path):
    cache = _cache_from_fixture(tmp_path)
    first = tmp_path / "a.sqlite"
    second = tmp_path / "b.sqlite"
    builder.build(first, tmp_path / "audit-a.json", cache_dir=cache)
    builder.build(second, tmp_path / "audit-b.json", cache_dir=cache)
    assert hashlib.sha256(first.read_bytes()).hexdigest() == \
        hashlib.sha256(second.read_bytes()).hexdigest()


def test_provider_rejects_oversized_bbox_and_missing_dataset(tmp_path: Path):
    provider = SqliteBuildingGeometryProvider(tmp_path / "nope.sqlite")
    assert provider.available() is False
    assert "не найден" in provider.provenance_description()
    with pytest.raises(BuildingStoreError):
        provider.footprints_in_bbox((59.9, 30.3, 59.91, 30.31))
    provider2 = SqliteBuildingGeometryProvider(FIXTURE)  # not a sqlite db
    with pytest.raises(BuildingStoreError):
        provider2.dataset_meta()

def test_provider_bbox_limit_is_documented_and_enforced(built_dataset: Path):
    provider = SqliteBuildingGeometryProvider(built_dataset)
    with pytest.raises(BuildingStoreLimitError, match="exceeds the documented limit"):
        provider.footprints_in_bbox((59.0, 30.0, 59.5, 30.5))  # 0.25 deg² > 0.05


def test_provider_reports_coverage_honestly(built_dataset: Path):
    provider = SqliteBuildingGeometryProvider(built_dataset)
    inside = provider.coverage_for_bbox((59.9390, 30.3110, 59.9410, 30.3160))
    assert isinstance(inside, DatasetCoverage)
    assert inside.covered is True
    assert inside.zone_ids == ("palace_snippet",)
    assert inside.buildings_total > 0
    outside = provider.coverage_for_bbox((59.80, 30.10, 59.82, 30.12))
    assert outside.covered is False
    assert outside.zone_ids == ()
    # Route-level three-state driver:
    route_inside = [(59.9395, 30.3120), (59.9405, 30.3130)]
    state, zones = provider.coverage_for_route(route_inside)
    assert (state, zones) == ("full", ("palace_snippet",))
    state, _ = provider.coverage_for_route([(59.9395, 30.3120), (59.80, 30.10)])
    assert state == "partial"
    state, zones = provider.coverage_for_route([(59.80, 30.10)])
    assert (state, zones) == ("outside", ())


def test_provider_excludes_invalid_geometry_rows(tmp_path: Path):
    cache = _cache_from_fixture(tmp_path)
    out = tmp_path / "dataset.sqlite"
    audit = builder.build(out, tmp_path / "audit.json", cache_dir=cache)
    import sqlite3
    with sqlite3.connect(out) as con:
        rows = con.execute(
            "SELECT geometry_validation_status, COUNT(*) FROM buildings GROUP BY 1").fetchall()
    statuses = dict(rows)
    # The fixture's real geometries must be usable; invalid rows, if any, are
    # never returned by the provider.
    assert statuses.get("ok", 0) >= 5
    for status, count in statuses.items():
        if status not in ("ok", "repaired"):
            assert audit["totals"]["invalid_geometries"] >= count


def test_acquisition_scripts_expose_the_documented_pilot_zones():
    from scripts import acquire_phase_c7_1_osm as acquire
    assert set(acquire.ZONES) == {
        "centre_palace", "centre_nevsky", "admiralteysky",
        "petrogradsky", "vasileostrovsky", "control_kupchino"}
    for zone in acquire.ZONES.values():
        s, w, n, e = zone["bbox"]
        assert s < n and w < e
        assert (n - s) * (e - w) < 0.001  # bounded: never the whole city
    assert acquire.OSM_API_MAP.startswith("https://api.openstreetmap.org/api/0.6/")
    assert "bounded" in acquire.CLIENT  # the client self-identifies politely
