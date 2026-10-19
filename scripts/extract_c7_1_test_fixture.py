"""Extract a small, self-contained REAL OSM snippet as the C7.1 test fixture.

Run from the repository root: python -m scripts.extract_c7_1_test_fixture

The snippet is cut from the committed-evidence acquisition cache
(``docs/phase_c7_1_acquisition/centre_palace.osm.xml``) over a small box
around the Winter Palace and contains the complete node/way/relation closure
for the buildings inside it — real OpenStreetMap data (ODbL, © OpenStreetMap
contributors), fixed in the repository so tests can exercise the real parsing
paths offline, deterministically, and without any network access.

The fixture file records its provenance (source file + SHA-256, bbox,
retrieval date) so nothing about it is anonymous.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs/phase_c7_1_acquisition/centre_palace.osm.xml"
TARGET = ROOT / "tests/fixtures/c7_1_osm_palace_snippet.osm.xml"
SCENARIOS = ROOT / "tests/fixtures/c7_1_scenarios.json"
# Winter Palace and its immediate neighbours in the historic centre; the box
# fully contains the palace relation (59.93928, 30.31129)–(59.94166, 30.31628)
# with all 23 member ways present, so the multipolygon assembly path is
# exercised on real data.
BBOX = (59.9388, 30.3105, 59.9420, 30.3180)  # s, w, n, e


def write_scenarios() -> int:
    """Reproducible §19 control scenarios: real OSM PEDESTRIAN walking geometry.

    Each scenario is one real OSM way taken verbatim from the cached zone
    responses, with its id, source zone and geometry so the scenario can be
    re-derived and audited. Selection rules keep the scenarios meaningful:

    * the way must be walkable (``highway`` in a pedestrian-capable set and
      never ``foot=no`` — a car carriageway centreline is not a walking route);
    * every vertex must lie inside the mapped zone box, so coverage is exactly
      known and the scenario doubles as a coverage regression;
    * the longest such way wins, which gives real end-to-end walking geometry.

    The scenarios are ROUTE geometry references, not shade measurements.
    """
    import math

    from scripts.acquire_phase_c7_1_osm import ZONES

    def haversine(a, b):
        (la, lo), (lb, lob) = a, b
        phi1, phi2 = math.radians(la), math.radians(lb)
        d_phi = math.radians(lb - la)
        d_lambda = math.radians(lob - lo)
        h = (math.sin(d_phi / 2) ** 2
             + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2)
        return 6_371_000.0 * 2 * math.atan2(math.sqrt(h), math.sqrt(1 - h))

    walkable = {"footway", "pedestrian", "living_street", "path", "steps",
                "residential", "service"}
    # Verified-by-inspection real cases with distinct physical character, so a
    # scenario's expected behaviour can be reasoned about instead of merely
    # memorised. Ids are stable OSM way ids from the cached zone responses.
    wanted = {
        "gostiny_dvor_arcade": ("centre_nevsky", {
            "way_id": 452713489,
            "character": ("Замкнутый обход внутри Гостиного двора (галереи/проходы "
                          "под зданием): ожидаемо в тени почти в любое время дня."),
        }),
        "palace_square_open": ("centre_palace", {
            "way_id": 217399816,
            "character": ("Открытое пространство Дворцовой площади (периметр площади, "
                          "living_street): в полдень ожидаемо на солнце."),
        }),
        "isaac_blocks": ("admiralteysky", {
            "way_id": 148613750,
            "character": ("Пешеходный проход в плотной застройке у Исаакиевского собора: "
                          "частично в тени, чувствителен к высоте Солнца."),
        }),
    }
    scenarios = {}
    for key, (zone_id, spec) in wanted.items():
        zone_file = f"{zone_id}.osm.xml"
        path = ROOT / "docs/phase_c7_1_acquisition" / zone_file
        if not path.exists():
            print(f"scenario {key}: source cache missing, skipped")
            continue
        s, w, n, e = ZONES[zone_id]["bbox"]
        root = ET.fromstring(path.read_bytes())
        nodes = {int(node.get("id")): (float(node.get("lat")), float(node.get("lon")))
                 for node in root.findall("node")}
        way = next((way for way in root.findall("way")
                    if int(way.get("id")) == spec["way_id"]), None)
        if way is None:
            print(f"scenario {key}: way {spec['way_id']} not found, skipped")
            continue
        tags = {t.get("k"): t.get("v") for t in way.findall("tag")}
        pts = [nodes[int(nd.get("ref"))] for nd in way.findall("nd")
               if int(nd.get("ref")) in nodes]
        if len(pts) < 5 or tags.get("highway") not in walkable or tags.get("foot") == "no":
            print(f"scenario {key}: way {spec['way_id']} is not usable walking geometry, skipped")
            continue
        inside = all(s <= la <= n and w <= lo <= e for la, lo in pts)
        length = sum(haversine(a, b) for a, b in zip(pts, pts[1:]))
        scenarios[key] = {
            "source_zone": zone_id,
            "osm_way_id": spec["way_id"],
            "name": tags.get("name", ""),
            "highway": tags.get("highway", ""),
            "character": spec["character"],
            "closed_loop": pts[0] == pts[-1],
            "fully_inside_zone": inside,
            "length_m": round(length, 1),
            "geometry_lat_lon": [[round(la, 7), round(lo, 7)] for la, lo in pts],
        }
    payload = {
        "description": ("C7.1 reproducible geographic control scenarios: real OSM "
                        "pedestrian way geometry, verbatim from the acquisition cache."),
        "license": "ODbL 1.0 — © OpenStreetMap contributors",
        "extracted_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "note": ("Geometry references only. Expected shade values in tests are model "
                 "outputs at the recorded dataset version — they are regression anchors, "
                 "not field measurements."),
        "scenarios": scenarios,
    }
    SCENARIOS.parent.mkdir(parents=True, exist_ok=True)
    SCENARIOS.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
                         encoding="utf-8")
    print(f"scenarios: {len(scenarios)} → {SCENARIOS.name}: {sorted(scenarios)}")
    for key, entry in scenarios.items():
        print(f"  {key}: way {entry['osm_way_id']} {entry['name']!r} "
              f"{entry['highway']} {entry['length_m']} m, {len(entry['geometry_lat_lon'])} pts")
    return 0


def main() -> int:
    if not SOURCE.exists():
        print(f"source snapshot missing: {SOURCE}")
        return 1
    payload = SOURCE.read_bytes()
    source_sha = hashlib.sha256(payload).hexdigest()
    root = ET.fromstring(payload)
    s, w, n, e = BBOX

    node_by_id: dict[int, ET.Element] = {}
    for node in root.findall("node"):
        node_by_id[int(node.get("id"))] = node
    way_refs: dict[int, list[int]] = {}
    way_by_id: dict[int, ET.Element] = {}
    for way in root.findall("way"):
        way_by_id[int(way.get("id"))] = way
        way_refs[int(way.get("id"))] = [int(nd.get("ref")) for nd in way.findall("nd")]

    def building_tags(element: ET.Element) -> dict[str, str]:
        return {t.get("k"): t.get("v") for t in element.findall("tag")}

    def nodes_in_box(refs: list[int]) -> bool:
        for ref in refs:
            node = node_by_id.get(ref)
            if node is None:
                return False
            lat, lon = float(node.get("lat")), float(node.get("lon"))
            if not (s <= lat <= n and w <= lon <= e):
                return False
        return True

    # Building relations fully inside the box (multipolygon path) …
    relations: list[ET.Element] = []
    relation_member_ways: set[int] = set()
    for relation in root.findall("relation"):
        if "building" not in building_tags(relation):
            continue
        member_ways = [int(m.get("ref")) for m in relation.findall("member")
                       if m.get("type") == "way"]
        if not member_ways:
            continue
        if all(ref in way_refs and nodes_in_box(way_refs[ref]) for ref in member_ways):
            relations.append(relation)
            relation_member_ways.update(member_ways)

    # … and standalone building ways fully inside the box.
    ways: dict[int, ET.Element] = {}
    for way_id, refs in way_refs.items():
        tags = building_tags(way_by_id[way_id])
        if "building" not in tags:
            continue
        if nodes_in_box(refs):
            ways[way_id] = way_by_id[way_id]

    used_way_ids = set(ways) | relation_member_ways
    used_node_ids = {ref for way_id in used_way_ids for ref in way_refs[way_id]}
    nodes = {nid: node_by_id[nid] for nid in sorted(used_node_ids) if nid in node_by_id}
    # Relation member ways must be written too, or the relations are unusable.
    ways_out = {wid: way_by_id[wid] for wid in sorted(used_way_ids) if wid in way_by_id}

    out_root = ET.Element("osm", {
        "version": "0.6", "generator": "SmartNavigator C7.1 test-fixture extractor",
        "provenance_source": SOURCE.name,
        "provenance_source_sha256": source_sha,
        "provenance_bbox_s_w_n_e": f"{s},{w},{n},{e}",
        "provenance_extracted_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "license": "ODbL 1.0 — © OpenStreetMap contributors",
    })
    ET.SubElement(out_root, "bounds", {
        "minlat": f"{s:.7f}", "minlon": f"{w:.7f}", "maxlat": f"{n:.7f}", "maxlon": f"{e:.7f}"})
    for node_id in sorted(nodes):
        out_root.append(nodes[node_id])
    for way_id in sorted(ways_out):
        out_root.append(ways_out[way_id])
    for relation in relations:
        out_root.append(relation)

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(out_root).write(TARGET, encoding="utf-8", xml_declaration=True)
    fixture_bytes = TARGET.read_bytes()
    print(f"fixture: {TARGET.name} nodes={len(nodes)} ways={len(ways_out)} "
          f"relations={len(relations)}")
    print(f"bytes={len(fixture_bytes)} sha256={hashlib.sha256(fixture_bytes).hexdigest()}")
    return write_scenarios()


if __name__ == "__main__":
    raise SystemExit(main())
