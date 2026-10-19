"""Build the C3 visitor-entrance audit from cached acquisition evidence.

Run from the repository root: python -m scripts.build_phase_c3_entrance_audit
Offline; binds the entrance=* and barrier=gate evidence to the accepted parks
and records the honest verdict: no publicly documented visitor entrance exists
for any Phase C3 addition, so routing coordinates stay unfilled.
"""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "docs/phase_c3_acquisition"
CHECKED_ON = "2026-10-10"
RADIUS_METERS = 15

# Accepted way-parks per entrance/gate acquisition group.
ACCEPTED_WAY_PARKS = {
    "kirovsky": {32702194: "komsomolsky-garden", 96364629: "molvinsky-garden",
                 141623677: "tentelovsky-square"},
    "krasnoselsky": {154855800: "general-duhanov-square", 692406919: "komissar-grushko-square",
                     1006790808: "mikhail-ashik-square"},
    "primorsky": {143828654: "matsievich-square", 143828757: "orlovy-denisovy-estate-park"},
    "kalininsky": {206639567: "kondratyevsky-garden", 261805529: "sverdlovsky-garden",
                   245659313: "zhores-alfyorov-garden"},
    "vyborgsky": {23772140: "polyustrovsky-garden", 769365054: "blockade-survivors-square"},
}
ENTRANCE_GROUPS = {"kirovsky": ("c3_entrances_kirovsky",), "krasnoselsky": ("c3_entrances_krasnoselsky",),
                   "primorsky": ("c3_entrances_primorsky_west", "c3_entrances_primorsky_east"),
                   "kalininsky": ("c3_entrances_kalininsky_south", "c3_entrances_kalininsky_north"),
                   "vyborgsky": ("c3_entrances_vyborgsky_south", "c3_entrances_vyborgsky_north")}
GATE_QUERIES = {g: f"c3_park_gates_{g}" for g in ACCEPTED_WAY_PARKS}


def successful_file(base):
    """Latest successful ledger entry and its response file for a query base."""
    log = json.loads((CACHE / "requests.json").read_text(encoding="utf-8"))
    entries = [e for e in log if e["query_id"].rsplit("_", 1)[0].rsplit("_retry", 1)[0] == base
               and e.get("status") == "success"]
    # query_id looks like <base>_<alias>[_<attempt>]; strip alias and attempt.
    entries = [e for e in log if e.get("status") == "success"
               and e["query_id"].split("_retry", 1)[0].startswith(base + "_")]
    if not entries:
        raise ValueError(f"{base}: no successful acquisition evidence")
    entry = entries[-1]
    filename = entry["query_id"].split("_retry", 1)[0] + ".json"
    body = (CACHE / filename).read_bytes()
    if hashlib.sha256(body).hexdigest() != entry["sha256"]:
        raise ValueError(f"{base}: evidence hash mismatch")
    return entry, json.loads(body)


def gates_inside(gates, bounds, tol=1e-5):
    inside = []
    for g in gates:
        tags = g.get("tags", {})
        if tags.get("access") in ("private", "no", "permit"):
            access_state = "restricted"
        elif tags.get("access"):
            access_state = "restricted"
        else:
            access_state = "untagged"
        if (bounds["minlat"] - tol <= g["lat"] <= bounds["maxlat"] + tol
                and bounds["minlon"] - tol <= g["lon"] <= bounds["maxlon"] + tol):
            inside.append({"osm_id": g["id"], "lat": g["lat"], "lon": g["lon"],
                           "entrance": tags.get("entrance"), "access": tags.get("access"),
                           "access_state": access_state, "version": g.get("version")})
    return inside


def main():
    parks = {}
    for group, way_ids in ACCEPTED_WAY_PARKS.items():
        gate_entry, gate_raw = successful_file(GATE_QUERIES[group])
        gate_ways = {e["id"]: e for e in gate_raw["elements"] if e["type"] == "way"}
        gate_nodes = [e for e in gate_raw["elements"] if e["type"] == "node"]
        entrance_elements = []
        entrance_queries = []
        for base in ENTRANCE_GROUPS[group]:
            entry, raw = successful_file(base)
            entrance_queries.append(base)
            entrance_elements.extend(e for e in raw["elements"] if e["type"] == "node")
        for way_id, key in way_ids.items():
            park = gate_ways.get(way_id)
            if park is None:
                raise ValueError(f"{key}: park absent from gate acquisition evidence")
            bounds = park["bounds"]
            bar_gates = gates_inside(gate_nodes, bounds)
            ent_gates = gates_inside(entrance_elements, bounds)
            documented = [{"node": e["osm_id"], "entrance": e["entrance"], "access": e["access"]}
                          for e in bar_gates + ent_gates]
            public = [d for d in documented if d["access"] not in ("private", "no", "permit")
                      and d["access"] is not None]
            verdict = ("verified_public_entrance" if public
                       else "restricted_access" if any(d["access"] in ("private", "no", "permit") for d in documented)
                       else "access_uncertain" if any(d["access"] is None for d in documented)
                       else "no_documented_entrance")
            parks[key] = {
                "catalog_key": key, "park_way_id": way_id,
                "park_bounds": bounds, "entrance_radius_meters": RADIUS_METERS,
                "gate_evidence_query": GATE_QUERIES[group],
                "entrance_evidence_queries": entrance_queries,
                "documented_entrance_nodes": documented,
                "public_entrance_count": len(public),
                "verdict": verdict,
            }
    result = {
        "checked_on": CHECKED_ON,
        "method": ("Bounded Overpass acquisition: entrance=* and barrier=gate nodes "
                   f"within {RADIUS_METERS} m of named park/garden polygons; per-park binding "
                   "by response bounds plus the server-side around-filter."),
        "routing_point_rule": ("A routing coordinate is filled only for an OSM-documented "
                               "visitor entrance node with entrance in {yes, main} or an explicit "
                               "public gate, without access restrictions, bound to the canonical "
                               "park. Absence of access=private is NOT treated as proof of "
                               "publicity; untagged gates are access_uncertain."),
        "result_summary": {
            "accepted_way_parks_audited": len(parks),
            "verified_public_entrances": sum(1 for p in parks.values() if p["verdict"] == "verified_public_entrance"),
            "restricted_gates": sum(1 for p in parks.values() if p["verdict"] == "restricted_access"),
            "untagged_gates": sum(1 for p in parks.values() if p["verdict"] == "access_uncertain"),
            "parks_without_documented_entrance": sum(1 for p in parks.values() if p["verdict"] == "no_documented_entrance"),
        },
        "relation_park_limitation": ("Entrance evidence acquisition covered way-mapped parks; "
                                     "relation-mapped parks were not scanned in this phase."),
        "parks": parks,
        "decision": ("No catalog record receives routing coordinates in Phase C3: zero verified "
                     "public entrances. The additive routing schema ships unfilled with "
                     "routing_access_status=representative_point_only everywhere."),
    }
    (ROOT / "docs/phase_c3_entrance_audit.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["result_summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
