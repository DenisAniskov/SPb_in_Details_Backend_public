"""Deterministically build the unsimplified C7.2 pilot from hash-pinned OSM.

From backend: python -m scripts.build_phase_c7_2_ways [--check-reproducible]
No network, defaults for absent tags, nearest-road matching, or invented ways.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import xml.etree.ElementTree as ET

from scripts.acquire_phase_c7_2_osm import CACHE, ROOT, verified_payload

OUTPUT = ROOT / "data/puddle_ways_spb_pilot.json"
AUDIT = CACHE / "coverage_audit.json"


def canonical_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def extract_ways(source_format: str, payload: bytes) -> list[dict]:
    if source_format == "overpass_json":
        raw = json.loads(payload)
        if raw.get("remark"):
            raise ValueError("Incomplete Overpass response")
        records = [{"osm_id": element["id"], "osm_version": element["version"],
                    "osm_timestamp": element["timestamp"], "tags": element.get("tags", {}),
                    "geometry": [[point["lon"], point["lat"]]
                                 for point in element["geometry"]]}
                   for element in raw["elements"]
                   if element["type"] == "way" and "highway" in element.get("tags", {})]
    elif source_format == "osm_xml":
        raw_xml = ET.fromstring(payload)
        nodes = {node.attrib["id"]: [float(node.attrib["lon"]), float(node.attrib["lat"])]
                 for node in raw_xml.findall("node")}
        records = []
        for way in raw_xml.findall("way"):
            tags = {tag.attrib["k"]: tag.attrib["v"] for tag in way.findall("tag")}
            if "highway" not in tags:
                continue
            # Missing referenced nodes are an acquisition/build error, never a
            # reason to interpolate a new road or silently omit a segment.
            geometry = [nodes[node.attrib["ref"]] for node in way.findall("nd")]
            records.append({"osm_id": int(way.attrib["id"]),
                            "osm_version": int(way.attrib["version"]),
                            "osm_timestamp": way.attrib["timestamp"],
                            "tags": tags, "geometry": geometry})
    else:
        raise ValueError("Unsupported source format")
    if not records or len({row["osm_id"] for row in records}) != len(records):
        raise ValueError("Empty or duplicate source ways")
    for record in records:
        if len(record["geometry"]) < 2:
            raise ValueError(f"Degenerate source way {record['osm_id']}")
        for lon, lat in record["geometry"]:
            if not math.isfinite(lon) or not math.isfinite(lat) or not (-180 <= lon <= 180 and -90 <= lat <= 90):
                raise ValueError("Invalid source coordinate")
    return sorted(records, key=lambda row: row["osm_id"])


def clipped_length(geometry: list[list[float]], bbox: list[float]) -> float:
    """Length of actual segments clipped to bbox; no vertex-count weighting."""
    west, south, east, north = bbox
    total = 0.0
    for first, second in zip(geometry, geometry[1:]):
        low, high = 0.0, 1.0
        for start, delta, minimum, maximum in (
            (first[0], second[0] - first[0], west, east),
            (first[1], second[1] - first[1], south, north),
        ):
            if delta == 0:
                if start < minimum or start > maximum:
                    high = -1
                    break
                continue
            a, b = (minimum - start) / delta, (maximum - start) / delta
            low, high = max(low, min(a, b)), min(high, max(a, b))
        if high <= low:
            continue
        start = [a + low * (b - a) for a, b in zip(first, second)]
        end = [a + high * (b - a) for a, b in zip(first, second)]
        lat1, lat2 = math.radians(start[1]), math.radians(end[1])
        dlat, dlon = lat2 - lat1, math.radians(end[0] - start[0])
        h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        total += 6_371_000 * 2 * math.asin(min(1, math.sqrt(h)))
    return total


def build() -> tuple[bytes, bytes]:
    manifest, payload = verified_payload()
    ways = extract_ways(manifest["source_format"], payload)
    dataset = {key: manifest[key] for key in (
        "schema_version", "dataset_version", "retrieved_at_utc", "osm_base_timestamp",
        "pilot_bbox", "license", "license_url", "attribution")}
    dataset["source"] = {key: manifest[key] for key in (
        "endpoint", "source_format", "query_sha256", "raw_sha256")}
    dataset["ways"] = ways
    lengths = [(row, clipped_length(row["geometry"], manifest["pilot_bbox"])) for row in ways]
    walking = {"footway", "pedestrian", "path", "steps", "corridor", "living_street"}
    groups = {"all_highway_ways": lengths,
              "pedestrian_highway_types": [(row, length) for row, length in lengths
                                            if row["tags"]["highway"] in walking]}
    audit = {"dataset_version": manifest["dataset_version"], "pilot_bbox": manifest["pilot_bbox"],
             "length_basis": "sum_of_source_way_segments_clipped_to_bbox_not_unique_network_or_route_coverage",
             "groups": {}}
    for name, rows in groups.items():
        total = sum(length for _, length in rows)
        audit["groups"][name] = {
            "way_count": len(rows), "length_meters": round(total, 3),
            "highway_counts": dict(sorted(Counter(row["tags"]["highway"] for row, _ in rows).items())),
            "tags": {tag: {"ways_present": sum(tag in row["tags"] for row, _ in rows),
                            "length_with_tag_meters": round(sum(length for row, length in rows if tag in row["tags"]), 3),
                            "length_without_tag_meters": round(sum(length for row, length in rows if tag not in row["tags"]), 3),
                            "values": dict(sorted(Counter(row["tags"][tag] for row, _ in rows if tag in row["tags"]).items()))}
                     for tag in ("surface", "smoothness", "covered", "tunnel", "access", "foot")},
        }
    data = canonical_bytes(dataset)
    audit["dataset_bytes"] = len(data)
    audit["dataset_sha256"] = hashlib.sha256(data).hexdigest()
    return data, canonical_bytes(audit)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-reproducible", action="store_true")
    args = parser.parse_args()
    data, audit = build()
    if args.check_reproducible:
        other_data, other_audit = build()
        if data != other_data or audit != other_audit or OUTPUT.read_bytes() != data or AUDIT.read_bytes() != audit:
            raise ValueError("Dataset or coverage audit is not byte-reproducible from pinned raw evidence")
    else:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_bytes(data)
        AUDIT.write_bytes(audit)
    print(json.dumps({"outcome": "reproducible" if args.check_reproducible else "built",
                      "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}))


if __name__ == "__main__":
    main()
