"""Acquire and build the Phase C11 quiet-road pilot from a pinned OSM extract.

The backend runtime NEVER downloads OSM data. This development/acquisition
tool produces the versioned read-only snapshot under ``backend/data`` from one
hash-pinned Geofabrik extract (ODbL-1.0):

    python -m scripts.acquire_phase_c11_quiet_data --verify
    python -m scripts.acquire_phase_c11_quiet_data --refresh --pbf PATH
    python -m scripts.acquire_phase_c11_quiet_data --check-reproducible --pbf PATH

``--refresh`` requires the optional ``osmium`` Python package. The Docker
runtime and the backend test suite do not depend on it.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import gzip
import hashlib
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/quiet_spb_pilot.json.gz"
MANIFEST = ROOT / "data/quiet_spb_pilot.manifest.json"
CACHE = ROOT / "docs/phase_c11_acquisition/cache"

PBF_URL = ("https://download.geofabrik.de/russia/"
           "northwestern-fed-district-261016.osm.pbf")
PBF_SHA256 = "3051f24dd1dc0ab6efc63f0a015781ef8380d1523c02a0f09c1e6c5ed52d7fb7"
PBF_SIZE = 655_141_467
PBF_LAST_MODIFIED = "2026-10-17T02:13:55Z"

# west, south, east, north: central SPb, Palace Square through the Fontanka.
BBOX = (30.28, 59.915, 30.38, 59.965)
DATASET_VERSION = "c11_quiet_spb_central_v1"
SCHEMA_VERSION = 1
LICENSE = "ODbL-1.0"
LICENSE_URL = "https://opendatacommons.org/licenses/odbl/1-0/"
ATTRIBUTION = "© OpenStreetMap contributors"

# Only roads that can carry motor traffic. Footways, paths, cycleways and
# service aisles are deliberately absent: the model must not pretend that a
# missing car road is a measured quiet surface.
CORE_HIGHWAYS = frozenset({
    "motorway", "trunk", "primary", "secondary", "tertiary",
    "residential", "unclassified", "living_street",
    "motorway_link", "trunk_link", "primary_link", "secondary_link",
    "tertiary_link",
})
TAG_WHITELIST = frozenset({
    "highway", "access", "motor_vehicle", "motorcar", "vehicle", "lanes",
    "lanes:forward", "lanes:backward", "maxspeed", "maxspeed:forward",
    "maxspeed:backward", "oneway", "service", "tunnel", "bridge", "layer",
    "surface", "sidewalk", "foot", "name", "ref",
})


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _inside(lon: float, lat: float) -> bool:
    west, south, east, north = BBOX
    return west <= lon <= east and south <= lat <= north


def _segment_intersects_box(a: tuple[float, float], b: tuple[float, float]) -> bool:
    """Liang-Barsky clip: true when the source segment touches the pilot box."""
    west, south, east, north = BBOX
    x0, y0 = a
    dx, dy = b[0] - x0, b[1] - y0
    low, high = 0.0, 1.0
    for p, q in ((-dx, x0 - west), (dx, east - x0),
                 (-dy, y0 - south), (dy, north - y0)):
        if p == 0:
            if q < 0:
                return False
        else:
            t = q / p
            if p < 0:
                low = max(low, t)
            else:
                high = min(high, t)
            if low > high:
                return False
    return True


def _haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    lon1, lat1 = a
    lon2, lat2 = b
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat, dlon = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 6_371_000.0 * 2 * math.asin(min(1.0, math.sqrt(h)))


def _extract(pbf: Path) -> list[dict]:
    try:
        import osmium  # optional acquisition dependency
    except ImportError:
        raise SystemExit("osmium is required for --refresh: python -m pip install osmium==4.3.1") from None

    class Handler(osmium.SimpleHandler):
        def __init__(self) -> None:
            super().__init__()
            self.ways: list[dict] = []

        def way(self, way) -> None:
            tags = {key: way.tags[key] for key in TAG_WHITELIST if key in way.tags}
            if tags.get("highway") not in CORE_HIGHWAYS:
                return
            geometry = [[node.lon, node.lat] for node in way.nodes]
            if len(geometry) < 2:
                return
            if not any(_inside(lon, lat) for lon, lat in geometry) and not any(
                    _segment_intersects_box(a, b)
                    for a, b in zip(geometry, geometry[1:])):
                return
            for lon, lat in geometry:
                if not (math.isfinite(lon) and math.isfinite(lat)
                        and -180 <= lon <= 180 and -90 <= lat <= 90):
                    raise ValueError(f"invalid coordinate on way {way.id}")
            self.ways.append({
                "osm_id": way.id,
                "osm_version": way.version,
                "osm_timestamp": way.timestamp.isoformat() if way.timestamp else None,
                "tags": tags,
                "geometry": geometry,
            })

    handler = Handler()
    handler.apply_file(str(pbf), locations=True)
    if not handler.ways:
        raise ValueError("no pilot ways were extracted")
    if len({row["osm_id"] for row in handler.ways}) != len(handler.ways):
        raise ValueError("duplicate source way id")
    return sorted(handler.ways, key=lambda row: row["osm_id"])


def _osm_base_timestamp(pbf: Path) -> str:
    try:
        import osmium
    except ImportError:
        raise SystemExit("osmium is required for --refresh") from None
    reader = osmium.io.Reader(str(pbf))
    try:
        value = reader.header().get("osmosis_replication_timestamp")
    finally:
        reader.close()
    if not value:
        raise ValueError("the source extract has no replication timestamp")
    return value


def _download() -> Path:
    import urllib.request

    CACHE.mkdir(parents=True, exist_ok=True)
    target = CACHE / Path(PBF_URL).name
    if target.exists():
        return target
    partial = target.with_suffix(target.suffix + ".part")
    digest = hashlib.sha256()
    received = 0
    with urllib.request.urlopen(PBF_URL, timeout=120) as response, \
            partial.open("wb") as handle:
        if response.status != 200:
            raise ValueError(f"unexpected HTTP status {response.status}")
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            received += len(chunk)
            if received > PBF_SIZE:
                raise ValueError("source extract exceeds its pinned size")
            digest.update(chunk)
            handle.write(chunk)
    if received != PBF_SIZE or digest.hexdigest() != PBF_SHA256:
        partial.unlink(missing_ok=True)
        raise ValueError("downloaded extract does not match its pinned checksum")
    partial.replace(target)
    return target


def _verified_pbf(path: Path | None) -> Path:
    pbf = path or _download()
    if not pbf.exists():
        raise ValueError("pinned PBF extract is missing")
    digest = hashlib.sha256()
    size = 0
    with pbf.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    if size != PBF_SIZE or digest.hexdigest() != PBF_SHA256:
        raise ValueError("extract does not match the pinned SHA-256/size")
    return pbf


def build(pbf: Path) -> tuple[dict, bytes, bytes]:
    ways = _extract(pbf)
    osm_base = _osm_base_timestamp(pbf)
    dataset = {
        "schema_version": SCHEMA_VERSION,
        "dataset_version": DATASET_VERSION,
        "pilot_bbox": list(BBOX),
        "license": LICENSE,
        "license_url": LICENSE_URL,
        "attribution": ATTRIBUTION,
        "source": {
            "provider": "Geofabrik GmbH",
            "name": "OpenStreetMap — Northwestern Federal District extract",
            "url": PBF_URL,
            "raw_sha256": PBF_SHA256,
            "raw_bytes": PBF_SIZE,
            "raw_last_modified_utc": PBF_LAST_MODIFIED,
            "osm_base_timestamp": osm_base,
            "query": {
                "bbox_west_south_east_north": list(BBOX),
                "highway_classes": sorted(CORE_HIGHWAYS),
                "tag_whitelist": sorted(TAG_WHITELIST),
            },
        },
        "ways": ways,
    }
    data = canonical_bytes(dataset)
    compressed = gzip.compress(data, compresslevel=9, mtime=0)
    total_length = sum(_haversine(a, b) for way in ways
                       for a, b in zip(way["geometry"], way["geometry"][1:]))
    class_counts = Counter(way["tags"]["highway"] for way in ways)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "dataset_version": DATASET_VERSION,
        "source_url": PBF_URL,
        "source_sha256": PBF_SHA256,
        "source_bytes": PBF_SIZE,
        "source_last_modified_utc": PBF_LAST_MODIFIED,
        "osm_base_timestamp": osm_base,
        "pilot_bbox": list(BBOX),
        "license": LICENSE,
        "license_url": LICENSE_URL,
        "attribution": ATTRIBUTION,
        "way_count": len(ways),
        "class_counts": dict(sorted(class_counts.items())),
        "total_way_length_meters": round(total_length, 3),
        "dataset_bytes": len(data),
        "dataset_sha256": sha256(data),
        "gzip_bytes": len(compressed),
        "gzip_sha256": sha256(compressed),
        "built_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    return manifest, data, compressed


def load_manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def verify() -> dict:
    manifest = load_manifest()
    compressed = DATA.read_bytes()
    if sha256(compressed) != manifest["gzip_sha256"]:
        raise ValueError("gzip artifact checksum mismatch")
    data = gzip.decompress(compressed)
    if len(data) != manifest["dataset_bytes"] or sha256(data) != manifest["dataset_sha256"]:
        raise ValueError("dataset checksum mismatch")
    dataset = json.loads(data)
    if dataset.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported dataset schema")
    if dataset.get("dataset_version") != manifest["dataset_version"]:
        raise ValueError("dataset version mismatch")
    if list(dataset.get("pilot_bbox", [])) != list(BBOX):
        raise ValueError("pilot bbox mismatch")
    ways = dataset.get("ways") or []
    if len(ways) != manifest["way_count"] or not ways:
        raise ValueError("way count mismatch")
    ids = [row["osm_id"] for row in ways]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate way id")
    if any(row["tags"].get("highway") not in CORE_HIGHWAYS for row in ways):
        raise ValueError("unexpected highway class")
    if any(len(row["geometry"]) < 2 for row in ways):
        raise ValueError("degenerate geometry")
    return {
        "outcome": "verified",
        "dataset_version": manifest["dataset_version"],
        "way_count": manifest["way_count"],
        "dataset_sha256": manifest["dataset_sha256"],
        "gzip_sha256": manifest["gzip_sha256"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--refresh", action="store_true")
    mode.add_argument("--verify", action="store_true")
    mode.add_argument("--check-reproducible", action="store_true")
    parser.add_argument("--pbf", type=Path,
                        help="already downloaded pinned PBF (skips the download)")
    args = parser.parse_args()
    if args.verify or (not args.refresh and not args.check_reproducible):
        print(json.dumps(verify(), ensure_ascii=False))
        return
    pbf = _verified_pbf(args.pbf)
    manifest, data, compressed = build(pbf)
    if args.check_reproducible:
        if DATA.read_bytes() != compressed or load_manifest()["dataset_sha256"] != manifest["dataset_sha256"]:
            raise ValueError("the built dataset is not byte-reproducible")
        print(json.dumps({"outcome": "reproducible",
                          "dataset_sha256": manifest["dataset_sha256"]}, ensure_ascii=False))
        return
    DATA.parent.mkdir(parents=True, exist_ok=True)
    DATA.write_bytes(compressed)
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8", newline="\n")
    print(json.dumps({"outcome": "built",
                      "way_count": manifest["way_count"],
                      "dataset_sha256": manifest["dataset_sha256"],
                      "gzip_bytes": manifest["gzip_bytes"]}, ensure_ascii=False))


if __name__ == "__main__":
    sys.exit(main())
