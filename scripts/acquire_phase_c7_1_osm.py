"""Opt-in, serial, bounded and cached C7.1 building-footprint acquisition.

Run from the repository root: python -m scripts.acquire_phase_c7_1_osm [--verify]

Phase C7.1 needs REAL building footprint geometry with auditable provenance
for a small pilot set of Saint Petersburg zones (never the whole city in one
request, never synthetic data). The acquisition channel and its limits are
part of the evidence:

* Public Overpass instances were probed first (Phase C3 discipline). In this
  environment they answered with HTTP 403/406/500/504 or stalled mid-stream
  on bounded building queries; every attempt is recorded in ``requests.json``
  and repeated attempts to an unhealthy endpoint are suppressed.
* The bounded official OpenStreetMap API ``/api/0.6/map?bbox=`` call — the
  primary OSM data endpoint, not a mirror — answered reliably for every pilot
  zone in seconds and is therefore used as the acquisition channel. It is the
  same OpenStreetMap source data, retrieved through the official API.

Successful raw responses are retained as versioned evidence with SHA-256
hashes (``manifest.json``); nothing is retried blindly and nothing is
invented. Run with ``--verify`` to re-check the cached files against the
manifest hashes without any network access.
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
CACHE = ROOT / "docs/phase_c7_1_acquisition"
CLIENT = ("SmartNavigator-SPb-Phase-C7-1/1.0 "
          "(bounded pilot building-footprint acquisition; "
          "https://github.com/DenisAniskov/SmartNavigator_Spb)")
OVERPASS_ENDPOINTS = (
    ("de", "https://overpass-api.de/api/interpreter"),
    ("lz4", "https://lz4.overpass-api.de/api/interpreter"),
    ("mail", "https://maps.mail.ru/osm/tools/overpass/api/interpreter"),
    ("kumi", "https://overpass.kumi.systems/api/interpreter"),
)
OSM_API_MAP = "https://api.openstreetmap.org/api/0.6/map"

# One trivial probe per Overpass instance: proof of reachability and of the
# public gateways' state at acquisition time. The probe queries are tiny and
# bounded; the C3 pattern is reused verbatim so the two phases stay
# comparable.
OVERPASS_PROBES = {
    "probe_node": '[out:json][timeout:10];node(270902609);out meta;',
    "probe_ways": ('[out:json][timeout:25];way["building"]'
                   '(59.9365,30.3155,59.9375,30.3175);out geom meta;'),
}

# ---------------------------------------------------------------------------
# Pilot zones (C7.1 §6.1 priority list): historic centre, Admiralteyskaya
# part, Petrograd side, Vasilyevsky island, plus a control area outside the
# historic centre. Each box is ≈0.9 km × 0.95 km — small enough for a single
# polite official-API request (the node limit is 50 000; observed counts are
# well below), large enough to contain courtyards, embankments, straight
# streets and open squares. Deliberately NOT the whole city.
# ---------------------------------------------------------------------------
ZONES: dict[str, dict] = {
    "centre_palace": {
        "description": ("Пилот 1 — Исторический центр: Дворцовая площадь и Эрмитаж "
                        "(плотная застройка, дворы, открытая площадь)"),
        "bbox": (59.9358, 30.3075, 59.9438, 30.3245),  # s, w, n, e
    },
    "centre_nevsky": {
        "description": ("Пилот 2 — Исторический центр: Невский проспект у Гостиного двора "
                        "(прямая улица-коридор, непрерывный фронт застройки)"),
        "bbox": (59.9305, 30.3265, 59.9385, 30.3435),
    },
    "admiralteysky": {
        "description": ("Пилот 3 — Адмиралтейская часть: Исаакиевский собор и окрестные кварталы "
                        "(дворы-колодцы, разновысотная застройка)"),
        "bbox": (59.9280, 30.2975, 59.9360, 30.3145),
    },
    "petrogradsky": {
        "description": ("Пилот 4 — Петроградская сторона: Петропавловская крепость и Аврора "
                        "(набережные, крепостные стены, зелень)"),
        "bbox": (59.9540, 30.3085, 59.9620, 30.3255),
    },
    "vasileostrovsky": {
        "description": ("Пилот 5 — Васильевский остров: Университетская набережная и Стрелка "
                        "(набережная, широкие проспекты, исторические корпуса)"),
        "bbox": (59.9380, 30.2900, 59.9460, 30.3070),
    },
    "control_kupchino": {
        "description": ("Контрольная территория вне исторического центра: Купчино "
                        "(типовая жилая застройка, свободная планировка)"),
        "bbox": (59.8490, 30.3915, 59.8570, 30.4085),
    },
}


def _log_path() -> Path:
    return CACHE / "requests.json"


def _read_log() -> list[dict]:
    path = _log_path()
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def _write_log(entries: list[dict]) -> None:
    _log_path().write_text(
        json.dumps(entries, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def _already_attempted(log: list[dict], query_id: str) -> bool:
    return any(entry["query_id"] == query_id for entry in log)


def _record(entry: dict) -> None:
    log = _read_log()
    log.append(entry)
    _write_log(log)


def _get(url: str, timeout: int) -> tuple[bytes, int]:
    request = urllib.request.Request(url, headers={
        "User-Agent": CLIENT, "Accept": "application/xml, application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(), response.status


def probe_overpass(reprobe: bool = False) -> None:
    """One bounded probe per public Overpass instance; failures are evidence.

    With ``reprobe=True`` a second labeled round is attempted and the response
    bodies of successful probes are cached — a successful Overpass answer for
    the small probe box is independent cross-channel evidence that can be
    compared against the official-API dataset for the same box.
    """
    log = _read_log()
    suffix = "_r2" if reprobe else ""
    for alias, endpoint in OVERPASS_ENDPOINTS:
        for probe_name, query in OVERPASS_PROBES.items():
            query_id = f"c7_1_{probe_name}_{alias}{suffix}"
            if not reprobe and _already_attempted(log, query_id):
                print(f"{query_id}: prior attempt already logged")
                continue
            if reprobe and _already_attempted(log, query_id):
                print(f"{query_id}: already reprobed")
                continue
            encoded = urllib.parse.urlencode({"data": query})
            entry = {
                "query_id": query_id, "endpoint": endpoint, "query": query,
                "requested_at_utc": datetime.now(timezone.utc).isoformat(),
                "client": CLIENT, "client_timeout_seconds": 45,
                "concurrency": 1, "method": "GET",
            }
            try:
                body, status = _get(f"{endpoint}?{encoded}", timeout=45)
                entry["status"] = "ok" if status == 200 else f"http_{status}"
                entry["response_bytes"] = len(body)
                if status == 200 and reprobe:
                    target = CACHE / f"probe_{probe_name}_{alias}{suffix}.json"
                    target.write_bytes(body)
                    entry["cached_file"] = target.name
                    entry["sha256"] = hashlib.sha256(body).hexdigest()
            except urllib.error.HTTPError as exc:
                entry["status"] = "failed"
                entry["error"] = f"HTTP Error {exc.code}: {exc.reason}"
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                entry["status"] = "failed"
                entry["error"] = f"{type(exc).__name__}: {exc}"
            _record(entry)
            print(f"{query_id}: {entry['status']} {entry.get('error', '')}".strip())


def acquire_zone(zone_id: str, *, rate_delay_s: float = 1.5) -> bool:
    """One bounded official-OSM-API map call per zone, cached and hashed."""
    zone = ZONES[zone_id]
    target = CACHE / f"{zone_id}.osm.xml"
    if target.exists():
        print(f"{zone_id}: cached")
        return True
    s, w, n, e = zone["bbox"]
    url = (f"{OSM_API_MAP}?bbox={w:.4f},{s:.4f},{e:.4f},{n:.4f}")
    entry = {
        "query_id": f"c7_1_map_{zone_id}", "endpoint": OSM_API_MAP,
        "query": f"bbox={w:.4f},{s:.4f},{e:.4f},{n:.4f}",
        "description": zone["description"],
        "requested_at_utc": datetime.now(timezone.utc).isoformat(),
        "client": CLIENT, "client_timeout_seconds": 120,
        "concurrency": 1, "method": "GET",
    }
    try:
        body, status = _get(url, timeout=120)
        if status != 200:
            entry["status"] = f"http_{status}"
            entry["error"] = f"unexpected status {status}"
            _record(entry)
            print(f"{zone_id}: http_{status} — not cached")
            return False
        target.write_bytes(body)
        entry["status"] = "ok"
        entry["response_bytes"] = len(body)
        entry["sha256"] = hashlib.sha256(body).hexdigest()
        _record(entry)
        print(f"{zone_id}: ok {len(body)} bytes")
        time.sleep(rate_delay_s)  # official API etiquette: ≤1 heavy call/s
        return True
    except urllib.error.HTTPError as exc:
        entry["status"] = "failed"
        entry["error"] = f"HTTP Error {exc.code}: {exc.reason}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        entry["status"] = "failed"
        entry["error"] = f"{type(exc).__name__}: {exc}"
    _record(entry)
    print(f"{zone_id}: failed — {entry['error']}")
    return False


def write_manifest() -> None:
    entries = []
    for zone_id in ZONES:
        target = CACHE / f"{zone_id}.osm.xml"
        if not target.exists():
            continue
        payload = target.read_bytes()
        entries.append({
            "zone_id": zone_id,
            "description": ZONES[zone_id]["description"],
            "bbox_s_w_n_e": list(ZONES[zone_id]["bbox"]),
            "file": target.name,
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        })
    manifest = {
        "phase": "C7.1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "OpenStreetMap via the official OSM API 0.6 map call",
        "source_endpoint": OSM_API_MAP,
        "client": CLIENT,
        "license": "Open Database License (ODbL) 1.0 — © OpenStreetMap contributors",
        "attribution": "© OpenStreetMap contributors (https://www.openstreetmap.org/copyright)",
        "acquisition_channel_note": (
            "Public Overpass instances were probed and failed in this environment "
            "(see requests.json); the bounded official OSM API map call is the "
            "acquisition channel. Same OpenStreetMap source data, official endpoint."
        ),
        "zones": entries,
    }
    (CACHE / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"manifest: {len(entries)} zones")


def verify_manifest() -> int:
    manifest_path = CACHE / "manifest.json"
    if not manifest_path.exists():
        print("manifest missing")
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    failures = 0
    for zone in manifest["zones"]:
        target = CACHE / zone["file"]
        if not target.exists():
            print(f"{zone['zone_id']}: MISSING")
            failures += 1
            continue
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        ok = digest == zone["sha256"]
        print(f"{zone['zone_id']}: {'ok' if ok else 'HASH MISMATCH'}")
        failures += 0 if ok else 1
    return 1 if failures else 0


def main(argv: list[str]) -> int:
    CACHE.mkdir(parents=True, exist_ok=True)
    if "--verify" in argv:
        return verify_manifest()
    if "--reprobe" in argv:
        probe_overpass(reprobe=True)
        return 0
    probe_overpass()
    for zone_id in ZONES:
        acquire_zone(zone_id)
    write_manifest()
    return verify_manifest()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
