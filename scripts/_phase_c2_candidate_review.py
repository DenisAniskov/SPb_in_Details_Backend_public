"""Development helper: print actual acquired candidates grouped by district."""
import json
from pathlib import Path
from scripts.validate_phase_b_catalog import point_in_boundary

ROOT = Path(__file__).resolve().parents[1]


def boundary_from_full(raw):
    sources = {(e["type"], e["id"]): e for e in raw["elements"]}
    relation = sources[("relation", 369514)]
    ways = []
    for member in relation["members"]:
        if member["type"] != "way" or member["role"] not in {"outer", "inner"}:
            continue
        way = sources[("way", member["ref"])]
        ways.append({"osm_way_id": way["id"], "role": member["role"],
                     "coordinates": [[sources[("node", n)]["lat"], sources[("node", n)]["lon"]]
                                     for n in way["nodes"]]})
    return {"district": relation["tags"]["name"], "osm_relation_id": relation["id"],
            "osm_boundary_url": "https://www.openstreetmap.org/relation/369514",
            "acquisition_url": "https://www.openstreetmap.org/api/0.6/relation/369514/full.json",
            "source_checked_on": "2026-10-10", "relation_version": relation["version"],
            "relation_timestamp": relation["timestamp"], "relation_tags": relation["tags"],
            "geometry_validation": "All outer/inner way nodes present; endpoint closure replay required.",
            "ways": ways}


def get_sources_boundaries():
    boundaries = json.loads((ROOT / "docs/phase_b_boundary_evidence.json").read_text(encoding="utf-8"))
    boundary = boundary_from_full(json.loads((ROOT / "docs/phase_c2_acquisition/frunzensky_full.json").read_text(encoding="utf-8")))
    boundaries["boundaries"].append(boundary)
    boundaries["boundary_count"] += 1
    sources = {}
    for path in [ROOT / "docs/phase_c2_acquisition/east_south_kumi.json"]:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or path.name == "frunzensky_full.json":
            continue
        for element in raw.get("elements", []):
            sources[(element["type"], element["id"])] = element
    return sources, boundaries


if __name__ == "__main__":
    sources, boundaries = get_sources_boundaries()
    old = json.loads((ROOT / "docs/phase_c1_catalog_baseline.json").read_text(encoding="utf-8"))
    old_urls = {i["source_url"] for i in old}
    rows = []
    for (kind, id), e in sources.items():
        tags = e.get("tags", {})
        p = e if kind == "node" else e.get("center", {})
        if not p or f"https://www.openstreetmap.org/{kind}/{id}" in old_urls:
            continue
        district = ",".join(b["district"] for b in boundaries["boundaries"] if point_in_boundary(p["lat"], p["lon"], b))
        rows.append((district, kind, id, tags.get("name:ru", tags.get("name")), p["lat"], p["lon"],
                     {k:v for k,v in tags.items() if k in {"leisure", "historic", "tourism", "memorial", "artwork_type", "access", "foot", "wikidata", "description", "operator", "website", "opening_hours", "building"}}))
    for row in sorted(rows):
        print(json.dumps(row, ensure_ascii=False))
