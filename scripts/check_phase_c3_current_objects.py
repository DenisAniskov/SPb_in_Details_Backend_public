"""Opt-in current-object verification for the reviewed C3 additions (OSM API 0.6).

Run from the repository root: python -m scripts.check_phase_c3_current_objects
Batched multi-fetch of every accepted canonical object, serial with spacing;
responses are cached as versioned evidence like the Overpass acquisition.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
import urllib.parse

from scripts.acquire_phase_c3_osm import acquire_osm_api, CACHE, ROOT

OSM_API = "https://www.openstreetmap.org/api/0.6"
BATCH = 15


def reviewed_identities() -> dict[str, list[tuple[str, int]]]:
    from scripts.build_phase_c3_catalog import REVIEWED
    by_type: dict[str, list[tuple[str, int]]] = {}
    for kind, osm_id, _key, _category, _minutes, _unusual, _reason in REVIEWED:
        by_type.setdefault(kind, []).append((kind, osm_id))
    return by_type


def main() -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    for kind, pairs in reviewed_identities().items():
        for start in range(0, len(pairs), BATCH):
            chunk = pairs[start:start + BATCH]
            ids = ",".join(str(osm_id) for _k, osm_id in chunk)
            name = f"current_{kind}_{start // BATCH}"
            url = f"{OSM_API}/{kind}s.json?{urllib.parse.urlencode({'nodes' if kind == 'node' else kind + 's': ids})}"
            result = acquire_osm_api(name, url)
            if result is None:
                print(f"{name}: missing evidence; build must not proceed")
                return
            found = {(e["type"], e["id"]) for e in result["elements"]}
            missing = set(chunk) - found
            if missing:
                raise ValueError(f"OSM API response lacks reviewed objects: {sorted(missing)}")
            time.sleep(2)
    print("current-object evidence complete")


if __name__ == "__main__":
    main()
