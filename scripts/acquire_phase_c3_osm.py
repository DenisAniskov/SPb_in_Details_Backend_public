"""Opt-in, serial, bounded and cached C3 OSM evidence acquisition (never startup).

Run from the repository root: python -m scripts.acquire_phase_c3_osm
C3 targets the under-represented priority districts (Primorsky, Kirovsky,
Krasnoselsky, Kalininsky, Vyborgsky) with two attraction queries and, for the
visitor-entrance audit, two bounded entrance queries around named parks.
Successful raw responses and exact queries are retained as versioned evidence.
Failures are logged without repeatedly trying an unhealthy public endpoint.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import sys
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "docs/phase_c3_acquisition"
CLIENT = "SmartNavigator-SPb-Phase-C3/1.0 (bounded development catalog audit; https://github.com/DenisAniskov/SmartNavigator_Spb)"
# kumi served Phase C2 but intermittently returned HTTP 500 during the C3 run
# (both failures are logged in requests.json); the main instance answered.
ENDPOINTS = (
    ("de", "https://overpass-api.de/api/interpreter"),
    ("mail", "https://maps.mail.ru/osm/tools/overpass/api/interpreter"),
    ("kumi", "https://overpass.kumi.systems/api/interpreter"),
)
ENDPOINT_ALIASES = {alias for alias, _ in ENDPOINTS}
# overpass.osm.ch was probed but is a regional instance: its empty
# answers for SPb boxes are not evidence of absence, so it stays out
# of the evidence chain (attempts logged in requests.json).


# Same attraction filters as Phase C2, bounded to the C3 priority districts:
# Kirovsky, Krasnoselsky, Primorsky, Kalininsky, Vyborgsky. Tight per-district
# boxes keep each query well inside the public gateways' time budget.
KIROVSKY = "(59.78,30.08,59.92,30.46)"
KRASNONSELSKY = "(59.78,30.10,59.87,30.31)"
# Primorsky's full box exceeded the public gateways' time budget (504/500,
# logged); it is acquired as two halves.
PRIMORSKY_WEST = "(59.97,30.13,60.11,30.30)"
PRIMORSKY_EAST = "(59.97,30.30,60.11,30.46)"
KALININSKY_SOUTH = "(59.95,30.28,59.99,30.51)"
KALININSKY_NORTH = "(59.99,30.28,60.08,30.51)"
VYBORGSKY_SOUTH = "(59.98,30.10,60.08,30.42)"
VYBORGSKY_NORTH = "(60.08,30.10,60.15,30.42)"
_ATTRACTIONS = ('nwr["name"]["historic"~"^(memorial|monument|castle|manor|ruins)$"]{box};'
                'nwr["name"]["leisure"~"^(park|garden)$"]{box};'
                'nwr["name"]["tourism"~"^(museum|artwork|attraction|viewpoint)$"]{box};')
ENTRANCE_RADIUS_METERS = 15

QUERIES = {
    "c3_probe": '[out:json][timeout:10];node(270902609);out meta;',
    "c3_kirovsky": '[out:json][timeout:90];(' + _ATTRACTIONS.format(box=KIROVSKY) + ');out meta center;',
    "c3_krasnoselsky": '[out:json][timeout:90];(' + _ATTRACTIONS.format(box=KRASNONSELSKY) + ');out meta center;',
    "c3_primorsky_west": '[out:json][timeout:90];(' + _ATTRACTIONS.format(box=PRIMORSKY_WEST) + ');out meta center;',
    "c3_primorsky_east": '[out:json][timeout:90];(' + _ATTRACTIONS.format(box=PRIMORSKY_EAST) + ');out meta center;',
    "c3_kalininsky_south": '[out:json][timeout:90];(' + _ATTRACTIONS.format(box=KALININSKY_SOUTH) + ');out meta center;',
    "c3_kalininsky_north": '[out:json][timeout:90];(' + _ATTRACTIONS.format(box=KALININSKY_NORTH) + ');out meta center;',
    "c3_vyborgsky_south": '[out:json][timeout:90];(' + _ATTRACTIONS.format(box=VYBORGSKY_SOUTH) + ');out meta center;',
    "c3_vyborgsky_north": '[out:json][timeout:90];(' + _ATTRACTIONS.format(box=VYBORGSKY_NORTH) + ');out meta center;',
}
# Entrance audit: visitor entrance nodes documented on or within 15 m of a
# named park/garden polygon. Park bounds are returned for local binding of
# each entrance to its park; polygons themselves stay on the server.
for _district, _box in (("kirovsky", KIROVSKY), ("krasnoselsky", KRASNONSELSKY),
                        ("primorsky_west", PRIMORSKY_WEST), ("primorsky_east", PRIMORSKY_EAST),
                        ("kalininsky_south", KALININSKY_SOUTH), ("kalininsky_north", KALININSKY_NORTH),
                        ("vyborgsky_south", VYBORGSKY_SOUTH), ("vyborgsky_north", VYBORGSKY_NORTH)):
    QUERIES[f"c3_entrances_{_district}"] = (
        f'[out:json][timeout:90];'
        f'way["name"]["leisure"~"^(park|garden)$"]{_box}->.parks;'
        f'node(around.parks:{ENTRANCE_RADIUS_METERS})["entrance"]->.gates;'
        f'.gates out meta; .parks out meta bb;')

# Gate audit for Part B: barrier=gate nodes around exactly the 13 accepted
# way-parks (district-wide versions kept timing out on public gateways and are
# logged as failed attempts). Park bounds are returned for local binding.
PARK_GATES = {
    "kirovsky": (32702194, 96364629, 141623677),
    "krasnoselsky": (154855800, 692406919, 1006790808),
    "primorsky": (143828654, 143828757),
    "kalininsky": (206639567, 261805529, 245659313),
    "vyborgsky": (23772140, 769365054),
}
for _group, _ids in PARK_GATES.items():
    QUERIES[f"c3_park_gates_{_group}"] = (
        f'[out:json][timeout:60];'
        f'way(id:{",".join(str(i) for i in _ids)})->.parks;'
        f'node(around.parks:{ENTRANCE_RADIUS_METERS})["barrier"="gate"]->.gates;'
        f'.gates out meta; .parks out meta bb;')



def acquire(name: str, endpoint: str, timeout: int, attempt: str = "") -> dict | None:
    path = CACHE / f"{name}.json"
    if path.exists():
        print(f"{name}: cached")
        return json.loads(path.read_text(encoding="utf-8"))
    log_path = CACHE / "requests.json"
    log = json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else []
    if any(entry["query_id"] == name + attempt for entry in log):
        print(f"{name}{attempt}: prior failed request; no automatic repeat")
        return None
    base = name.rsplit("_", 1)[0] if name.rsplit("_", 1)[-1] in ENDPOINT_ALIASES else name
    query = QUERIES[base]
    encoded = urllib.parse.urlencode({"data": query})
    request = urllib.request.Request(endpoint + "?" + encoded, headers={
        "User-Agent": CLIENT, "Accept": "application/json"})
    entry = {"query_id": name + attempt, "endpoint": endpoint, "query": query,
             "requested_at_utc": datetime.now(timezone.utc).isoformat(), "client": CLIENT,
             "client_timeout_seconds": timeout, "concurrency": 1, "method": "GET"}
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(8_000_001)
            if len(body) > 8_000_000:
                raise ValueError("bounded response size exceeded")
            result = json.loads(body)
            if "elements" not in result or result.get("remark"):
                raise ValueError("incomplete Overpass response")
            path.write_bytes(body)
            entry.update(status="success", response_url=response.geturl(), sha256=hashlib.sha256(body).hexdigest(),
                         element_count=len(result["elements"]), bytes=len(body),
                         source_timestamp=result.get("osm3s", {}).get("timestamp_osm_base"))
            print(f"{name}: {len(result['elements'])} objects / {len(body)} bytes")
    except (urllib.error.URLError, ValueError, TimeoutError) as exc:
        entry.update(status="failed", error=str(exc))
        result = None
        print(f"{name}: failed: {exc}")
    log = json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else []
    log.append(entry)
    log_path.write_text(json.dumps(log, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    attempt = sys.argv[1] if len(sys.argv) > 1 else ""
    for name in ("c3_probe", "c3_kirovsky", "c3_krasnoselsky", "c3_primorsky_west",
                 "c3_primorsky_east", "c3_kalininsky_south", "c3_kalininsky_north",
                 "c3_vyborgsky_south", "c3_vyborgsky_north",
                 "c3_entrances_kirovsky", "c3_entrances_krasnoselsky",
                 "c3_entrances_primorsky_west", "c3_entrances_primorsky_east",
                 "c3_entrances_kalininsky_south", "c3_entrances_kalininsky_north",
                 "c3_entrances_vyborgsky_south", "c3_entrances_vyborgsky_north",
                 "c3_park_gates_kirovsky", "c3_park_gates_krasnoselsky",
                 "c3_park_gates_primorsky", "c3_park_gates_kalininsky",
                 "c3_park_gates_vyborgsky"):
        for alias, endpoint in ENDPOINTS:
            result = acquire(f"{name}_{alias}", endpoint, 65, attempt)
            if result is not None:
                break
            time.sleep(5)
        else:
            print("Acquisition stopped: no healthy endpoint answered; review before retrying.")
            break
        time.sleep(15)


def acquire_osm_api(name: str, url: str) -> dict | None:
    """Low-volume fallback for exact current-object checks (OSM API 0.6)."""
    path = CACHE / f"{name}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    log_path = CACHE / "requests.json"
    log = json.loads(log_path.read_text(encoding="utf-8"))
    if any(entry["query_id"] == name and entry.get("status") == "failed" for entry in log):
        print(f"{name}: prior failed request; review required")
        return None
    entry = {"query_id": name, "endpoint": url,
             "requested_at_utc": datetime.now(timezone.utc).isoformat(), "client": CLIENT,
             "client_timeout_seconds": 35, "method": "GET", "concurrency": 1}
    try:
        request = urllib.request.Request(url, headers={"User-Agent": CLIENT, "Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=35) as response:
            body = response.read(8_000_001)
            if len(body) > 8_000_000:
                raise ValueError("bounded response size exceeded")
            result = json.loads(body)
            if "elements" not in result:
                raise ValueError("missing OSM elements")
            path.write_bytes(body)
            entry.update(status="success", sha256=hashlib.sha256(body).hexdigest(),
                         element_count=len(result["elements"]), bytes=len(body))
            print(f"{name}: {len(result['elements'])} objects / {len(body)} bytes")
    except (urllib.error.URLError, ValueError, TimeoutError) as exc:
        entry.update(status="failed", error=str(exc))
        result = None
        print(f"{name}: failed: {exc}")
    log = json.loads(log_path.read_text(encoding="utf-8"))
    log.append(entry)
    log_path.write_text(json.dumps(log, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


if __name__ == "__main__":
    main()
