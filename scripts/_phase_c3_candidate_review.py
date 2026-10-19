"""Development helper: print actual acquired C3 candidates grouped by district.

Reads only cached acquisition evidence; no network, no database.
"""
import json
from pathlib import Path
from scripts.validate_phase_b_catalog import point_in_boundary

ROOT = Path(__file__).resolve().parents[1]
ACQUISITION_QUERIES = ("c3_kirovsky", "c3_krasnoselsky", "c3_primorsky_west",
                       "c3_primorsky_east", "c3_kalininsky_south", "c3_kalininsky_north",
                       "c3_vyborgsky_south", "c3_vyborgsky_north")
PRIORITY_DISTRICTS = {"Приморский район", "Кировский район", "Красносельский район",
                      "Калининский район", "Выборгский район"}
INTERESTING_TAGS = ("leisure", "historic", "tourism", "memorial", "artwork_type",
                    "access", "foot", "wikidata", "description", "operator",
                    "website", "opening_hours", "building")


def response_files():
    """Latest successful response file per acquisition query."""
    requests = json.loads((ROOT / "docs/phase_c3_acquisition/requests.json").read_text(encoding="utf-8"))
    files = {}
    for entry in requests:
        if entry.get("status") != "success":
            continue
        base = entry["query_id"]
        # Strip any retry-attempt suffix for the district query key; the
        # response file itself is stored under the alias-stable name.
        stable = base.split("_retry", 1)[0]
        query_key = stable
        for suffix in ("_de", "_mail", "_ch", "_kumi"):
            if query_key.endswith(suffix):
                query_key = query_key[: -len(suffix)]
                break
        files[query_key] = f"{stable}.json"
    return files


def get_sources_boundaries():
    boundaries = json.loads((ROOT / "docs/phase_b_boundary_evidence.json").read_text(encoding="utf-8"))
    extra = json.loads((ROOT / "docs/phase_c2_boundary_evidence.json").read_text(encoding="utf-8"))
    boundaries = {**boundaries, "boundary_count": boundaries["boundary_count"] + extra["boundary_count"],
                  "boundaries": boundaries["boundaries"] + extra["boundaries"]}
    sources = {}
    for query, filename in response_files().items():
        if query not in ACQUISITION_QUERIES:
            continue
        raw = json.loads((ROOT / "docs/phase_c3_acquisition" / filename).read_text(encoding="utf-8"))
        for element in raw.get("elements", []):
            sources[(element["type"], element["id"])] = element
    return sources, boundaries


def candidate_rows(sources, boundaries, catalog_keys):
    rows = []
    for (kind, id), e in sorted(sources.items()):
        tags = e.get("tags", {})
        p = e if kind == "node" else e.get("center", {})
        if not p or not tags.get("name"):
            continue
        url = f"https://www.openstreetmap.org/{kind}/{id}"
        if url in catalog_keys:
            continue
        inside = [b for b in boundaries["boundaries"] if point_in_boundary(p["lat"], p["lon"], b)]
        if len(inside) != 1 or inside[0]["district"] not in PRIORITY_DISTRICTS:
            continue
        rows.append((inside[0]["district"], kind, id, tags.get("name:ru", tags["name"]),
                     p["lat"], p["lon"],
                     {k: v for k, v in tags.items() if k in INTERESTING_TAGS}))
    return rows


if __name__ == "__main__":
    sources, boundaries = get_sources_boundaries()
    catalog = json.loads((ROOT / "app/db/seed_places.json").read_text(encoding="utf-8"))
    catalog_urls = {i["source_url"] for i in catalog}
    rows = candidate_rows(sources, boundaries, catalog_urls)
    print(f"candidates: {len(rows)}")
    for row in sorted(rows):
        print(json.dumps(row, ensure_ascii=False))
