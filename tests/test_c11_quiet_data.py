"""C11 geographic dataset validation on the real, pinned central-SPb pilot.

Synthetic fixtures never replace these checks: the tests assert actual OSM way
identities, tags, geometry bounds, licence and checksums from the shipped
snapshot.
"""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

from app.services.quiet_engine.model import MAX_INFLUENCE_METERS, ROAD_CLASS_WEIGHTS
from app.services.quiet_engine.source import QuietDataset

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/quiet_spb_pilot.json.gz"
MANIFEST = ROOT / "data/quiet_spb_pilot.manifest.json"


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _dataset() -> dict:
    return json.loads(gzip.decompress(DATA.read_bytes()))


def test_snapshot_checksums_and_provenance_match_the_manifest():
    manifest = _manifest()
    compressed = DATA.read_bytes()
    assert hashlib.sha256(compressed).hexdigest() == manifest["gzip_sha256"]
    data = gzip.decompress(compressed)
    assert len(data) == manifest["dataset_bytes"]
    assert hashlib.sha256(data).hexdigest() == manifest["dataset_sha256"]
    dataset = json.loads(data)
    assert dataset["license"] == "ODbL-1.0"
    assert dataset["attribution"] == "© OpenStreetMap contributors"
    assert dataset["source"]["url"].startswith("https://download.geofabrik.de/")
    assert dataset["source"]["raw_sha256"] == manifest["source_sha256"]
    assert dataset["source"]["osm_base_timestamp"] == manifest["osm_base_timestamp"]
    assert dataset["pilot_bbox"] == manifest["pilot_bbox"]
    assert manifest["way_count"] == len(dataset["ways"])


def test_real_pilot_contains_expected_central_streets_and_no_footways():
    dataset = _dataset()
    ways = dataset["ways"]
    assert len(ways) == 3638
    assert len({row["osm_id"] for row in ways}) == len(ways)
    classes = {row["tags"]["highway"] for row in ways}
    assert "footway" not in classes
    assert "path" not in classes
    assert "cycleway" not in classes
    assert classes <= set(ROAD_CLASS_WEIGHTS)
    names = {row["tags"].get("name") for row in ways}
    assert "Невский проспект" in names
    assert "Садовая улица" in names
    # The pilot is central: every source way touches the declared box.
    west, south, east, north = dataset["pilot_bbox"]
    for row in ways:
        assert any(west <= lon <= east and south <= lat <= north
                   for lon, lat in row["geometry"]), row["osm_id"]


def test_real_nevsky_way_has_primary_or_secondary_class_and_real_geometry():
    ways = _dataset()["ways"]
    nevsky = [row for row in ways
              if row["tags"].get("name") == "Невский проспект"]
    assert nevsky
    assert {row["tags"]["highway"] for row in nevsky} & {"primary", "secondary"}
    for row in nevsky:
        assert len(row["geometry"]) >= 2
        # A source way may cross the pilot boundary; at least one vertex is
        # inside and every vertex stays in the central-SPb acquisition area.
        assert any(30.28 <= lon <= 30.38 and 59.915 <= lat <= 59.965
                   for lon, lat in row["geometry"])
        for lon, lat in row["geometry"]:
            assert 30.25 <= lon <= 30.41
            assert 59.90 <= lat <= 59.98


def test_loaded_dataset_excludes_non_motor_access_and_reports_counts():
    dataset = QuietDataset.load(DATA, MANIFEST)
    assert dataset.available, dataset.reason
    assert dataset.dataset_version == "c11_quiet_spb_central_v1"
    assert dataset.dataset_sha256 == _manifest()["dataset_sha256"]
    assert len(dataset.roads) == 3595
    assert dataset.excluded_way_count == 43
    assert dataset.provenance["license"] == "ODbL-1.0"
    assert dataset.provenance["class_counts"]["primary"] == 678
    assert dataset.bbox == (30.28, 59.915, 30.38, 59.965)
    assert MAX_INFLUENCE_METERS == 120.0


def test_missing_or_mismatched_snapshot_is_never_partially_trusted(tmp_path):
    missing = QuietDataset.load(tmp_path / "missing.gz", tmp_path / "missing.json")
    assert missing.available is False
    assert missing.reason == "quiet_dataset_unavailable"
    bad_manifest = tmp_path / "manifest.json"
    bad_manifest.write_text(json.dumps({**_manifest(), "dataset_sha256": "0" * 64}),
                            encoding="utf-8")
    corrupted = QuietDataset.load(DATA, bad_manifest)
    assert corrupted.available is False
    assert corrupted.reason == "quiet_dataset_unavailable"
