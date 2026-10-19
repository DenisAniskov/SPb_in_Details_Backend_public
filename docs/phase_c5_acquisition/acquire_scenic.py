#!/usr/bin/env python3
"""Phase C5 scenic dataset acquisition (cache-first, idempotent).

Queries Overpass (OpenStreetMap) for verified public scenic pedestrian
features in five target districts of Saint Petersburg:

  - named pedestrian streets (highway=pedestrian)
  - named embankment ways (name ~ набережная) with walkable highway tags
  - public squares (place=square)

The C3 entrance audit found no verified public park entrances, therefore
parks/gardens are NOT claimed as accessible waypoints by this acquisition;
they are fetched only as context for the audit report. Every raw response
is cached; each request is logged. Re-running the script only re-uses the
cache (pass --refresh to force new requests).

Sources are recorded per response (endpoint, date, license, attribution).
No invented coordinates: every dataset candidate comes from an OSM element.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent

DISTRICTS = {
    "central": {"label": "Центральный район", "bbox": (59.9250, 30.2400, 59.9600, 30.4000)},
    "petrogradsky": {"label": "Петроградский район", "bbox": (59.9650, 30.2400, 59.9950, 30.3600)},
    "vasileostrovsky": {"label": "Василеостровский район", "bbox": (59.9300, 30.2000, 59.9650, 30.3100)},
    "nevsky": {"label": "Невский район", "bbox": (59.8850, 30.3500, 59.9200, 30.5000)},
    "primorsky": {"label": "Приморский район", "bbox": (59.9700, 30.2200, 60.0200, 30.4000)},
}

ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.mail.ru/api/interpreter",
]

QUERY = """[out:json][timeout:90];
(
  way["highway"="pedestrian"]["name"]({bbox});
  way["highway"]["name"~"[Нн]абережная"]["highway"~"pedestrian|footway|service|residential|living_street|unclassified|steps"]["name"]({bbox});
  node["place"="square"]["name"]({bbox});
  way["place"="square"]["name"]({bbox});
  way["leisure"~"^(park|garden)$"]["name"]({bbox});
);
out body geom;
"""


def bbox_string(bbox: tuple[float, float, float, float]) -> str:
    south, west, north, east = bbox
    return f"{south},{west},{north},{east}"


def fetch(query: str, log: list[dict], district: str) -> dict:
    body = ("data=" + urllib.parse.quote(query)).encode("utf-8")
    errors: list[str] = []
    for endpoint in ENDPOINTS:
        request = urllib.request.Request(
            endpoint,
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "SmartNavigatorSPb-C5-acquisition/1.0 "
                "(OpenStreetMap data acquisition; contact via repository)",
            },
            method="POST",
        )
        started = datetime.now(timezone.utc).isoformat()
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = json.loads(response.read().decode("utf-8"))
            log.append(
                {
                    "district": district,
                    "endpoint": endpoint,
                    "started_at": started,
                    "status": "ok",
                    "elements": len(payload.get("elements", [])),
                }
            )
            return payload
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as error:
            errors = [f"{endpoint}: {type(error).__name__}: {error}"]
            log.append(
                {
                    "district": district,
                    "endpoint": endpoint,
                    "started_at": started,
                    "status": f"error: {errors[-1]}",
                }
            )
            time.sleep(3)
    raise RuntimeError(f"All Overpass endpoints failed for {district}: {errors}")


def main() -> int:
    refresh = "--refresh" in sys.argv
    log_path = HERE / "requests.json"
    log: list[dict] = []
    if log_path.exists() and not refresh:
        log = json.loads(log_path.read_text(encoding="utf-8"))
    else:
        log = []

    ok = True
    for key, meta in DISTRICTS.items():
        cache_path = HERE / f"scenic_{key}.json"
        if cache_path.exists() and not refresh:
            print(f"[cache] {cache_path.name}")
            continue
        query = QUERY.format(bbox=bbox_string(meta["bbox"]))
        print(f"[fetch] {key} from Overpass ...")
        try:
            payload = fetch(query, log, key)
        except RuntimeError as error:
            print(f"  FAILED: {error}")
            ok = False
            continue
        payload["_acquisition"] = {
            "district": key,
            "district_label": meta["label"],
            "bbox": meta["bbox"],
            "fetched_on": date.today().isoformat(),
            "source": "OpenStreetMap via Overpass API",
            "license": "ODbL 1.0",
            "attribution": "© OpenStreetMap contributors",
        }
        cache_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(f"  saved {len(payload.get('elements', []))} elements")
        time.sleep(5)  # polite interval between bulk queries

    log_path.write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    missing = [k for k in DISTRICTS if not (HERE / f"scenic_{k}.json").exists()]
    if missing:
        print(f"missing caches: {missing}")
        return 1
    print("acquisition complete")
    return 0 if ok else 1


if __name__ == "__main__":
    import urllib.parse  # noqa: E402  (used by fetch)

    raise SystemExit(main())
