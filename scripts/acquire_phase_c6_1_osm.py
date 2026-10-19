"""Opt-in, serial, bounded and cached C6.1 OSM evidence acquisition (never startup).

Run from the repository root: python -m scripts.acquire_phase_c6_1_osm
Phase C6.1 extends the frozen 173-record catalog with seven reviewed city
attractions. Each target gets one tight bounded Overpass query around its
documented address so the selected canonical object and its neighbouring
candidates stay reproducible from the retained raw response. Successful raw
responses and exact queries are versioned evidence; failures are logged without
repeatedly trying an unhealthy public endpoint.
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
CACHE = ROOT / "docs/phase_c6_1_acquisition"
CLIENT = "SmartNavigator-SPb-Phase-C6.1/1.0 (bounded development catalog audit; https://github.com/DenisAniskov/SmartNavigator_Spb)"
# Same public gateways as Phase C3: overpass-api.de answered the C3 run, while
# kumi intermittently returned HTTP 500 (logged in the C3 ledger).
ENDPOINTS = (
    ("de", "https://overpass-api.de/api/interpreter"),
    ("mail", "https://maps.mail.ru/osm/tools/overpass/api/interpreter"),
    ("kumi", "https://overpass.kumi.systems/api/interpreter"),
)
ENDPOINT_ALIASES = {alias for alias, _ in ENDPOINTS}

# Tight per-target boxes around the documented addresses (Биржевая линия 14,
# Петропавловская крепость, Шпалерная 56, Лиговский 74, Лиговский 53,
# Елагин остров, пр. Энгельса 154А). Each query keeps the reviewed object
# plus its immediate neighbours in one bounded response.
QUERIES = {
    "c61_probe": '[out:json][timeout:10];node(270902609);out meta;',
    "c61_museum_of_optics": '[out:json][timeout:50];nwr["name"~"птик"](59.9400,30.2820,59.9475,30.2970);out meta center;',
    "c61_glushko_museum": '[out:json][timeout:50];(nwr["name"~"осмонавтик"](59.9455,30.3090,59.9545,30.3250);nwr["tourism"="museum"](59.9455,30.3090,59.9545,30.3250););out meta center;',
    "c61_vselennaya_vody": '[out:json][timeout:50];(nwr["name"~"Вселенная воды"](59.9435,30.3670,59.9505,30.3800);nwr["name"~"одонапорн"](59.9435,30.3670,59.9505,30.3800););out meta center;',
    "c61_etagi": '[out:json][timeout:50];nwr["name"~"[Ээ]тажи"](59.9200,30.3530,59.9240,30.3590);out meta center;',
    "c61_pushkinskaya_10": '[out:json][timeout:50];nwr["name"~"Пушкинская"](59.9265,30.3490,59.9335,30.3610);out meta center;',
    "c61_elagin_park": '[out:json][timeout:50];nwr["name"~"Кирова|Елагин"](59.9690,30.2390,59.9910,30.2770);out meta center;',
    "c61_volshebnaya_milya": '[out:json][timeout:50];nwr["name"~"олшебная"](60.0560,30.3290,60.0610,30.3390);out meta center;',
}


def ledger_path() -> Path:
    return CACHE / "requests.json"


def read_ledger() -> list:
    path = ledger_path()
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def write_ledger(log: list) -> None:
    ledger_path().write_text(json.dumps(log, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def acquire(name: str, endpoint: str, timeout: int, attempt: str = "") -> dict | None:
    # The attempt suffix is part of the response file name: a retry with a
    # corrected query must never be answered from a superseded cached response.
    path = CACHE / f"{name}{attempt}.json"
    if path.exists():
        print(f"{name}{attempt}: cached")
        return json.loads(path.read_text(encoding="utf-8"))
    log = read_ledger()
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
    log = read_ledger()
    log.append(entry)
    write_ledger(log)
    return result


def main() -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    attempt = sys.argv[1] if len(sys.argv) > 1 else ""
    # Public gateways are load-shedding under the audit's request volume; a
    # narrowed rerun (argv[2]: comma-separated targets) retries only the
    # still-missing evidence instead of re-issuing the whole query set.
    targets = QUERIES if len(sys.argv) < 3 else [name for name in sys.argv[2].split(",") if name]
    failures = []
    for name in targets:
        for alias, endpoint in ENDPOINTS:
            result = acquire(f"{name}_{alias}", endpoint, 60, attempt)
            if result is not None:
                break
            time.sleep(3)
        else:
            # One unavailable target must not hide the others: collect and
            # report, then retry the remainder under a fresh attempt suffix.
            failures.append(name)
            print(f"{name}: all endpoints failed; continuing with remaining targets")
        time.sleep(5)
    if failures:
        print(f"Failed targets ({len(failures)}): {', '.join(failures)}")


def acquire_osm_api(name: str, url: str) -> dict | None:
    """Low-volume fallback for exact current-object checks (OSM API 0.6)."""
    path = CACHE / f"{name}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    log = read_ledger()
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
    log = read_ledger()
    log.append(entry)
    write_ledger(log)
    return result


if __name__ == "__main__":
    main()
