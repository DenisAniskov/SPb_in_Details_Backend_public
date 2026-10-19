"""Opt-in serial current-object comparison against the stale Overpass mirror."""
import json
import time
from scripts.acquire_phase_c2_osm import CACHE, acquire_osm_api
from scripts.build_phase_c2_catalog import REVIEWED


def main():
    old = json.loads((CACHE / "east_south_kumi.json").read_text(encoding="utf-8"))
    old_sources = {(e["type"],e["id"]):e for e in old["elements"]}
    differences = []
    for kind, plural in (("node","nodes"),("way","ways"),("relation","relations")):
        ids = [r[1] for r in REVIEWED if r[0] == kind]
        url = f"https://www.openstreetmap.org/api/0.6/{plural}.json?{plural}="+",".join(map(str,ids))
        raw = acquire_osm_api(f"current_{plural}",url)
        if raw is None:
            break
        latest = {(e["type"],e["id"]):e for e in raw["elements"]}
        assert latest.keys() == {(kind,id) for id in ids}
        for identity, current in latest.items():
            previous = old_sources[identity]
            if previous["version"] != current["version"]:
                differences.append({"type":kind,"id":identity[1],"mirror_version":previous["version"],
                                    "current_version":current["version"], "mirror_tags":previous["tags"],
                                    "current_tags":current["tags"], "current_timestamp":current["timestamp"]})
        time.sleep(2)
    (CACHE / "current_version_comparison.json").write_text(json.dumps({"checked_on":"2026-10-10", "mirror_timestamp":old["osm3s"]["timestamp_osm_base"], "changed_objects":differences},ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(differences,ensure_ascii=False,indent=2))


if __name__ == "__main__":
    main()
