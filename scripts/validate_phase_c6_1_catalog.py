"""Offline C6.1 provenance/identity/geometry audit; no network or database access.

From the repository root: python -m scripts.validate_phase_c6_1_catalog [--write-reports]
The frozen Phase B/C2/C3 windows stay byte-identical (checked by digest against
their committed manifests). The seven C6.1 additions are validated against
their own bounded Overpass acquisition, current-object checks via the official
OSM API, official-source verification of names/addresses/operating status, and
the frozen district boundary polygons.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re

from scripts.validate_phase_b_catalog import (
    canonical_identity, catalog_digest, nearby_pairs, read_json,
    replay_district_assignments, validate_catalog as validate_phase_b_structure,
)
from scripts._phase_c6_1_candidate_review import (
    CACHE, CHECKED_ON, PRIOR_CATALOG_COUNT, ROOT, SELECTED,
    acquisition_queries, binding_successes, boundaries, file_sha256, merged_snapshot,
    per_query_data, selected_sources,
)

BASE_COMMIT = "6803c44842589c310e7c3fe2753e222dcab08871"
TOTAL_COUNT = PRIOR_CATALOG_COUNT + len(SELECTED)
RESTRICTED = {"private", "no", "permit", "permit_required", "restricted", "military", "employees", "customers"}
ROUTING_FIELDS = ("routing_latitude", "routing_longitude", "routing_point_source",
                  "routing_point_verified_on", "routing_access_status")
PRIOR_MANIFESTS = ("phase_b_catalog_manifest.json", "phase_c2_catalog_manifest.json",
                   "phase_c3_catalog_manifest.json")
CURRENT_CHECKS = ("phase_c6_1_acquisition/current_node_0.json",
                  "phase_c6_1_acquisition/current_way_0.json",
                  "phase_c6_1_acquisition/current_relation_0.json")


def frozen_windows(stored: list[dict]) -> dict:
    """The three earlier windows must be byte-identical to their manifests."""
    if len(stored) != TOTAL_COUNT:
        raise ValueError(f"Catalog must hold exactly {TOTAL_COUNT} records, found {len(stored)}")
    digests = {}
    for count, name in ((77, PRIOR_MANIFESTS[0]), (126, PRIOR_MANIFESTS[1]), (173, PRIOR_MANIFESTS[2])):
        manifest = read_json(ROOT / "docs" / name)
        digest = catalog_digest(stored[:count])
        if digest != manifest["catalog_sha256"]:
            raise ValueError(f"Frozen window {count} changed; {name} no longer reproduces")
        digests[count] = digest
    if stored[:126] != read_json(ROOT / "docs/phase_c2_catalog_baseline.json"):
        raise ValueError("Original 126 C2 catalog content/identities/order changed")
    return digests


def validate_acquisition(new_items: list[dict]) -> tuple[dict, dict]:
    queries = acquisition_queries()
    bindings = binding_successes(queries)
    elements_by_query, timestamps = per_query_data()
    sources, query_of = selected_sources()
    by_key = {item["catalog_key"]: item for item in new_items}
    if set(by_key) != set(SELECTED):
        raise ValueError("New records must be exactly the seven reviewed C6.1 additions")
    for key, identity in SELECTED.items():
        item = by_key[key]
        source = sources[identity]
        tags = source.get("tags", {})
        query = query_of[identity]
        if item["source_checked_on"] != CHECKED_ON:
            raise ValueError(f"{key}: source verification date must be the phase date")
        if item.get("coordinate_snapshot_timestamp") != timestamps[query]:
            raise ValueError(f"{key}: coordinate snapshot timestamp mismatch")
        if item.get("current_object_checked_on") != CHECKED_ON:
            raise ValueError(f"{key}: current-object check date must be recorded")
        if item["source_url"] != f"https://www.openstreetmap.org/{identity[0]}/{identity[1]}":
            raise ValueError(f"{key}: canonical source identity mismatch")
        if item.get("coordinate_source_url") != item["source_url"]:
            raise ValueError(f"{key}: representative coordinate source disagrees with canonical source")
        if item.get("access_point_status") != "representative_point_not_verified":
            raise ValueError(f"{key}: unsupported or falsely verified access point status")
        if (tags.get("access") in RESTRICTED or tags.get("foot") in RESTRICTED
                or tags.get("disused") or tags.get("abandoned")
                or any(tag.startswith(("disused:", "abandoned:")) for tag in tags)):
            raise ValueError(f"{key}: restricted or inactive canonical attraction")
        for field in ROUTING_FIELDS:
            if item.get(field) is not None:
                raise ValueError(f"{key}: unfilled routing schema must not carry {field}")
        if "не проверен" not in item["visit_note"].lower():
            raise ValueError(f"{key}: access uncertainty must be visible in the visit note")
        if item["visit_mode"] == "park_walk" and "част" not in item["visit_note"]:
            raise ValueError(f"{key}: park portion activity must be explicit")
        if item["visit_mode"] == "indoor_museum" and item["recommended_visit_minutes"] < 45:
            raise ValueError(f"{key}: full museum visit implausibly short")
    names: dict[str, str] = {}
    for item in new_items:
        normalized = re.sub(r"[^\w]", "", item["title"].casefold().replace("ё", "е"))
        if normalized in names:
            raise ValueError(f"{item['catalog_key']}: duplicate normalized visitor title")
        names[normalized] = item["catalog_key"]
    return sources, timestamps


def validate_source_verification(new_items: list[dict]) -> dict:
    verification = read_json(ROOT / "docs/phase_c6_1_source_verification.json")
    attractions = verification.get("attractions", {})
    if verification.get("checked_on") != CHECKED_ON:
        raise ValueError("Source verification check date invalid")
    if set(attractions) != set(SELECTED):
        raise ValueError("Source verification must cover exactly the C6.1 additions")
    for item in new_items:
        entry = attractions[item["catalog_key"]]
        if entry.get("checked_on") != CHECKED_ON or not entry.get("confirms"):
            raise ValueError(f"{item['catalog_key']}: official-source check incomplete")
        if entry["official_source"] not in item["verification_evidence"]:
            raise ValueError(f"{item['catalog_key']}: verification evidence must cite the official source")
        if item["address_status"] == "official_source_verified":
            if "address" not in entry["confirms"]:
                raise ValueError(f"{item['catalog_key']}: verified address lacks an official-source confirm")
        elif item["address_status"] != "osm_tags":
            raise ValueError(f"{item['catalog_key']}: unsupported address provenance for a C6.1 addition")
        elif not any(tag.startswith("addr:") for tag in _source_tags(item)):
            raise ValueError(f"{item['catalog_key']}: osm_tags address has no address evidence")
    return {key: entry["official_source"] for key, entry in sorted(attractions.items())}


def _source_tags(item: dict) -> dict:
    identity = canonical_identity(item["source_url"])
    merged = merged_snapshot()
    for element in merged["elements"]:
        if (element["type"], element["id"]) == identity:
            return element.get("tags", {})
    raise ValueError(f"{item['catalog_key']}: canonical source absent from merged snapshot")


def validate_current_object_checks(sources: dict) -> dict:
    """Current objects come from the official OSM API, never the Overpass mirror."""
    log = read_json(CACHE / "requests.json")
    current: dict[tuple[str, int], dict] = {}
    for rel_path in CURRENT_CHECKS:
        name = Path(rel_path).stem
        entry = next((e for e in log if e.get("query_id") == name and e.get("status") == "success"), None)
        if entry is None:
            raise ValueError(f"{name}: current-object check missing from the ledger")
        path = CACHE / f"{name}.json"
        if entry.get("sha256") != file_sha256(path) or not entry.get("endpoint", "").startswith(
                "https://www.openstreetmap.org/api/0.6/") or entry.get("requested_at_utc", "")[:10] != CHECKED_ON:
            raise ValueError(f"{name}: current-object check hash/date/source invalid")
        for element in read_json(path)["elements"]:
            identity = (element["type"], element["id"])
            if identity in current:
                raise ValueError("Duplicate current object check")
            current[identity] = element
    selected = {(element["type"], element["id"]): element for element in sources.values()}
    if current.keys() != selected.keys() or len(current) != len(SELECTED):
        raise ValueError("Current-object checks must cover precisely all C6.1 additions")
    changed = []
    for identity, source in selected.items():
        latest = current[identity]
        if latest["tags"] != source["tags"]:
            raise ValueError("Current selected-object tags changed; renewed editorial/access review required")
        if identity[0] == "node" and (latest["lat"], latest["lon"]) != (source["lat"], source["lon"]):
            raise ValueError("Current node coordinates changed; renewed coordinate review required")
        if latest["version"] != source["version"]:
            changed.append({"source_url": f"https://www.openstreetmap.org/{identity[0]}/{identity[1]}",
                            "mirror_version": source["version"], "current_version": latest["version"],
                            "current_timestamp": latest["timestamp"], "tags_equal": True,
                            "geometry_current": "NOT_VERIFIED"})
    return {"current_object_count": len(current), "changed_parent_versions": changed,
            "geometry_note": ("Current node coordinates match; parent way/relation checks do not verify "
                              "member-node coordinates or the current bounds center.")}


def validate_candidate_review(new_items: list[dict]) -> dict:
    review = read_json(ROOT / "docs/phase_c6_1_candidate_review.json")
    rows = review.get("reviews", [])
    if review.get("accepted_count") != len(SELECTED) or review.get("candidate_count") != len(rows):
        raise ValueError("Candidate review counts invalid")
    accepted_identities = {(element["type"], element["id"]) for element in selected_sources()[0].values()}
    decisions = {(row["osm_type"], row["osm_id"]): row for row in rows}
    if len(decisions) != len(rows):
        raise ValueError("Duplicate candidate decisions")
    for identity in accepted_identities:
        row = decisions.get(identity)
        if row is None or row["status"] != "accepted" or not row["reason"]:
            raise ValueError(f"{identity}: accepted candidate lacks an explicit decision")
    for row in rows:
        if row["status"] not in {"accepted", "not_accepted"} or not row["reason"]:
            raise ValueError(f"candidate {row['osm_type']}/{row['osm_id']}: decision reason missing")
        if row["status"] == "accepted" and (row["osm_type"], row["osm_id"]) not in accepted_identities:
            raise ValueError("Candidate marked accepted without a catalog record")
    if any(item["catalog_key"] not in SELECTED for item in new_items):
        raise ValueError("Candidate review does not match the additions")
    return {"candidate_count": review["candidate_count"], "accepted_count": review["accepted_count"],
            "not_accepted_count": review["candidate_count"] - review["accepted_count"]}


def validate_nearby_pairs(stored: list[dict]) -> dict:
    pairs = nearby_pairs(stored)
    window_pairs = [pair for pair in pairs
                    if any(key in SELECTED for key in pair["catalog_keys"])]
    prior_pairs = {tuple(pair["catalog_keys"]) for pair in nearby_pairs(stored[:PRIOR_CATALOG_COUNT])}
    manifest = read_json(ROOT / "docs/phase_c3_catalog_manifest.json")
    if prior_pairs != {tuple(pair["catalog_keys"]) for pair in manifest["nearby_pair_reviews"]}:
        raise ValueError("Frozen-window close-pair review set changed")
    review = read_json(ROOT / "docs/phase_c6_1_nearby_pair_reviews.json")
    if review.get("threshold_meters") != 120 or review.get("catalog_pairs_within_threshold") != window_pairs:
        raise ValueError("C6.1 close-pair review is stale")
    reviewed_keys = {tuple(entry.get("catalog_keys", [])) for entry in review.get("reviews", [])}
    flagged = {tuple(pair["catalog_keys"]) for pair in window_pairs}
    if reviewed_keys != flagged:
        raise ValueError("Every C6.1 close pair requires an explicit review")
    for entry in review.get("reviews", []):
        if entry.get("decision") not in {"distinct_keep", "duplicate_rejected"} or not entry.get("rationale"):
            raise ValueError("C6.1 close-pair review needs a documented decision")
    if window_pairs and not review.get("reviews"):
        raise ValueError("C6.1 close-pair review is missing")
    return {"nearby_threshold_meters": 120, "window_close_pairs": window_pairs,
            "window_reviews": review.get("reviews", []),
            "decision": review.get("decision")}


def audit() -> dict:
    stored = read_json(ROOT / "app/db/seed_places.json")
    digests = frozen_windows(stored)
    new_items = stored[PRIOR_CATALOG_COUNT:]
    sources, timestamps = validate_acquisition(new_items)
    window_stats = validate_phase_b_structure(new_items, merged_snapshot(), preserve_original=False,
                                              verification_on=CHECKED_ON)
    stats = {"final_count": len(stored), "original_count": PRIOR_CATALOG_COUNT,
             "added_count": len(new_items),
             "new_category_counts": window_stats["category_counts"],
             "new_coordinate_kind_counts": window_stats["coordinate_kind_counts"],
             "new_address_status_counts": window_stats["address_status_counts"]}
    geography = replay_district_assignments(
        new_items, read_json(ROOT / "docs/phase_c6_1_district_assignments.json"),
        boundaries(), verification_on=CHECKED_ON)
    boundary_note = read_json(ROOT / "docs/phase_c6_1_boundary_note.json")
    if boundary_note.get("note") != ("No new district boundaries acquired; all C6.1 additions fall "
                                     "inside the frozen Phase B/C2 polygons."):
        raise ValueError("C6.1 boundary note invalid")
    source_verification = validate_source_verification(new_items)
    current_checks = validate_current_object_checks(sources)
    candidate_review = validate_candidate_review(new_items)
    pairs = validate_nearby_pairs(stored)
    bindings = binding_successes(acquisition_queries())
    notice = (ROOT / "app/db/README.md").read_text(encoding="utf-8")
    if "OpenStreetMap contributors" not in notice or "ODbL" not in notice or "Phase C6.1" not in notice:
        raise ValueError("Complete catalog notice must identify C6.1 evidence and ODbL")
    return {
        "base_commit": BASE_COMMIT, "verification_on": CHECKED_ON, **stats,
        "frozen_window_sha256": {str(count): digest for count, digest in digests.items()},
        "accepted_catalog_keys": [item["catalog_key"] for item in stored],
        "catalog_sha256": catalog_digest(stored),
        "new_catalog_keys": [item["catalog_key"] for item in new_items],
        "new_district_counts": dict(sorted(Counter(assignment["district"] for assignment in geography.values()).items())),
        "new_visit_mode_counts": dict(sorted(Counter(item["visit_mode"] for item in new_items).items())),
        "acquisition_query_ids": sorted(bindings),
        "acquisition_sha256": {base: entry["sha256"] for base, entry in sorted(bindings.items())},
        "acquisition_response_files": {base: entry["response_file"] for base, entry in sorted(bindings.items())},
        "coordinate_snapshot_timestamps": timestamps,
        "current_object_checks": current_checks,
        "candidate_review": candidate_review,
        "source_verification": source_verification,
        **pairs,
        "routing_point_implementation": ("Additive schema implemented and unfilled: no C6.1 record carries "
                                         "verified entrance coordinates."),
        "native_access_and_mapkit_validation": "NOT_TESTED",
        "source_offer_status": ("Private repository does not fulfill public ODbL source offer; explicit "
                                "publication still required."),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-reports", action="store_true")
    args = parser.parse_args()
    result = audit()
    manifest_path = ROOT / "docs/phase_c6_1_catalog_manifest.json"
    if args.write_reports:
        manifest_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    elif not manifest_path.exists() or read_json(manifest_path) != result:
        raise ValueError("C6.1 catalog manifest is stale; rerun with --write-reports")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
