"""Offline C3 provenance/identity/geometry audit; no network or database access.

From the repository root: python -m scripts.validate_phase_c3_catalog [--write-reports]
Phase B/C2 inputs/reports stay frozen. C3 evidence is additive and versioned;
the frozen C2 window (126 records) is validated for unchanged identity, and the
47 C3 additions are validated against their own acquisition evidence.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re

from scripts.validate_phase_b_catalog import (
    canonical_identity, catalog_digest, nearby_pairs, read_json,
    replay_district_assignments, validate_catalog as validate_phase_b_structure,
)
from scripts.validate_phase_c2_catalog import MANUAL_CLOSE_PAIR_REVIEWS, file_sha256

ROOT = Path(__file__).resolve().parents[1]
BASE_COMMIT = "d5531552cceb398944dd646414768c0378eea8ca"
CHECKED_ON = "2026-10-10"
C2_COUNT = 126
# Phase C6.1 appends new records after this window; the C3 gate keeps
# validating exactly the frozen C3 catalog and rejects any change inside it.
C3_COUNT = 173
ATTRACTION_QUERIES = ("c3_kirovsky", "c3_krasnoselsky", "c3_primorsky_west",
                      "c3_primorsky_east", "c3_kalininsky_south", "c3_kalininsky_north",
                      "c3_vyborgsky_south", "c3_vyborgsky_north")
ENTRANCE_QUERIES = ("c3_entrances_kirovsky", "c3_entrances_krasnoselsky",
                    "c3_entrances_primorsky_west", "c3_entrances_primorsky_east",
                    "c3_entrances_kalininsky_south", "c3_entrances_kalininsky_north",
                    "c3_entrances_vyborgsky_south", "c3_entrances_vyborgsky_north")
GATE_QUERIES = ("c3_park_gates_kirovsky", "c3_park_gates_krasnoselsky",
                "c3_park_gates_primorsky", "c3_park_gates_kalininsky",
                "c3_park_gates_vyborgsky")
RESTRICTED = {"private", "no", "permit", "permit_required", "restricted", "military", "employees", "customers"}
ROUTING_FIELDS = ("routing_latitude", "routing_longitude", "routing_point_source",
                  "routing_point_verified_on", "routing_access_status")


def latest_success(requests: list, base: str) -> dict:
    def key(query_id: str) -> str:
        stable = query_id.split("_retry", 1)[0]
        for alias in ("_de", "_mail", "_ch", "_kumi"):
            if stable.endswith(alias):
                return stable[: -len(alias)]
        return stable

    entries = [e for e in requests if e.get("status") == "success"
               and not e.get("invalidated") and key(e["query_id"]) == base]
    if not entries:
        raise ValueError(f"{base}: successful acquisition required")
    # Ledger is append-ordered; the latest success binds the evidence file
    # (earlier successes remain as superseded attempt history).
    return entries[-1]


def response_file(base: str) -> Path:
    """Alias-stable response file for a query base, per the evidence ledger."""
    requests = read_json(ROOT / "docs/phase_c3_acquisition/requests.json")
    entry = latest_success(requests, base)
    return ROOT / "docs/phase_c3_acquisition" / f"{entry['query_id'].split('_retry', 1)[0]}.json"


def merged_snapshot(c3_snapshot: dict) -> dict:
    old = read_json(ROOT / "docs/osm_source_snapshot.json")
    c2 = read_json(ROOT / "docs/phase_c2_osm_source_snapshot.json")
    return {**old, "elements": old["elements"] + c2["elements"] + c3_snapshot["elements"]}


def validate_acquisition(requests: list, per_query_elements: dict) -> dict:
    checked = {}
    for base in ATTRACTION_QUERIES + ENTRANCE_QUERIES + GATE_QUERIES:
        request = latest_success(requests, base)
        path = response_file(base)
        if request.get("sha256") != file_sha256(path):
            raise ValueError(f"{base}: acquisition hash invalid")
        if (request.get("element_count") != len(json.loads(path.read_text(encoding="utf-8"))["elements"])
                or request.get("method") != "GET" or request.get("concurrency") != 1
                or request.get("requested_at_utc", "")[:10] != CHECKED_ON
                or not request.get("client") or not request.get("query")
                or request.get("client_timeout_seconds", 999) > 90):
            raise ValueError(f"{base}: acquisition binding invalid")
        checked[base] = request
    return checked


def current_object_checks(accepted_sources: list) -> dict:
    current = {}
    requests = read_json(ROOT / "docs/phase_c3_acquisition/requests.json")
    latest = {}
    for entry in requests:
        if entry.get("status") == "success" and entry["query_id"].startswith("current_"):
            latest[entry["query_id"]] = entry
    for base, entry in latest.items():
        path = ROOT / "docs/phase_c3_acquisition" / f"{base}.json"
        if entry.get("sha256") != file_sha256(path):
            raise ValueError(f"{base}: current-object hash invalid")
        raw = read_json(path)
        for e in raw["elements"]:
            identity = (e["type"], e["id"])
            if identity in current:
                raise ValueError("Duplicate current object check")
            current[identity] = e
    selected = {(e["type"], e["id"]): e for e in accepted_sources}
    if current.keys() != selected.keys():
        raise ValueError("Current object checks must cover precisely all C3 additions")
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
            "geometry_note": "Current node coordinates match; parent way/relation checks do not verify member-node coordinates or current bounds center."}


def validate_catalog(items: list[dict], c3_snapshot: dict, per_query_timestamp: dict,
                     query_for_identity: dict, *, preserve_original: bool = True) -> dict:
    baseline = read_json(ROOT / "docs/phase_c2_catalog_baseline.json")
    if preserve_original and (len(baseline) != C2_COUNT or items[:C2_COUNT] != baseline):
        raise ValueError("Original 126 C2 catalog content/identities/order changed")
    stats = validate_phase_b_structure(items, merged_snapshot(c3_snapshot), preserve_original=preserve_original)
    originals = {item["catalog_key"] for item in baseline}
    new_sources = {(e["type"], e["id"]): e for e in c3_snapshot["elements"]}
    if len(new_sources) != len(c3_snapshot["elements"]):
        raise ValueError("Duplicate additive C3 source identity")
    expected = {canonical_identity(item["source_url"]) for item in items if item["catalog_key"] not in originals}
    if preserve_original and new_sources.keys() != expected:
        raise ValueError("Additive snapshot must cover precisely all new canonical objects")
    for item in items:
        if item["catalog_key"] in originals:
            continue
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
        query = query_for_identity[canonical_identity(item["source_url"])]
        if item.get("coordinate_snapshot_timestamp") != per_query_timestamp[query]:
            raise ValueError(f"{item['catalog_key']}: coordinate snapshot timestamp mismatch")
        if item.get("current_object_checked_on") != CHECKED_ON:
            raise ValueError(f"{item['catalog_key']}: current-object check date must be recorded")
        for field in ROUTING_FIELDS:
            if item.get(field) is not None:
                raise ValueError(f"{item['catalog_key']}: unfilled routing schema must not carry {field}")
    stats.update(original_count=C2_COUNT, added_count=len(items) - C2_COUNT)
    return stats


def validate_entrance_audit() -> dict:
    audit = read_json(ROOT / "docs/phase_c3_entrance_audit.json")
    if audit.get("checked_on") != CHECKED_ON:
        raise ValueError("Entrance audit check date invalid")
    parks = audit.get("parks", {})
    if len(parks) != 13:
        raise ValueError("Entrance audit must cover the 13 accepted way-parks")
    verified = sum(1 for p in parks.values() if p["verdict"] == "verified_public_entrance")
    if verified != 0:
        raise ValueError("A verified entrance appeared without a routing-point review")
    for base in ENTRANCE_QUERIES + GATE_QUERIES:
        request = latest_success(read_json(ROOT / "docs/phase_c3_acquisition/requests.json"), base)
        if request.get("sha256") != file_sha256(response_file(base)):
            raise ValueError(f"{base}: entrance/gate evidence hash invalid")
    for p in parks.values():
        if p["public_entrance_count"] != 0:
            raise ValueError(f"{p['catalog_key']}: unexpected public entrance")
    return {"audited_parks": len(parks), "verified_public_entrances": verified,
            "verdict_counts": dict(Counter(p["verdict"] for p in parks.values())),
            "decision": audit["decision"]}


def audit() -> dict:
    stored = read_json(ROOT / "app/db/seed_places.json")
    if len(stored) < C3_COUNT:
        raise ValueError("Catalog has shrunk below the frozen C3 window")
    items = stored[:C3_COUNT]
    snapshot = read_json(ROOT / "docs/phase_c3_osm_source_snapshot.json")
    requests = read_json(ROOT / "docs/phase_c3_acquisition/requests.json")

    per_query_elements = {}
    per_query_timestamp = {}
    for base in ATTRACTION_QUERIES:
        raw = read_json(response_file(base))
        per_query_elements[base] = {(e["type"], e["id"]) for e in raw["elements"]}
        per_query_timestamp[base] = raw["osm3s"]["timestamp_osm_base"]
    acquisition = validate_acquisition(requests, per_query_elements)

    query_for_identity = {}
    for base, identities in per_query_elements.items():
        for identity in identities:
            query_for_identity.setdefault(identity, base)

    stats = validate_catalog(items, snapshot, per_query_timestamp, query_for_identity)
    current_checks = current_object_checks(snapshot["elements"])
    extra = read_json(ROOT / "docs/phase_c3_boundary_note.json")
    if extra.get("note") != "No new district boundaries acquired; all C3 additions fall inside the frozen Phase B/C2 polygons.":
        raise ValueError("C3 boundary note invalid")
    assignments = read_json(ROOT / "docs/phase_c3_district_assignments.json")
    from scripts._phase_c2_candidate_review import boundary_from_full
    c2_extra = read_json(ROOT / "docs/phase_c2_boundary_evidence.json")
    boundaries = {**read_json(ROOT / "docs/phase_b_boundary_evidence.json"),
                  "boundary_count": 12 + c2_extra["boundary_count"],
                  "boundaries": read_json(ROOT / "docs/phase_b_boundary_evidence.json")["boundaries"] + c2_extra["boundaries"]}
    geography = replay_district_assignments(items, assignments, boundaries)
    pairs = nearby_pairs(items)
    if {tuple(p["catalog_keys"]) for p in pairs} != MANUAL_CLOSE_PAIR_REVIEWS.keys():
        raise ValueError("Every final pair within 120 geometric meters requires explicit review")
    notice = (ROOT / "app/db/README.md").read_text(encoding="utf-8")
    if "OpenStreetMap contributors" not in notice or "ODbL" not in notice:
        raise ValueError("Complete catalog notice must identify C3 evidence and ODbL")
    baseline = read_json(ROOT / "docs/phase_c2_catalog_baseline.json")
    entrances = validate_entrance_audit()
    return {
        "base_commit": BASE_COMMIT, "verification_on": CHECKED_ON, **stats,
        "original_catalog_keys": [i["catalog_key"] for i in baseline],
        "original_catalog_sha256": catalog_digest(baseline),
        "accepted_catalog_keys": [i["catalog_key"] for i in items],
        "catalog_sha256": catalog_digest(items),
        "new_osm_source_urls": [i["source_url"] for i in items[C2_COUNT:]],
        "district_counts": dict(sorted(Counter(a["district"] for a in assignments.values()).items())),
        "new_district_counts": dict(sorted(Counter(i["district"] for i in items[C2_COUNT:]).items())),
        "new_coordinate_kind_counts": dict(sorted(Counter(i["coordinate_kind"] for i in items[C2_COUNT:]).items())),
        "new_visit_mode_counts": dict(sorted(Counter(i["visit_mode"] for i in items[C2_COUNT:]).items())),
        "acquisition_query_ids": sorted(acquisition),
        "current_object_checks": current_checks,
        "nearby_threshold_meters": 120,
        "nearby_pair_reviews": [{**p, "review": MANUAL_CLOSE_PAIR_REVIEWS[tuple(p["catalog_keys"])],
                                 "decision": "distinct_keep"} for p in pairs],
        "entrance_audit": entrances,
        "routing_point_implementation": ("Additive schema implemented and unfilled: API exposes "
                                         "display/routing fields; no record carries verified entrance "
                                         "coordinates in Phase C3."),
        "native_access_and_mapkit_validation": "NOT_TESTED",
        "source_offer_status": "Private repository does not fulfill public ODbL source offer; explicit publication still required.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-reports", action="store_true")
    args = parser.parse_args()
    result = audit()
    if args.write_reports:
        (ROOT / "docs/phase_c3_catalog_manifest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
