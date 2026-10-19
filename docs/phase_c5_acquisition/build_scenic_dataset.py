#!/usr/bin/env python3
"""Build the versioned scenic dataset from cached Overpass acquisitions.

Policy (see PHASE_C5_SCENIC_DATA_AUDIT.md):

- Waypoints are POINTS ON VERIFIED PUBLIC PEDESTRIAN WAYS: the middle node
  of a named pedestrian street or embankment way, or a public square
  point/perimeter node. Area centroids of parks are never used.
- Parks and gardens are fetched for audit context only: the C3 entrance
  audit found no verified public entrances, so no park/garden becomes a
  waypoint in this dataset.
- Elements tagged access=private/permit/no/military or foot=no/private are
  excluded. Unnamed elements are excluded. Duplicates are merged by OSM id.
- Every feature records its OSM id, URL, access evidence, verification date
  and an explicit confidence/uncertainty note.

Outputs:
  - data/scenic_features.json (complete language-neutral feature export)

The original manifest is frozen. --check validates the export without writes.
Run with: python docs/phase_c5_acquisition/build_scenic_dataset.py [--check]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
OUTPUT = ROOT / "data" / "scenic_features.json"
MANIFEST_PATH = HERE.parent / "phase_c5_scenic_dataset_manifest.json"
FROZEN_MANIFEST = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
GENERATED_ON = FROZEN_MANIFEST["generatedOn"]
DISTRICTS = {
    "central": "Центральный район",
    "petrogradsky": "Петроградский район",
    "vasileostrovsky": "Василеостровский район",
    "nevsky": "Невский район",
    "primorsky": "Приморский район",
}
DATASET_VERSION = "2026.10.11+1"
BLOCKED_ACCESS = {"private", "permit", "no", "military"}
WALKABLE_HIGHWAYS = {
    "pedestrian",
    "footway",
    "service",
    "residential",
    "living_street",
    "unclassified",
    "steps",
    "path",
}
TYPE_WEIGHTS = {"embankment": 0.90, "pedestrianStreet": 0.90, "square": 0.80}


def haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )
    return 6371000 * 2 * math.asin(math.sqrt(min(1.0, h)))


def way_line(elements_geometry: list[dict], max_points: int = 12) -> list[list[float]]:
    """Reduce a way geometry to at most max_points for line-based checks."""
    nodes = [(g["lat"], g["lon"]) for g in elements_geometry if "lat" in g]
    if len(nodes) <= max_points:
        return [[round(a, 6), round(b, 6)] for a, b in nodes]
    step = (len(nodes) - 1) / (max_points - 1)
    sampled = [nodes[round(i * step)] for i in range(max_points)]
    return [[round(a, 6), round(b, 6)] for a, b in sampled]


def way_length_meters(nodes: list[tuple[float, float]]) -> float:
    return sum(haversine(nodes[i], nodes[i + 1]) for i in range(len(nodes) - 1))


def is_public(tags: dict) -> bool:
    if tags.get("access") in BLOCKED_ACCESS:
        return False
    if tags.get("foot") in BLOCKED_ACCESS:
        return False
    return True


def classify(tags: dict) -> str | None:
    if tags.get("highway") == "pedestrian":
        return "pedestrianStreet"
    name = tags.get("name", "")
    if "абережная" in name and tags.get("highway") in WALKABLE_HIGHWAYS:
        return "embankment"
    if tags.get("place") == "square":
        return "square"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Verify existing export without modifying files")
    arguments = parser.parse_args()
    features: dict[str, dict] = {}
    excluded = {"unnamed": 0, "blocked_access": 0, "park_context_only": 0, "degenerate": 0}
    per_district_counts: dict[str, dict[str, int]] = {}

    for key, label in DISTRICTS.items():
        cache_path = HERE / f"scenic_{key}.json"
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        raw = hashlib.sha256(cache_path.read_bytes()).hexdigest()
        if raw != FROZEN_MANIFEST["byDistrict"][key]["_source_sha256"]:
            raise ValueError(f"{key}: raw scenic acquisition does not match the frozen manifest")
        per_district_counts.setdefault(key, {})
        per_district_counts[key]["_source_sha256"] = raw
        per_district_counts[key]["_elements"] = len(payload.get("elements", []))
        for element in payload.get("elements", []):
            tags = element.get("tags", {})
            kind = classify(tags)
            if kind is None:
                if tags.get("leisure") in ("park", "garden"):
                    excluded["park_context_only"] += 1
                continue
            name = tags.get("name", "").strip()
            if not name or len(name) > 80:
                excluded["unnamed"] += 1
                continue
            if not is_public(tags):
                excluded["blocked_access"] += 1
                continue
            osm_type = element["type"]
            osm_id = element["id"]
            feature_id = f"spb-scenic-{key}-{osm_type}-{osm_id}"
            if feature_id in features:
                continue
            if osm_type == "way":
                geometry = element.get("geometry") or []
                nodes = [(g["lat"], g["lon"]) for g in geometry if "lat" in g]
                if len(nodes) < 2:
                    excluded["degenerate"] += 1
                    continue
                middle = nodes[len(nodes) // 2]
                line = way_line(geometry)
                extent = round(way_length_meters(nodes))
                waypoint = {"latitude": round(middle[0], 6), "longitude": round(middle[1], 6)}
            elif osm_type == "node":
                waypoint = {
                    "latitude": round(element["lat"], 6),
                    "longitude": round(element["lon"], 6),
                }
                line = None
                extent = 0
            else:
                continue
            confidence = TYPE_WEIGHTS[kind]
            uncertainty = (
                "Точка расположена на именованном общественном пешеходном пути "
                "по данным OSM; статус входа на закрытую территорию не заявляется."
                if kind != "square"
                else "Общественная городская площадь (открытое пространство); "
                "маршрут проходит по пешеходной сети вокруг/через площадь."
            )
            access_evidence = {
                tag: value
                for tag, value in tags.items()
                if tag in ("access", "foot", "highway", "place", "leisure")
            }
            features[feature_id] = {
                "id": feature_id,
                "type": kind,
                "name": name,
                "district": key,
                "districtLabel": label,
                "waypoint": waypoint,
                "line": line,
                "extentMeters": extent,
                "osm": {"type": osm_type, "id": osm_id, "url": f"https://www.openstreetmap.org/{osm_type}/{osm_id}"},
                "source": "OpenStreetMap via Overpass API",
                "license": "ODbL 1.0",
                "verifiedOn": GENERATED_ON,
                "accessEvidence": access_evidence,
                "confidence": confidence,
                "uncertainty": uncertainty,
            }
            per_district_counts[key][kind] = per_district_counts[key].get(kind, 0) + 1

    ordered = sorted(features.values(), key=lambda f: (f["district"], f["type"], f["id"]))
    manifest = {
        "datasetVersion": DATASET_VERSION,
        "generatedOn": GENERATED_ON,
        "featureCount": len(ordered),
        "byDistrict": {
            key: {k: v for k, v in per_district_counts[key].items()}
            for key in DISTRICTS
        },
        "excluded": excluded,
        "policy": {
            "parkWaypoints": "excluded (no verified public entrances, C3 audit)",
            "accessFilter": "access/foot in private|permit|no|military excluded",
            "waypointBasis": "point on a named public pedestrian way, never an area centroid",
        },
    }
    if manifest != FROZEN_MANIFEST:
        raise ValueError("Scenic feature selection no longer reproduces the frozen C5 manifest")
    document = {
        "datasetVersion": DATASET_VERSION,
        "generatedOn": GENERATED_ON,
        "source": "OpenStreetMap via Overpass API",
        "license": "ODbL 1.0",
        "attribution": "© OpenStreetMap contributors",
        "featureCount": len(ordered),
        "features": ordered,
    }
    content = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    if arguments.check:
        if not OUTPUT.is_file() or OUTPUT.read_text(encoding="utf-8") != content:
            raise ValueError("The committed scenic export differs from the reproducible source records")
    else:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(content, encoding="utf-8", newline="\n")
    print(json.dumps(manifest["byDistrict"], ensure_ascii=False, indent=1))
    print(f"features: {len(ordered)}, excluded: {excluded}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
