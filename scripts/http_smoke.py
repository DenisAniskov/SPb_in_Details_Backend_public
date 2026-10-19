"""Bounded real HTTP acceptance checks, shared by native and Docker validation.

Only the standard library is required. No Yandex keys, external API calls,
request logging, private GPS samples, or database mutation are used here.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import time
from typing import Callable
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def request(base_url: str, path: str, payload: dict | None = None,
            *, raw_body: bytes | None = None) -> tuple[int, object, float]:
    body = raw_body if raw_body is not None else (
        json.dumps(payload, separators=(",", ":")).encode() if payload is not None else None)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    query = Request(base_url.rstrip("/") + path, data=body, headers=headers)
    start = time.monotonic()
    try:
        with urlopen(query, timeout=20) as response:
            status, data = response.status, response.read()
    except HTTPError as error:
        status, data = error.code, error.read()
    try:
        parsed = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        parsed = None
    return status, parsed, round((time.monotonic() - start) * 1000, 2)


def catalog_fingerprint(items: list[dict]) -> str:
    stable = sorted((item["id"], item["title"], item["latitude"], item["longitude"]) for item in items)
    return hashlib.sha256(json.dumps(stable, separators=(",", ":")).encode()).hexdigest()


def check_http(base_url: str, *, cpu_usage_usec: Callable[[], int] | None = None) -> dict:
    """Assert actual endpoint contracts; return aggregates safe to publish in CI."""
    results: dict = {"transport": base_url.split(":", 1)[0], "latency_ms": {}}
    for path, value in (("/health", "ok"), ("/ready", "ready")):
        status, body, latency = request(base_url, path)
        require(status == 200 and body["status"] == value, f"{path} failed")
        results[path.lstrip("/")] = status
        results["latency_ms"][path] = latency

    status, catalog, latency = request(base_url, "/api/v1/places?limit=200")
    require(status == 200 and catalog["total"] == 180 and len(catalog["items"]) == 180,
            "Catalog must contain 180 readable POI")
    items = catalog["items"]
    require(len({item["id"] for item in items}) == 180, "Catalog IDs are not unique")
    seed = json.loads((ROOT / "app/db/seed_places.json").read_text(encoding="utf-8"))
    def public_identity(item: dict) -> tuple:
        return item["title"], item["latitude"], item["longitude"]
    expected_keys = {public_identity(item): item["catalog_key"] for item in seed}
    require(Counter(public_identity(item) for item in items) ==
            Counter(public_identity(item) for item in seed), "Public catalog differs from source seed")
    require(Counter(item["category"] for item in items) == Counter(item["category"] for item in seed),
            "Catalog categories differ from source seed")
    status, categories, _ = request(base_url, "/api/v1/places/categories")
    require(status == 200 and sum(item["count"] for item in categories) == 180,
            "Category counts must cover the complete catalog")
    results["catalog"] = {"poi": 180, "stable_id_sha256": catalog_fingerprint(items),
                          "categories": dict(sorted(Counter(item["category"] for item in items).items()))}
    results["latency_ms"]["catalog"] = latency
    history_count = 0
    for item in items:
        status, history, _ = request(base_url, f'/api/v1/places/{item["id"]}/history')
        require(status == 200 and history["catalogKey"] == expected_keys[public_identity(item)],
                "History must resolve the stable catalog key")
        history_count += int(history["history"] is not None)
    require(history_count == 35, "Expected exactly 35 historical cards")
    status, detail, _ = request(base_url, f'/api/v1/places/{items[0]["id"]}')
    require(status == 200 and detail["id"] == items[0]["id"], "Place detail failed")
    results["history"] = {"cards": history_count, "requests": len(items)}

    results["smart_walk"] = {}
    for minutes in (60, 120, 240):
        payload = {"start_point": {"latitude": 59.9343, "longitude": 30.3351},
                   "time_budget_minutes": minutes, "max_places": 6}
        status, walk, latency = request(base_url, "/api/v1/routes/propose-walk", payload)
        require(status == 200, "Smart Walk failed")
        require(1 <= len(walk["suggested_places_sequence"]) <= 6, "Smart Walk POI count failed")
        require(walk["estimated_total_duration_minutes"] <= minutes,
                "Smart Walk exceeded visit and walking time budget")
        require(sum(item["visit_duration_minutes"] for item in walk["items"])
                == walk["estimated_visit_duration_minutes"], "Visit-time sum differs")
        results["smart_walk"][str(minutes)] = {
            "poi": len(walk["suggested_places_sequence"]),
            "total_minutes": walk["estimated_total_duration_minutes"],
            "visit_minutes": walk["estimated_visit_duration_minutes"], "latency_ms": latency}

    scenario = json.loads((ROOT / "tests/fixtures/c7_1_scenarios.json").read_text(encoding="utf-8"))[
        "scenarios"]["isaac_blocks"]
    payload = {"departure_at": "2026-07-01T14:00:00+03:00", "include_segments": True,
               "legs": [{"geometry": [{"latitude": lat, "longitude": lon}
                                       for lat, lon in scenario["geometry_lat_lon"]],
                         "walking_duration_seconds": scenario["length_m"] / 1.35,
                         "visit_duration_seconds_after_leg": 600}]}
    # A visit advances the clock between walking legs. The final visit lies
    # outside the evaluated walking period, so use a real return leg here.
    payload["legs"].append({"geometry": list(reversed(payload["legs"][0]["geometry"])),
                            "walking_duration_seconds": scenario["length_m"] / 1.35,
                            "visit_duration_seconds_after_leg": 0})
    cpu_before = cpu_usage_usec() if cpu_usage_usec else None
    status, shade, latency = request(base_url, "/api/v1/environment/shade/analyze", payload)
    cpu_after = cpu_usage_usec() if cpu_usage_usec else None
    require(status == 200 and shade["status"] in ("available", "partial"), "Real pilot shade unavailable")
    require(shade["method_version"] == "c7_1.2", "Shade method version must preserve C7.1.2")
    require(shade["dataset_version"] == "c7_1_pilot_v1" and shade["estimate"] is True,
            "Real pilot provenance or estimate flag incorrect")
    require(shade["coverage_metrics"]["coverage_share"] == 1.0, "Pilot route coverage incorrect")
    require(shade["height_provenance_metrics"]["buildings_considered"] > 0,
            "Real building provider did not participate")
    require("OpenStreetMap" in shade["dataset_provenance"], "Missing real OSM provenance")
    total = sum(shade[key] for key in
                ("estimated_shaded_meters", "estimated_exposed_meters", "unknown_meters"))
    require(abs(total - shade["route_length_meters"]) <= 0.3, "Shade three-state invariant failed")
    require(shade["classified_segments"] and all(len(item["geometry"]) >= 2
            for item in shade["classified_segments"]), "Classified segments lack real geometry")
    elapsed = (datetime.fromisoformat(shade["evaluated_period"]["end"])
               - datetime.fromisoformat(shade["evaluated_period"]["start"])).total_seconds()
    require(abs(elapsed - (2 * payload["legs"][0]["walking_duration_seconds"] + 600)) < 1,
            "Shade walking and visit clock did not advance")
    results["shade"] = {"status": shade["status"], "dataset_version": shade["dataset_version"],
                        "method_version": shade["method_version"],
                        "route_length_meters": shade["route_length_meters"],
                        "shaded_meters": shade["estimated_shaded_meters"],
                        "exposed_meters": shade["estimated_exposed_meters"],
                        "unknown_meters": shade["unknown_meters"], "latency_ms": latency}
    if cpu_before is not None and cpu_after is not None:
        cpu_ms = round((cpu_after - cpu_before) / 1000, 2)
        results["shade"]["container_cpu_ms_during_request"] = cpu_ms
        results["shade"]["container_cpu_percent_during_request"] = round(cpu_ms / max(latency, 0.01) * 100, 2)
    outside = {"departure_at": payload["departure_at"], "legs": [{
        "geometry": [{"latitude": 59.8800, "longitude": 30.3400},
                     {"latitude": 59.8810, "longitude": 30.3400}],
        "walking_duration_seconds": 300}]}
    status, uncovered, _ = request(base_url, "/api/v1/environment/shade/analyze", outside)
    require(status == 200 and uncovered["status"] == "outside_coverage", "Unmapped route status incorrect")
    require(uncovered["coverage_metrics"]["coverage_share"] == 0 and
            uncovered["estimated_shaded_meters"] == 0 and uncovered["estimated_exposed_meters"] == 0 and
            abs(uncovered["unknown_meters"] - uncovered["route_length_meters"]) <= 0.1,
            "Unmapped ground must be entirely unknown")
    results["shade"]["outside_coverage"] = "unknown_only"

    repeat_metrics = {"count": 3, "latency_ms": [latency]}
    if cpu_before is not None and cpu_after is not None:
        repeat_metrics["container_cpu_ms_during_request"] = [cpu_ms]
    length_keys = ("estimated_shaded_meters", "estimated_exposed_meters", "unknown_meters")
    for _ in range(2):
        cpu_start = cpu_usage_usec() if cpu_usage_usec else None
        status, repeated, repeat_latency = request(base_url, "/api/v1/environment/shade/analyze", payload)
        cpu_end = cpu_usage_usec() if cpu_usage_usec else None
        require(status == 200 and repeated["method_version"] == "c7_1.2" and
                repeated["dataset_version"] == "c7_1_pilot_v1", "Repeated real pilot shade contract failed")
        require([repeated[key] for key in length_keys] == [shade[key] for key in length_keys],
                "Repeated shade three-state lengths changed")
        repeat_metrics["latency_ms"].append(repeat_latency)
        if cpu_start is not None and cpu_end is not None:
            repeat_metrics["container_cpu_ms_during_request"].append(round((cpu_end - cpu_start) / 1000, 2))
    results["shade"]["repeat_metrics"] = repeat_metrics

    denied = ("/.env", "/.env.dev", "/data", "/data/", "/smart_navigator_spb.db",
              "/data/smart_navigator_spb.db", "/data/shade_buildings_spb_pilot.sqlite",
              "/docs", "/redoc", "/openapi.json")
    for path in denied:
        status, _, _ = request(base_url, path)
        require(status == 404, f"Sensitive/public documentation path exposed: {path}")
    oversized = b'{"padding":"' + b"x" * 16384 + b'"}'
    status, _, _ = request(base_url, "/api/v1/environment/shade/analyze", raw_body=oversized)
    require(status == 413, "Requests larger than 16384 bytes must be rejected explicitly")
    status, _, _ = request(base_url, "/api/v1/search/suggest?text=A5_NO_NETWORK_CHECK")
    require(status == 503, "Suggest must fail safely without a configured server key")
    results["security"] = {"denied_paths": len(denied), "oversized_body": 413,
                           "disabled_yandex": 503}
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = check_http(args.base_url)
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
