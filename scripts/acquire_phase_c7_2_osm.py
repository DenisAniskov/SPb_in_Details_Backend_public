"""Download ONE real bounded OSM pilot; preserve bytes and all attempt outcomes.

From backend: python -m scripts.acquire_phase_c7_2_osm [--verify | --refresh]
No runtime acquisition, invented fallback, retries, or whole-city query.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "docs/phase_c7_2_acquisition"
MANIFEST = CACHE / "manifest.json"
BBOX = [30.307, 59.9365, 30.326, 59.9445]  # west, south, east, north
QUERY = ('[out:json][timeout:25];\n'
         'way["highway"](59.9365,30.307,59.9445,30.326);\n'
         'out meta geom;\n')
CLIENT = ("SmartNavigator-SPb-C7.2/1.0 bounded research pilot "
          "https://github.com/DenisAniskov/SmartNavigator_Spb")
MAX_RAW_BYTES = 20_000_000


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def verified_payload(manifest: dict | None = None) -> tuple[dict, bytes]:
    """Offline verification is shared by the deterministic builder."""
    manifest = manifest or json.loads(MANIFEST.read_text(encoding="utf-8"))
    query = (CACHE / "centre_palace.overpassql").read_bytes()
    if sha256(query) != manifest["query_sha256"]:
        raise ValueError("Query checksum mismatch")
    raw_path = CACHE / manifest["raw_file"]
    # The manifest is local evidence, not an arbitrary filesystem path input.
    if raw_path.resolve().parent != CACHE.resolve():
        raise ValueError("Raw evidence path must stay inside acquisition directory")
    compressed = raw_path.read_bytes()
    if sha256(compressed) != manifest["raw_artifact_sha256"]:
        raise ValueError("Raw compressed artifact checksum mismatch")
    payload = gzip.decompress(compressed)
    if len(payload) != manifest["raw_bytes"] or sha256(payload) != manifest["raw_sha256"]:
        raise ValueError("Raw source payload checksum mismatch")
    return manifest, payload


def acquire() -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    (CACHE / "centre_palace.overpassql").write_bytes(QUERY.encode("utf-8"))
    attempts = []
    candidates = [
        ("overpass_json", "https://overpass-api.de/api/interpreter", 35),
        ("osm_xml", "https://api.openstreetmap.org/api/0.6/map?bbox="
         + ",".join(str(x) for x in BBOX), 50),
    ]
    for source_format, endpoint, timeout in candidates:
        started = utc_now()
        clock = time.perf_counter()
        request = urllib.request.Request(endpoint, headers={"User-Agent": CLIENT})
        if source_format == "overpass_json":
            request.data = urllib.parse.urlencode({"data": QUERY}).encode("ascii")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read(MAX_RAW_BYTES + 1)
                response_date = response.headers.get("Date")
                status = response.status
            if len(payload) > MAX_RAW_BYTES:
                raise ValueError("Source response exceeds bounded acquisition size")
            if source_format == "overpass_json":
                parsed = json.loads(payload)
                if parsed.get("remark") or not parsed.get("elements"):
                    raise ValueError("Overpass reported incomplete/empty data")
                base_timestamp = parsed.get("osm3s", {}).get("timestamp_osm_base")
            else:
                parsed_xml = ET.fromstring(payload)
                if parsed_xml.tag != "osm" or not parsed_xml.findall("way"):
                    raise ValueError("OSM map response has no ways")
                meta = parsed_xml.find("meta")
                base_timestamp = None if meta is None else meta.get("osm_base")
            finished = utc_now()
            attempts.append({"endpoint": endpoint, "started_at_utc": started,
                             "completed_at_utc": finished, "status": status,
                             "elapsed_seconds": round(time.perf_counter() - clock, 3),
                             "outcome": "success", "raw_bytes": len(payload)})
            suffix = "json" if source_format == "overpass_json" else "xml"
            filename = f"centre_palace.osm.{suffix}.gz"
            compressed = gzip.compress(payload, mtime=0)
            (CACHE / filename).write_bytes(compressed)
            manifest = {
                "schema_version": 1, "dataset_version": "c7_2_palace_pilot_v1",
                "pilot_bbox": BBOX, "retrieved_at_utc": finished,
                "osm_base_timestamp": base_timestamp,
                "source_format": source_format, "endpoint": endpoint,
                "response_date_header": response_date,
                "query_sha256": sha256(QUERY.encode("utf-8")),
                "raw_file": filename, "raw_bytes": len(payload),
                "raw_sha256": sha256(payload),
                "raw_artifact_bytes": len(compressed),
                "raw_artifact_sha256": sha256(compressed),
                "license": "ODbL-1.0",
                "license_url": "https://opendatacommons.org/licenses/odbl/1-0/",
                "attribution": "© OpenStreetMap contributors", "attempts": attempts,
            }
            MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2)
                                + "\n", encoding="utf-8", newline="\n")
            verified_payload(manifest)
            print(json.dumps({"outcome": "downloaded_and_verified", "format": source_format,
                              "raw_bytes": len(payload), "sha256": sha256(payload)}))
            return
        except (OSError, ValueError, urllib.error.HTTPError) as error:
            attempts.append({"endpoint": endpoint, "started_at_utc": started,
                             "completed_at_utc": utc_now(),
                             "elapsed_seconds": round(time.perf_counter() - clock, 3),
                             "outcome": "failed", "error_type": type(error).__name__,
                             "http_status": getattr(error, "code", None)})
            (CACHE / "failed_attempts.json").write_text(
                json.dumps(attempts, indent=2) + "\n", encoding="utf-8", newline="\n")
    raise RuntimeError("All real acquisition channels failed; no dataset was fabricated")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--verify", action="store_true")
    mode.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    if args.verify or (MANIFEST.exists() and not args.refresh):
        manifest, payload = verified_payload()
        print(json.dumps({"outcome": "offline_verified", "raw_bytes": len(payload),
                          "sha256": manifest["raw_sha256"]}))
    else:
        acquire()


if __name__ == "__main__":
    main()
