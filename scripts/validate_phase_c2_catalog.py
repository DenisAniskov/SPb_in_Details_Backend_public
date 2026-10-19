"""Offline C2 provenance/identity/geometry audit; no network or database access.

From the repository root: python -m scripts.validate_phase_c2_catalog [--write-reports]
Phase B inputs/reports stay frozen. C2 evidence is additive and versioned.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re

from scripts.validate_phase_b_catalog import (
    canonical_identity, catalog_digest, nearby_pairs, read_json,
    replay_district_assignments, validate_catalog as validate_phase_b_structure,
)

ROOT = Path(__file__).resolve().parents[1]
BASE_COMMIT = "91f11840be20f86af0131ecbe7c8cbdb49883bce"
CHECKED_ON = "2026-10-10"
ORIGINAL_COUNT = 77
# Phase C3 appends new records after this window; the C2 gate keeps validating
# exactly the frozen C2 catalog and rejects any change inside the window.
C2_COUNT = 126
RESTRICTED = {"private", "no", "permit", "permit_required", "restricted", "military", "employees", "customers"}
MANUAL_CLOSE_PAIR_REVIEWS = {
    ("political-history-museum", "saint-petersburg-mosque"):
        "Unchanged Phase B pair: distinct museum and mosque, independent canonical OSM objects and visitor activities. 102.388 geometric meters does not establish a duplicate."
}


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def merged_snapshot(c2_snapshot: dict) -> dict:
    old = read_json(ROOT / "docs/osm_source_snapshot.json")
    return {**old, "elements": old["elements"] + c2_snapshot["elements"]}


def merged_boundaries(extra: dict) -> dict:
    old = read_json(ROOT / "docs/phase_b_boundary_evidence.json")
    return {**old, "boundary_count": old["boundary_count"] + extra["boundary_count"],
            "boundaries": old["boundaries"] + extra["boundaries"]}


def validate_acquisition(snapshot: dict, requests: list, raw: dict, raw_sha256: str) -> dict:
    successful = [r for r in requests if r.get("query_id") == "east_south_kumi" and r.get("status") == "success"]
    if len(successful) != 1:
        raise ValueError("Exactly one successful C2 attraction acquisition required")
    request = successful[0]
    if (request.get("sha256") != raw_sha256 or request.get("element_count") != len(raw.get("elements", []))
            or request.get("method") != "GET" or request.get("concurrency") != 1
            or request.get("requested_at_utc", "")[:10] != CHECKED_ON
            or not request.get("client") or not request.get("query")
            or request.get("client_timeout_seconds", 999) > 60):
        raise ValueError("C2 acquisition hash/query/date/count/client binding invalid")
    if raw.get("remark") or snapshot.get("checked_on") != CHECKED_ON:
        raise ValueError("Incomplete response or unsupported source-check date")
    if snapshot.get("license") != "ODbL 1.0" or snapshot.get("attribution") != "© OpenStreetMap contributors":
        raise ValueError("C2 source attribution/licence missing")
    acquired = {(e["type"], e["id"]):e for e in raw["elements"]}
    if len(acquired) != len(raw["elements"]):
        raise ValueError("Duplicate raw OSM evidence identity")
    for e in snapshot["elements"]:
        identity = (e.get("type"), e.get("id"))
        if e != acquired.get(identity):
            raise ValueError("Selected source is not identical to acquired OSM object")
        if type(e.get("version")) is not int or e["version"] <= 0:
            raise ValueError("Selected OSM version missing or invalid")
        try:
            timestamp = datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00"))
            fetched = datetime.fromisoformat(request["requested_at_utc"])
            if timestamp > fetched:
                raise ValueError("OSM object version is newer than acquisition")
        except (TypeError, KeyError, ValueError) as exc:
            raise ValueError("Invalid OSM version timestamp") from exc
    return request


def validate_catalog(items: list[dict], c2_snapshot: dict, *, preserve_original: bool = True) -> dict:
    baseline = read_json(ROOT / "docs/phase_c1_catalog_baseline.json")
    if preserve_original and (len(baseline) != ORIGINAL_COUNT or items[:ORIGINAL_COUNT] != baseline):
        raise ValueError("Original 77 catalog content/identities/order changed")
    stats = validate_phase_b_structure(items, merged_snapshot(c2_snapshot), preserve_original=preserve_original)
    originals = {item["catalog_key"] for item in baseline}
    new_sources = {(e["type"], e["id"]):e for e in c2_snapshot["elements"]}
    if len(new_sources) != len(c2_snapshot["elements"]):
        raise ValueError("Duplicate additive C2 source identity")
    expected = {canonical_identity(item["source_url"]) for item in items if item["catalog_key"] not in originals}
    if preserve_original and new_sources.keys() != expected:
        raise ValueError("Additive snapshot must cover precisely all new canonical objects")
    names = {}
    for item in items:
        # Equal names warrant a semantic review; this phase accepts no ambiguous
        # repeated names. Subject:wikidata is deliberately not object identity.
        name = re.sub(r"[^\w]", "", item["title"].casefold().replace("ё", "е"))
        if name in names:
            raise ValueError(f"Duplicate normalized visitor title: {item['catalog_key']}")
        names[name] = item["catalog_key"]
        if item["catalog_key"] in originals:
            continue
        if (item.get("coordinate_snapshot_timestamp") != "2026-07-24T11:04:51Z"
                or item.get("current_object_checked_on") != CHECKED_ON):
            raise ValueError(f"{item['catalog_key']}: coordinate and current-object check dates must be separate")
        source = new_sources.get(canonical_identity(item["source_url"]), {})
        tags = source.get("tags", {})
        if (tags.get("access") in RESTRICTED or tags.get("foot") in RESTRICTED
                or tags.get("military") or tags.get("building") in {"collapsed", "ruins"}
                or tags.get("memorial") == "grave"
                or tags.get("amenity") in {"school", "prison"}):
            raise ValueError(f"{item['catalog_key']}: restricted/institutional or unsuitable source")
        for field in ("short_description", "full_description", "why_visit", "visit_note"):
            if not re.search("[А-Яа-яЁё]", item[field]):
                raise ValueError(f"{item['catalog_key']}: original Russian editorial content missing")
        if "не проверен" not in item["visit_note"].lower():
            raise ValueError(f"{item['catalog_key']}: access uncertainty must be visible")
        if item["visit_mode"] == "park_walk" and "част" not in item["visit_note"]:
            raise ValueError(f"{item['catalog_key']}: park portion activity must be explicit")
        if item["visit_mode"] == "indoor_museum" and item["recommended_visit_minutes"] < 45:
            raise ValueError(f"{item['catalog_key']}: full museum visit implausibly short")
    stats.update(original_count=ORIGINAL_COUNT, added_count=len(items)-ORIGINAL_COUNT)
    return stats


def validate_current_object_checks(snapshot: dict, requests: list) -> dict:
    current = {}
    for name in ("nodes", "ways", "relations"):
        path = ROOT / f"docs/phase_c2_acquisition/current_{name}.json"
        request = next(r for r in requests if r["query_id"] == f"current_{name}")
        if (request["status"] != "success" or request["sha256"] != file_sha256(path)
                or request["requested_at_utc"][:10] != CHECKED_ON
                or not request["endpoint"].startswith("https://www.openstreetmap.org/api/0.6/")):
            raise ValueError("Current selected-object check hash/date/source invalid")
        raw = read_json(path)
        for e in raw["elements"]:
            identity = (e["type"], e["id"])
            if identity in current:
                raise ValueError("Duplicate current object check")
            current[identity] = e
    selected = {(e["type"], e["id"]):e for e in snapshot["elements"]}
    if current.keys() != selected.keys():
        raise ValueError("Current object checks must cover precisely all additions")
    changed = []
    for identity, source in selected.items():
        latest = current[identity]
        if latest["tags"] != source["tags"]:
            raise ValueError("Current selected-object tags changed; renewed editorial/access review required")
        if identity[0] == "node" and (latest["lat"],latest["lon"]) != (source["lat"],source["lon"]):
            raise ValueError("Current node coordinates changed; renewed coordinate review required")
        if latest["version"] != source["version"]:
            changed.append({"source_url":f"https://www.openstreetmap.org/{identity[0]}/{identity[1]}",
                            "mirror_version":source["version"], "current_version":latest["version"],
                            "current_timestamp":latest["timestamp"], "tags_equal":True,
                            "geometry_current":"NOT_VERIFIED"})
    return {"current_object_count":len(current), "changed_parent_versions":changed,
            "coordinate_snapshot_timestamp":snapshot["coordinate_snapshot_timestamp"],
            "geometry_note":"Current node coordinates match; parent way/relation checks do not verify member-node coordinates or current bounds center."}


def audit() -> dict:
    stored = read_json(ROOT / "app/db/seed_places.json")
    c2_baseline = read_json(ROOT / "docs/phase_c2_catalog_baseline.json")
    if len(c2_baseline) != C2_COUNT or stored[:C2_COUNT] != c2_baseline:
        raise ValueError("Original 126 C2 catalog content/identities/order changed")
    items = stored[:C2_COUNT]
    snapshot = read_json(ROOT / "docs/phase_c2_osm_source_snapshot.json")
    stats = validate_catalog(items, snapshot)
    raw_path = ROOT / "docs/phase_c2_acquisition/east_south_kumi.json"
    requests = read_json(ROOT / "docs/phase_c2_acquisition/requests.json")
    request = validate_acquisition(snapshot, requests,
                                   read_json(raw_path), file_sha256(raw_path))
    current_checks = validate_current_object_checks(snapshot, requests)
    # Bind boundary geometry to full official API evidence as well.
    from scripts._phase_c2_candidate_review import boundary_from_full
    extra = read_json(ROOT / "docs/phase_c2_boundary_evidence.json")
    expected_boundary = boundary_from_full(read_json(ROOT / "docs/phase_c2_acquisition/frunzensky_full.json"))
    if extra["boundaries"] != [expected_boundary] or extra["boundary_count"] != 1:
        raise ValueError("Frunzensky geometry differs from official OSM full response")
    boundary_request = next(r for r in read_json(ROOT / "docs/phase_c2_acquisition/requests.json") if r["query_id"] == "frunzensky_full")
    if (boundary_request["status"] != "success" or boundary_request["sha256"] != file_sha256(ROOT / "docs/phase_c2_acquisition/frunzensky_full.json")):
        raise ValueError("Boundary acquisition hash invalid")
    assignments = read_json(ROOT / "docs/phase_c2_district_assignments.json")
    geography = replay_district_assignments(items, assignments, merged_boundaries(extra))
    pairs = nearby_pairs(items)
    if {tuple(p["catalog_keys"]) for p in pairs} != MANUAL_CLOSE_PAIR_REVIEWS.keys():
        raise ValueError("Every final pair within 120 geometric meters requires explicit review")
    notice = (ROOT / "app/db/README.md").read_text(encoding="utf-8")
    if "OpenStreetMap contributors" not in notice or "ODbL" not in notice or "PHASE_C2_SOURCE_LICENSES" not in notice:
        raise ValueError("Complete catalog notice must identify C2 evidence and ODbL")
    baseline = read_json(ROOT / "docs/phase_c1_catalog_baseline.json")
    return {
        "base_commit":BASE_COMMIT, "verification_on":CHECKED_ON, **stats,
        "original_catalog_keys":[i["catalog_key"] for i in baseline],
        "original_catalog_sha256":catalog_digest(baseline),
        "accepted_catalog_keys":[i["catalog_key"] for i in items], "catalog_sha256":catalog_digest(items),
        "new_osm_source_urls":[i["source_url"] for i in items[ORIGINAL_COUNT:]],
        "district_counts":dict(sorted(Counter(a["district"] for a in assignments.values()).items())),
        "new_district_counts":dict(sorted(Counter(i["district"] for i in items[ORIGINAL_COUNT:]).items())),
        "new_coordinate_kind_counts":dict(sorted(Counter(i["coordinate_kind"] for i in items[ORIGINAL_COUNT:]).items())),
        "new_visit_mode_counts":dict(sorted(Counter(i["visit_mode"] for i in items[ORIGINAL_COUNT:]).items())),
        "acquisition_sha256":request["sha256"], "acquisition_query_id":request["query_id"],
        "raw_candidate_count":request["element_count"], "current_object_checks":current_checks,
        "nearby_threshold_meters":120,
        "nearby_pair_reviews":[{**p,"review":MANUAL_CLOSE_PAIR_REVIEWS[tuple(p["catalog_keys"])],"decision":"distinct_keep"} for p in pairs],
        "native_access_and_mapkit_validation":"NOT_TESTED",
        "source_offer_status":"Private repository does not fulfill public ODbL source offer; explicit publication still required.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-reports", action="store_true")
    args = parser.parse_args()
    result = audit()
    if args.write_reports:
        (ROOT / "docs/phase_c2_catalog_manifest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
