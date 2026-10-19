"""Reproduce Phase B data and Haversine scenario audits without opening a database.

Run from the repository root: python -m scripts.validate_phase_b_catalog [--write-reports]
The default only reads committed inputs. Report generation preserves manual nearby
pair reviews; no network request, production DB access, or runtime ranking change
is made here. Coordinates in START_SCENARIOS are synthetic district test starts.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any

from app.models.enums import PlaceCategory
from app.models.place import Place
from scripts.phase_c1_smart_walk_baseline import SmartWalkService, calculate_haversine_distance


ROOT = Path(__file__).resolve().parents[1]
BASE_COMMIT = "fc5bcb1e11f1f6418166bc7cf742f88942a2995b"
VERIFICATION_ON = "2026-10-10"
ORIGINAL_COUNT = 25
ORIGINAL_SHA256 = "49c5956ccfb538fa1aa6e6d7156f0f84ee216881a6eec680bd192691d38c2866"
ORIGINAL_KEYS = (
    "hermitage-winter-palace", "saint-isaacs-cathedral", "savior-on-spilled-blood",
    "peter-paul-fortress", "singer-house", "russian-museum", "faberge-museum",
    "mariinsky-theatre", "mosaic-courtyard", "kunstkamera", "popov-communications-museum",
    "icebreaker-krasin", "planetarium-one", "summer-garden", "peter-great-botanical-garden",
    "mikhailovsky-garden", "tauride-garden", "new-holland", "sevkabel-port",
    "berthold-centre", "leningrad-zoo", "grand-maket-russia", "neptune-oceanarium",
    "gorokhovaya-rotunda", "pel-apothecary",
)
COVERAGE_BOUNDS = {"south": 59.78, "north": 60.15, "west": 30.10, "east": 30.58}
NEARBY_THRESHOLD_METERS = 120
OSM_URL = re.compile(r"https://www\.openstreetmap\.org/(node|way|relation)/([1-9][0-9]*)")
START_SCENARIOS = (
    ("central", "Центральная часть города", 59.9390, 30.3158),
    ("vasilyevsky", "Василеостровский район", 59.9390, 30.2810),
    ("petrogradsky", "Петроградский район", 59.9660, 30.3110),
    ("primorsky", "Приморский район", 60.0080, 30.2520),
    ("moskovsky", "Московский район", 59.8510, 30.3220),
    ("nevsky", "Невский район", 59.8770, 30.4470),
)
TIME_BUDGETS = (30, 60, 120, 240)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def catalog_digest(items: list[dict]) -> str:
    canonical = json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def canonical_identity(url: str) -> tuple[str, int]:
    match = OSM_URL.fullmatch(url) if isinstance(url, str) else None
    if not match:
        raise ValueError(f"Invalid canonical OSM reference: {url!r}")
    return match[1], int(match[2])


def validate_catalog(items: list[dict], snapshot: dict, *, preserve_original: bool = True,
                     verification_on: str = VERIFICATION_ON) -> dict:
    """Reject incompatible data and provenance without interpreting proximity as identity.

    ``verification_on`` is the audit date new records in ``items`` must carry; the
    frozen Phase B/C2/C3 gates keep the default so their manifests stay stable.
    """
    if preserve_original:
        if tuple(item.get("catalog_key") for item in items[:ORIGINAL_COUNT]) != ORIGINAL_KEYS:
            raise ValueError("Original 25 catalog identities/order changed")
        if catalog_digest(items[:ORIGINAL_COUNT]) != ORIGINAL_SHA256:
            raise ValueError("Original 25 catalog content changed")
    if not isinstance(items, list) or not items:
        raise ValueError("Catalog must be a nonempty list")
    sources = {(element["type"], element["id"]): element for element in snapshot["elements"]}
    if len(sources) != len(snapshot["elements"]):
        raise ValueError("Duplicate OSM object in provenance snapshot")
    keys: set[str] = set()
    identities: set[tuple[str, int]] = set()
    wikidata_identities: dict[str, str] = {}
    supported_categories = {member.value for member in PlaceCategory}
    for item in items:
        key = item.get("catalog_key")
        if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,79}", key):
            raise ValueError("Invalid catalog_key")
        if key in keys:
            raise ValueError(f"Duplicate catalog_key: {key}")
        keys.add(key)
        for field, maximum in (("title", 200), ("short_description", 500),
                               ("full_description", None), ("address", 300),
                               ("why_visit", None), ("visit_note", None), ("tags", 300)):
            value = item.get(field)
            if not isinstance(value, str) or not value.strip() or (maximum and len(value) > maximum):
                raise ValueError(f"{key}: invalid required text {field}")
            if value.strip().casefold() in {"todo", "tbd", "placeholder", "unknown", "неизвестно"}:
                raise ValueError(f"{key}: placeholder {field}")
        tags = [tag.strip() for tag in item["tags"].split(",")]
        if not all(tags) or len(set(tag.casefold() for tag in tags)) != len(tags):
            raise ValueError(f"{key}: empty or duplicate topic tags")
        if item.get("category") not in supported_categories:
            raise ValueError(f"{key}: unsupported category")
        if type(item.get("is_unusual")) is not bool:
            raise ValueError(f"{key}: is_unusual must be a boolean")
        minutes = item.get("recommended_visit_minutes")
        if type(minutes) is not int or not 5 <= minutes <= 180:
            raise ValueError(f"{key}: visit estimate outside integer 5..180")
        lat, lon = item.get("latitude"), item.get("longitude")
        if any(type(value) not in (int, float) or not math.isfinite(value) for value in (lat, lon)):
            raise ValueError(f"{key}: coordinates must be finite numbers")
        if not (COVERAGE_BOUNDS["south"] <= lat <= COVERAGE_BOUNDS["north"]
                and COVERAGE_BOUNDS["west"] <= lon <= COVERAGE_BOUNDS["east"]):
            raise ValueError(f"{key}: outside documented urban Saint Petersburg boundary")
        identity = canonical_identity(item.get("source_url"))
        if identity in identities:
            raise ValueError(f"{key}: duplicate canonical OSM identity")
        identities.add(identity)
        if identity not in sources:
            raise ValueError(f"{key}: canonical source absent from snapshot")
        source = sources[identity]
        source_tags = source.get("tags", {})
        wikidata = source_tags.get("wikidata")
        if wikidata:
            if wikidata in wikidata_identities:
                raise ValueError(f"{key}: duplicate canonical Wikidata identity with {wikidata_identities[wikidata]}")
            wikidata_identities[wikidata] = key
        if not source_tags.get("name") and not source_tags.get("name:ru"):
            raise ValueError(f"{key}: named attraction identity missing from snapshot")
        checked = item.get("source_checked_on")
        try:
            checked_date = date.fromisoformat(checked)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key}: invalid source verification date") from exc
        if not date(2026, 10, 3) <= checked_date <= date.fromisoformat(verification_on):
            raise ValueError(f"{key}: source verification date outside audit window")
        if key not in ORIGINAL_KEYS:
            if checked != verification_on:
                raise ValueError(f"{key}: new source must have the phase verification date")
            for field in ("coordinate_uncertainty", "verification_evidence", "district", "visit_mode"):
                value = item.get(field)
                if (not isinstance(value, str) or not value.strip()
                        or value.strip().casefold() in {"unknown", "todo", "unverified", "неизвестно"}):
                    raise ValueError(f"{key}: missing new-record provenance {field}")
            if canonical_identity(item.get("coordinate_source_url")) != identity:
                raise ValueError(f"{key}: representative coordinate source disagrees with canonical source")
            if item.get("access_point_status") != "representative_point_not_verified":
                raise ValueError(f"{key}: unsupported or falsely verified access point status")
            if item["visit_mode"] not in {"exterior", "indoor_museum", "outdoor_monument", "outdoor_memorial",
                                           "theme_park", "park_walk"}:
                raise ValueError(f"{key}: unsupported editorial visit mode")
            if (source_tags.get("access") in {"private", "no"} or source_tags.get("foot") == "no"
                    or source_tags.get("disused") == "yes" or source_tags.get("abandoned") == "yes"
                    or any(tag.startswith(("disused:", "abandoned:")) for tag in source_tags)):
                raise ValueError(f"{key}: restricted or inactive canonical attraction")
        kind = item.get("coordinate_kind")
        coordinate_source = source
        if kind == "osm_node":
            if identity[0] != "node":
                raise ValueError(f"{key}: osm_node requires a node")
            expected = (source.get("lat"), source.get("lon"))
        elif kind == "osm_bounds_center":
            if identity[0] == "node" or "center" not in source:
                raise ValueError(f"{key}: osm_bounds_center requires area center evidence")
            expected = (source["center"].get("lat"), source["center"].get("lon"))
            if "center_derivation" in source:
                bounds = source.get("bounds", {})
                values = [bounds.get(field) for field in ("minlat", "maxlat", "minlon", "maxlon")]
                if (not isinstance(source["center_derivation"], str) or not source["center_derivation"].strip()
                        or any(type(value) not in (int, float) or not math.isfinite(value) for value in values)
                        or values[0] > values[1] or values[2] > values[3]):
                    raise ValueError(f"{key}: invalid derived bounds-center evidence")
                midpoint = ((values[0] + values[1]) / 2, (values[2] + values[3]) / 2)
                if any(not math.isclose(actual, derived, rel_tol=0, abs_tol=1e-7)
                       for actual, derived in zip(expected, midpoint)):
                    raise ValueError(f"{key}: derived center does not equal bounds midpoint")
        elif kind == "osm_entrance":
            entrance_identity = canonical_identity(item.get("entrance_source_url"))
            coordinate_source = sources.get(entrance_identity, {})
            tags = coordinate_source.get("tags", {})
            if (entrance_identity[0] != "node" or tags.get("entrance") not in {"yes", "main"}
                    or tags.get("access") in {"private", "no"}
                    or not item.get("entrance_belongs_to_attraction_evidence")):
                raise ValueError(f"{key}: verified visitor entrance evidence incomplete")
            expected = (coordinate_source.get("lat"), coordinate_source.get("lon"))
        else:
            raise ValueError(f"{key}: unsupported coordinate provenance classification")
        if any(type(value) not in (int, float) for value in expected) or any(
            not math.isclose(actual, observed, rel_tol=0, abs_tol=1e-7)
            for actual, observed in zip((lat, lon), expected)
        ):
            raise ValueError(f"{key}: coordinates do not match snapshot provenance")
        address_status = item.get("address_status")
        # "official_source_verified": the printed address was checked against
        # the attraction's official source on the phase verification date; the
        # phase that ships such records must also publish the source record.
        if address_status not in {"osm_tags", "legacy_address_not_independently_verified",
                                   "location_description_not_verified", "official_source_verified"}:
            raise ValueError(f"{key}: unsupported address provenance classification")
        if address_status == "osm_tags" and not any(tag.startswith("addr:") for tag in source_tags):
            raise ValueError(f"{key}: osm_tags address has no address evidence")
        # This phase intentionally supplies no unsourced ratings/hours/photos/styles/years.
        for field in ("rating", "working_hours", "photo_url", "architectural_style", "year_built"):
            if item.get(field) is not None:
                raise ValueError(f"{key}: unsupported precision in {field}")
        for field in ("official_website", "wikipedia_url"):
            value = item.get(field)
            if value is not None and (not isinstance(value, str) or not value.startswith(("https://", "http://"))):
                raise ValueError(f"{key}: invalid optional source URL {field}")
        website = item.get("official_website")
        if website is not None and website not in {source_tags.get("website"), source_tags.get("contact:website")}:
            raise ValueError(f"{key}: website does not match recorded OSM tag")
        wikipedia = item.get("wikipedia_url")
        if wikipedia is not None:
            tag = source_tags.get("wikipedia", "")
            language, separator, article = tag.partition(":")
            expected_url = f"https://{language}.wikipedia.org/wiki/{article.replace(' ', '_')}"
            if not separator or wikipedia != expected_url:
                raise ValueError(f"{key}: Wikipedia URL does not match recorded OSM tag")
    return {
        "final_count": len(items), "original_count": ORIGINAL_COUNT,
        "added_count": len(items) - ORIGINAL_COUNT,
        "category_counts": dict(sorted(Counter(item["category"] for item in items).items())),
        "coordinate_kind_counts": dict(sorted(Counter(item["coordinate_kind"] for item in items).items())),
        "address_status_counts": dict(sorted(Counter(item["address_status"] for item in items).items())),
    }


def nearby_pairs(items: list[dict], threshold: float = NEARBY_THRESHOLD_METERS) -> list[dict]:
    pairs = []
    for index, left in enumerate(items):
        for right in items[index + 1:]:
            distance = calculate_haversine_distance(
                left["latitude"], left["longitude"], right["latitude"], right["longitude"])
            if distance <= threshold:
                pairs.append({"catalog_keys": sorted([left["catalog_key"], right["catalog_key"]]),
                              "distance_meters": round(distance, 3)})
    return sorted(pairs, key=lambda pair: pair["catalog_keys"])


def validate_boundary_evidence(evidence: dict) -> list[dict]:
    """Check a complete district member-segment snapshot before replaying containment."""
    boundaries = evidence.get("boundaries", [])
    if not boundaries or evidence.get("boundary_count") != len(boundaries):
        raise ValueError("Boundary evidence count missing or inconsistent")
    identities = set()
    for boundary in boundaries:
        identity = canonical_identity(boundary.get("osm_boundary_url"))
        if identity != ("relation", boundary.get("osm_relation_id")) or identity in identities:
            raise ValueError("District boundary identity mismatch or duplication")
        identities.add(identity)
        tags = boundary.get("relation_tags", {})
        if (tags.get("name") != boundary.get("district") or tags.get("boundary") != "administrative"
                or tags.get("admin_level") != "5" or boundary.get("source_checked_on") != VERIFICATION_ON):
            raise ValueError("District boundary metadata does not match source")
        ways = boundary.get("ways", [])
        if not ways or len({way.get("osm_way_id") for way in ways}) != len(ways):
            raise ValueError("District boundary ways missing or duplicated")
        endpoints: Counter = Counter()
        has_outer = False
        for way in ways:
            if way.get("role") not in {"outer", "inner"}:
                raise ValueError("Unsupported district boundary member role")
            has_outer |= way["role"] == "outer"
            coordinates = way.get("coordinates", [])
            if len(coordinates) < 2:
                raise ValueError("Boundary member has incomplete geometry")
            for point in coordinates:
                if (not isinstance(point, list) or len(point) != 2
                        or any(type(value) not in (int, float) or not math.isfinite(value) for value in point)
                        or not -90 <= point[0] <= 90 or not -180 <= point[1] <= 180):
                    raise ValueError("Invalid district boundary coordinate")
            endpoints[tuple(coordinates[0])] += 1
            endpoints[tuple(coordinates[-1])] += 1
        if not has_outer or any(degree % 2 for degree in endpoints.values()):
            raise ValueError("District boundary segments do not form closed rings")
    return boundaries


def point_in_boundary(lat: float, lon: float, boundary: dict) -> bool:
    """Replay OSM member segments, including holes, without closing individual ways."""
    crossings = {"outer": 0, "inner": 0}
    for way in boundary["ways"]:
        for (ay, ax), (by, bx) in zip(way["coordinates"], way["coordinates"][1:]):
            # Boundary points require an explicit review, rather than an arbitrary district tie-break.
            cross = (lon - ax) * (by - ay) - (lat - ay) * (bx - ax)
            segment_length = math.hypot(bx - ax, by - ay)
            if (segment_length > 0 and abs(cross) <= 1e-10 * segment_length
                    and min(ax, bx) - 1e-10 <= lon <= max(ax, bx) + 1e-10
                    and min(ay, by) - 1e-10 <= lat <= max(ay, by) + 1e-10):
                raise ValueError("Catalog coordinate lies on a district boundary")
            if (ay > lat) != (by > lat) and lon < ax + (lat - ay) * (bx - ax) / (by - ay):
                crossings[way["role"]] += 1
    return crossings["outer"] % 2 == 1 and crossings["inner"] % 2 == 0


def replay_district_assignments(items: list[dict], assignments_document: dict, evidence: dict,
                                *, verification_on: str = VERIFICATION_ON) -> dict:
    boundaries = validate_boundary_evidence(evidence)
    assignments = list(assignments_document.values())
    by_key = {assignment["catalog_key"]: assignment for assignment in assignments}
    if (len(by_key) != len(assignments) or by_key.keys() != assignments_document.keys()
            or by_key.keys() != {item["catalog_key"] for item in items}):
        raise ValueError("Administrative assignments must cover each catalog identity exactly once")
    for item in items:
        assignment = by_key[item["catalog_key"]]
        if any(not math.isclose(item[field], assignment.get(field, math.inf), rel_tol=0, abs_tol=1e-10)
               for field in ("latitude", "longitude")):
            raise ValueError("Administrative assignment uses stale catalog coordinates")
        matches = [boundary for boundary in boundaries
                   if point_in_boundary(item["latitude"], item["longitude"], boundary)]
        if len(matches) != 1:
            raise ValueError("Catalog coordinate must belong to exactly one administrative district")
        expected = matches[0]
        if (assignment.get("district") != expected["district"]
                or assignment.get("osm_boundary_url") != expected["osm_boundary_url"]
                or assignment.get("assignment_method") != "osm_boundary_point_in_polygon"
                or assignment.get("source_checked_on") != verification_on):
            raise ValueError("Administrative assignment disagrees with boundary replay")
        if item["catalog_key"] not in ORIGINAL_KEYS and item.get("district") != expected["district"]:
            raise ValueError(f"{item['catalog_key']}: new seed district disagrees with boundary replay")
    return by_key


def validate_geographic_audit(items: list[dict], audit: dict) -> None:
    if audit.get("coverage_bounds") != COVERAGE_BOUNDS:
        raise ValueError("Geographic coverage bounds disagree with documented scope")
    if audit.get("near_duplicate_threshold_meters") != NEARBY_THRESHOLD_METERS:
        raise ValueError("Nearby duplicate review threshold changed")
    assignments = audit.get("places", [])
    if sorted(item["catalog_key"] for item in assignments) != sorted(item["catalog_key"] for item in items):
        raise ValueError("District assignment must cover every catalog identity exactly once")
    for assignment in assignments:
        if not assignment.get("district") or assignment["district"] == "unassigned":
            raise ValueError("Missing district assignment")
        if assignment.get("assignment_method") not in {"osm_boundary_point_in_polygon", "manual_source_review"}:
            raise ValueError("District assignment method missing")
        canonical_identity(assignment.get("osm_boundary_url"))
    source_assignments = replay_district_assignments(
        items, read_json(ROOT / "docs/phase_b_district_assignments.json"),
        read_json(ROOT / "docs/phase_b_boundary_evidence.json"))
    for assignment in assignments:
        expected = source_assignments[assignment["catalog_key"]]
        if any(assignment.get(field) != expected[field]
               for field in ("district", "osm_boundary_url", "assignment_method")):
            raise ValueError("Geographic audit disagrees with replayed administrative assignment")
    counts = dict(sorted(Counter(item["district"] for item in assignments).items()))
    if audit.get("district_counts") != counts:
        raise ValueError("District distribution disagrees with coordinates/assignments")
    if len(counts) < 6:
        raise ValueError("Catalog has insufficient geographic diversity for six district scenarios")
    expected_pairs = {tuple(pair["catalog_keys"]): pair for pair in nearby_pairs(items)}
    reviewed_pairs = {tuple(pair["catalog_keys"]): pair for pair in audit.get("nearby_pairs", [])}
    if len(reviewed_pairs) != len(audit.get("nearby_pairs", [])) or reviewed_pairs.keys() != expected_pairs.keys():
        raise ValueError("Nearby duplicate review does not cover all flagged pairs exactly once")
    for key, pair in reviewed_pairs.items():
        if not math.isclose(pair["distance_meters"], expected_pairs[key]["distance_meters"], abs_tol=.01):
            raise ValueError("Nearby duplicate review distance is stale")
        if (pair.get("classification") != "distinct_attractions" or not pair.get("rationale")
                or pair.get("reviewed_on") != VERIFICATION_ON):
            raise ValueError("Nearby attractions need documented identity review")


def make_places(items: list[dict]) -> list[Place]:
    columns = {column.name for column in Place.__table__.columns} - {"id"}
    return [Place(id=index + 1, **{key: (PlaceCategory(value) if key == "category" else value)
                                for key, value in item.items() if key in columns})
            for index, item in enumerate(items)]


def scenario_report(items: list[dict]) -> dict:
    places = make_places(items)
    keys = {place.id: place.catalog_key for place in places}
    cases = []
    for label, district, lat, lon in START_SCENARIOS:
        for budget in TIME_BUDGETS:
            result = {"scenario": label, "district_label": district,
                      "start": {"latitude": lat, "longitude": lon}, "time_budget_minutes": budget}
            for catalog_label, pool in (("original_25", places[:ORIGINAL_COUNT]), ("expanded", places)):
                proposal = SmartWalkService.propose_walk(lat, lon, budget, pool)
                sequence = proposal["suggested_places_sequence"]
                result[catalog_label] = {
                    "status": proposal["status"], "catalog_keys": [keys[id] for id in sequence],
                    "estimated_total_duration_minutes": proposal["estimated_total_duration_minutes"],
                    "estimated_walking_duration_seconds": proposal["estimated_walking_duration_seconds"],
                    "estimated_visit_duration_minutes": proposal["estimated_visit_duration_minutes"],
                    "estimated_total_distance_meters": proposal["estimated_total_distance_meters"],
                    "first_leg_haversine_meters": round(calculate_haversine_distance(
                        lat, lon, next(place for place in pool if place.id == sequence[0]).latitude,
                        next(place for place in pool if place.id == sequence[0]).longitude), 1) if sequence else None,
                }
            cases.append(result)
    return {"checked_on": VERIFICATION_ON,
            "method": "Unchanged deterministic Smart Walk; Haversine / 75 m per minute plus editorial visit estimates",
            "limitation": "Synthetic district starts. Geometric estimates are not real MapKit pedestrian distances or times.",
            "cases": cases}


def write_reports(items: list[dict], snapshot: dict) -> None:
    stats = validate_catalog(items, snapshot)
    assignments_path = ROOT / "docs/phase_b_district_assignments.json"
    assignments_document = read_json(assignments_path) if assignments_path.exists() else {}
    assignments = replay_district_assignments(items, assignments_document,
                                              read_json(ROOT / "docs/phase_b_boundary_evidence.json"))
    audit_path = ROOT / "docs/phase_b_geographic_audit.json"
    previous = read_json(audit_path) if audit_path.exists() else {}
    reviews = {tuple(pair["catalog_keys"]): pair for pair in previous.get("nearby_pairs", [])}
    places = []
    for item in items:
        assignment = assignments.get(item["catalog_key"], {})
        places.append({"catalog_key": item["catalog_key"],
                       "district": assignment.get("district", item.get("district", "unassigned")),
                       "osm_boundary_url": assignment.get("osm_boundary_url", item.get("osm_boundary_url")),
                       "assignment_method": assignment.get("assignment_method", item.get("district_assignment_method"))})
    pairs = []
    for pair in nearby_pairs(items):
        review = reviews.get(tuple(pair["catalog_keys"]), {})
        pairs.append({**review, **pair, "classification": review.get("classification", "needs_review"),
                      "rationale": review.get("rationale"), "reviewed_on": review.get("reviewed_on")})
    manifest = {"base_commit": BASE_COMMIT, "verification_on": VERIFICATION_ON, **stats,
                "original_catalog_keys": list(ORIGINAL_KEYS), "original_catalog_sha256": ORIGINAL_SHA256,
                "accepted_catalog_keys": [item["catalog_key"] for item in items],
                "catalog_sha256": catalog_digest(items)}
    audit = {"checked_on": VERIFICATION_ON, "coverage_bounds": COVERAGE_BOUNDS,
             "scope": "Urban Saint Petersburg. Bounding rectangle is an exclusion guard; administrative assignments use boundary evidence.",
             "near_duplicate_threshold_meters": NEARBY_THRESHOLD_METERS, "places": places,
             "district_counts": dict(sorted(Counter(place["district"] for place in places).items())),
             "nearby_pairs": pairs}
    for path, report in ((ROOT / "docs/phase_b_catalog_manifest.json", manifest),
                         (audit_path, audit),
                         (ROOT / "docs/phase_b_smart_walk_scenarios.json", scenario_report(items))):
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-reports", action="store_true")
    arguments = parser.parse_args()
    # Historical Phase B evidence remains fixed after C2 catalog/ranking changes.
    items = read_json(ROOT / "app/db/seed_places.json")[:77]
    snapshot = read_json(ROOT / "docs/osm_source_snapshot.json")
    if arguments.write_reports:
        write_reports(items, snapshot)
    stats = validate_catalog(items, snapshot)
    manifest = read_json(ROOT / "docs/phase_b_catalog_manifest.json")
    if (manifest["final_count"] != len(items) or manifest["catalog_sha256"] != catalog_digest(items)
            or manifest["original_catalog_sha256"] != ORIGINAL_SHA256):
        raise ValueError("Catalog manifest is stale")
    validate_geographic_audit(items, read_json(ROOT / "docs/phase_b_geographic_audit.json"))
    saved_scenarios = read_json(ROOT / "docs/phase_b_smart_walk_scenarios.json")
    if saved_scenarios != scenario_report(items):
        raise ValueError("Smart Walk scenario report is stale")
    print(json.dumps({"status": "valid", **stats, "smart_walk_scenarios": len(saved_scenarios["cases"])},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
