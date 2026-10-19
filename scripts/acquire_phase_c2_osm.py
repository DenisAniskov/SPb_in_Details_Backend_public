"""Opt-in, serial, bounded and cached OSM evidence acquisition (never startup).

Run from the repository root: python -m scripts.acquire_phase_c2_osm
Successful raw responses and exact queries are retained as versioned evidence.
Failures are logged without repeatedly trying an unhealthy public endpoint.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "docs/phase_c2_acquisition"
CLIENT = "SmartNavigator-SPb-Phase-C2/1.0 (bounded development catalog audit; https://github.com/DenisAniskov/SmartNavigator_Spb)"
CANDIDATE = "https://overpass.openstreetmap.ru/api/interpreter"
# Legacy hostname remains reachable from the development host, while the
# current advertised hostname returned HTTP 500 (both attempts are logged).
PRIMARY = "https://overpass.kumi.systems/api/interpreter"
QUERIES = {
    "candidate_probe": '[out:json][timeout:10];node(270902609);out meta;',
    "east_south": '[out:json][timeout:45];(nwr["name"]["historic"~"^(memorial|monument|castle|manor|ruins)$"](59.79,30.36,59.95,30.58);nwr["name"]["leisure"~"^(park|garden)$"](59.79,30.36,59.95,30.58);nwr["name"]["tourism"~"^(museum|artwork|attraction|viewpoint)$"](59.79,30.36,59.95,30.58););out meta center;',
    "nevsky_north_get": '[out:json][timeout:25];(nwr["name"]["historic"~"^(memorial|monument)$"](59.87,30.40,59.94,30.53);nwr["name"]["leisure"~"^(park|garden)$"](59.87,30.40,59.94,30.53);nwr["name"]["tourism"~"^(museum|artwork|attraction|viewpoint)$"](59.87,30.40,59.94,30.53););out meta center;',
    "south_west": '[out:json][timeout:45];(nwr["name"]["historic"~"^(memorial|monument|castle|manor|ruins)$"](59.79,30.10,59.91,30.36);nwr["name"]["leisure"~"^(park|garden)$"](59.79,30.10,59.91,30.36);nwr["name"]["tourism"~"^(museum|artwork|attraction|viewpoint)$"](59.79,30.10,59.91,30.36););out meta center;',
    "north_west": '[out:json][timeout:45];(nwr["name"]["historic"~"^(memorial|monument|castle|manor|ruins)$"](59.97,30.10,60.10,30.36);nwr["name"]["leisure"~"^(park|garden)$"](59.97,30.10,60.10,30.36);nwr["name"]["tourism"~"^(museum|artwork|attraction|viewpoint)$"](59.97,30.10,60.10,30.36););out meta center;',
    "north_east": '[out:json][timeout:45];(nwr["name"]["historic"~"^(memorial|monument|castle|manor|ruins)$"](59.93,30.36,60.08,30.53);nwr["name"]["leisure"~"^(park|garden)$"](59.93,30.36,60.08,30.53);nwr["name"]["tourism"~"^(museum|artwork|attraction|viewpoint)$"](59.93,30.36,60.08,30.53););out meta center;',
    "frunzensky_boundary": '[out:json][timeout:30];rel["boundary"="administrative"]["admin_level"="5"]["name"="Фрунзенский район"](59.78,30.10,60.15,30.58);out meta;',
    "west_parks": '[out:json][timeout:30];nwr["name"]["leisure"~"^(park|garden)$"](59.79,30.10,60.10,30.36);out meta center;',
    "north_east_parks": '[out:json][timeout:30];nwr["name"]["leisure"~"^(park|garden)$"](59.93,30.36,60.08,30.53);out meta center;',
    "west_historic": '[out:json][timeout:30];nwr["name"]["historic"~"^(memorial|monument|castle|manor|ruins)$"](59.79,30.10,60.10,30.36);out meta center;',
    "north_east_historic": '[out:json][timeout:30];nwr["name"]["historic"~"^(memorial|monument)$"](59.93,30.36,60.08,30.53);out meta center;',
}


def acquire(name: str, endpoint: str, timeout: int, method: str = "POST") -> dict | None:
    path = CACHE / f"{name}.json"
    if path.exists():
        print(f"{name}: cached")
        return json.loads(path.read_text(encoding="utf-8"))
    log_path = CACHE / "requests.json"
    log = json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else []
    if any(entry["query_id"] == name for entry in log):
        print(f"{name}: prior failed request; no automatic repeat")
        return None
    query = QUERIES[name]
    encoded = urllib.parse.urlencode({"data": query})
    request = urllib.request.Request(endpoint + ("?" + encoded if method == "GET" else ""),
                                     data=encoded.encode() if method == "POST" else None,
                                     headers={"User-Agent": CLIENT, "Accept": "application/json"})
    entry = {"query_id": name, "endpoint": endpoint, "query": query,
             "requested_at_utc": datetime.now(timezone.utc).isoformat(), "client": CLIENT,
             "client_timeout_seconds": timeout, "concurrency": 1, "method": method}
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
    # Reload after the request so a separately launched diagnostic cannot erase
    # an already completed request from the evidence ledger.
    log = json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else []
    log.append(entry)
    log_path.write_text(json.dumps(log, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    acquire("candidate_probe", CANDIDATE, 15)
    for name in ("east_south", "south_west", "north_west", "north_east"):
        cache_name = name + "_kumi"
        QUERIES[cache_name] = QUERIES[name]
        result = acquire(cache_name, PRIMARY, 60, "GET")
        # Serial spacing; on server errors stop harvesting this endpoint.
        if result is None:
            print("Acquisition stopped after failure; review before choosing another source.")
            break
        time.sleep(2)


def acquire_osm_api(name: str, url: str) -> dict | None:
    """Low-volume fallback for small map boxes and exact boundary objects."""
    path = CACHE / f"{name}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    log_path = CACHE / "requests.json"
    log = json.loads(log_path.read_text(encoding="utf-8"))
    if any(entry["query_id"] == name for entry in log):
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
