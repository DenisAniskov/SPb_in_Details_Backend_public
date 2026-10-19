"""Real C7.2 evidence integrity, lossless extraction and length-based audit."""
from __future__ import annotations

import hashlib
import json

import pytest

from scripts import acquire_phase_c7_2_osm as acquire
from scripts import build_phase_c7_2_ways as builder
from scripts import probe_phase_c7_2_weather as weather


def test_real_pilot_is_byte_reproducible_and_checksum_pinned():
    first, audit = builder.build()
    second, second_audit = builder.build()
    assert first == second == builder.OUTPUT.read_bytes()
    assert audit == second_audit == builder.AUDIT.read_bytes()
    assert json.loads(audit)["dataset_sha256"] == hashlib.sha256(first).hexdigest()


def test_real_ways_have_identity_version_timestamp_and_original_geometry():
    manifest, payload = acquire.verified_payload()
    records = builder.extract_ways(manifest["source_format"], payload)
    dataset = json.loads(builder.OUTPUT.read_bytes())
    assert dataset["ways"] == records
    assert dataset["source"]["raw_sha256"] == hashlib.sha256(payload).hexdigest()
    assert len(records) == 1094
    assert len({row["osm_id"] for row in records}) == len(records)
    assert all(row["osm_version"] >= 1 and row["osm_timestamp"].endswith("Z") for row in records)
    assert any("surface" not in row["tags"] for row in records)
    assert any(row["tags"].get("covered") == "yes" for row in records)


def test_hash_tampering_fails_without_replacing_data():
    manifest = json.loads(acquire.MANIFEST.read_bytes())
    with pytest.raises(ValueError, match="payload checksum mismatch"):
        acquire.verified_payload({**manifest, "raw_sha256": "0" * 64})


def test_raw_xml_extraction_never_defaults_missing_surface_or_simplifies():
    payload = b'''<osm><node id="1" lat="59.94" lon="30.31"/>
      <node id="2" lat="59.94001" lon="30.311"/>
      <node id="3" lat="59.94" lon="30.312"/>
      <way id="42" version="3" timestamp="2026-10-01T00:00:00Z">
      <nd ref="1"/><nd ref="2"/><nd ref="3"/>
      <tag k="highway" v="footway"/><tag k="covered" v="yes"/></way></osm>'''
    result = builder.extract_ways("osm_xml", payload)
    assert result == [{"osm_id": 42, "osm_version": 3,
                       "osm_timestamp": "2026-10-01T00:00:00Z",
                       "tags": {"highway": "footway", "covered": "yes"},
                       "geometry": [[30.31, 59.94], [30.311, 59.94001], [30.312, 59.94]]}]


def test_incomplete_source_nodes_and_overpass_remark_fail_closed():
    with pytest.raises(KeyError):
        builder.extract_ways("osm_xml", b'''<osm><way id="1" version="1"
          timestamp="2026-10-01T00:00:00Z"><nd ref="999"/>
          <tag k="highway" v="footway"/></way></osm>''')
    with pytest.raises(ValueError, match="Incomplete Overpass"):
        builder.extract_ways("overpass_json", b'{"remark":"runtime timeout","elements":[]}')


def test_clipped_length_is_independent_of_vertex_density_and_pilot_exterior():
    bbox = [30.31, 59.93, 30.32, 59.95]
    sparse = [[30.30, 59.94], [30.33, 59.94]]
    dense = [[30.30 + i * 0.0001, 59.94] for i in range(301)]
    length = builder.clipped_length(sparse, bbox)
    assert 550 < length < 560
    assert builder.clipped_length(dense, bbox) == pytest.approx(length, abs=0.001)
    assert builder.clipped_length([[30.30, 59.96], [30.33, 59.96]], bbox) == 0


def test_real_coverage_audit_keeps_missing_tags_explicit_and_not_route_coverage():
    audit = json.loads(builder.AUDIT.read_bytes())
    walking = audit["groups"]["pedestrian_highway_types"]
    assert walking["way_count"] == 535
    for tag in walking["tags"].values():
        assert tag["length_with_tag_meters"] + tag["length_without_tag_meters"] == pytest.approx(
            walking["length_meters"], abs=0.002)
    assert walking["tags"]["surface"]["length_without_tag_meters"] > 1800
    assert walking["tags"]["smoothness"]["length_without_tag_meters"] > 19000
    assert "not_unique_network_or_route_coverage" in audit["length_basis"]


def test_real_weather_probe_has_checksum_and_hour_ending_model_bins():
    metadata = json.loads(weather.META.read_bytes())
    payload = weather.RAW.read_bytes()
    assert hashlib.sha256(payload).hexdigest() == metadata["raw_sha256"]
    data = weather.validate(payload)
    assert len(data["hourly"]["time"]) == metadata["hour_count"]
    assert metadata["precipitation_interval"] == "(timestamp-3600,timestamp]"
    assert metadata["observed_at_utc"] is None
    assert metadata["model_issued_at_utc"] is None
    assert metadata["not_a_runtime_weather_cache"] is True
