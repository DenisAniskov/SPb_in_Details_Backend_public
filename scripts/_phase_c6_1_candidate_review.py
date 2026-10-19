"""Shared C6.1 selection/evidence helpers for the build and validation scripts.

The seven Phase C6.1 additions are an explicit editorial review of places named
by the project's reference directories, each bound to one canonical OSM object
acquired (or re-acquired) on 2026-10-11. Address and operating status were
additionally checked against the attractions' official sources; the machine-
readable record of those checks lives in docs/phase_c6_1_source_verification.json.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "docs/phase_c6_1_acquisition"
CHECKED_ON = "2026-10-11"
CLIENT = "SmartNavigator-SPb-Phase-C6.1/1.0 (bounded development catalog audit; https://github.com/DenisAniskov/SmartNavigator_Spb)"
PRIOR_CATALOG_COUNT = 173

# Final audited queries: the binding ledger success for each target must carry
# this exact text. Query ids are <base>_<endpoint alias>[_retry<n>].
ACQUISITION_QUERIES = (
    "c61_museum_of_optics", "c61_glushko_museum", "c61_vselennaya_vody",
    "c61_etagi", "c61_pushkinskaya_10", "c61_elagin_park", "c61_volshebnaya_milya",
)
ENDPOINT_ALIASES = ("_de", "_mail", "_kumi")

# key -> (type, id): the canonical OSM object bound to each new catalog record.
SELECTED = {
    "museum-of-optics-itmo": ("node", 1938744254),
    "glushko-space-museum": ("node", 501596014),
    "vselennaya-vody": ("node", 3937153654),
    "etagi-loft-project": ("way", 56077126),
    "pushkinskaya-10-art-centre": ("node", 5844613928),
    "elagin-island-central-park": ("relation", 1445800),
    "volshebnaya-milya-park": ("node", 9961754793),
}
# Objects deliberately not accepted although they appeared among the candidates
# of the same acquisition responses.
NOT_ACCEPTED = {
    ("node", 6533612689): "«Арт-центр Пушкинская 10» — второй узел того же арт-центра; принят более полный канонический объект node/5844613928 (amenity=arts_centre, wikidata, wikipedia).",
    ("node", 12481595032): "«Музей истории Петропавловской крепости» — не входил в список семи мест; отдельно не добавлен.",
    ("node", 3702115489): "«Елагин дворец» — уже представлен в каталоге записью yelagin-palace-museum (way/717881701); повторно не добавляется.",
}


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def base_of(query_id: str) -> str:
    stable = query_id.split("_retry", 1)[0]
    for alias in ENDPOINT_ALIASES:
        if stable.endswith(alias):
            return stable[: -len(alias)]
    return stable


def ledger() -> list:
    return read_json(CACHE / "requests.json")


def binding_successes(queries: dict[str, str]) -> dict[str, dict]:
    """Latest successful ledger entry per target, fully bound to its evidence.

    The binding entry must carry the final audited query text, so a superseded
    attempt (wrong box, older text) can never silently become the evidence.
    """
    log = ledger()
    bindings: dict[str, dict] = {}
    for entry in log:
        base = base_of(entry.get("query_id", ""))
        if base in queries and entry.get("status") == "success":
            # Ledger is append-ordered: the latest success binds the evidence.
            bindings[base] = entry
    if set(bindings) != set(queries):
        missing = sorted(set(queries) - set(bindings))
        raise ValueError(f"C6.1 acquisition lacks successful targets: {missing}")
    for base, entry in bindings.items():
        path = CACHE / f"{entry['query_id']}.json"
        raw = read_json(path)
        if entry.get("query") != queries[base]:
            raise ValueError(f"{base}: binding success predates the final audited query text")
        if entry.get("sha256") != file_sha256(path):
            raise ValueError(f"{base}: acquisition hash invalid")
        if entry.get("element_count") != len(raw.get("elements", [])):
            raise ValueError(f"{base}: acquisition element count mismatch")
        if (entry.get("method") != "GET" or entry.get("concurrency") != 1
                or entry.get("client") != CLIENT
                or entry.get("requested_at_utc", "")[:10] != CHECKED_ON
                or not entry.get("client_timeout_seconds", 999) <= 60
                or not entry.get("endpoint", "").startswith("https://")
                or raw.get("remark")):
            raise ValueError(f"{base}: acquisition binding invalid")
        entry["response_file"] = f"{entry['query_id']}.json"
    return bindings


def acquisition_queries() -> dict[str, str]:
    from scripts.acquire_phase_c6_1_osm import QUERIES
    return {base: QUERIES[base] for base in ACQUISITION_QUERIES}


def per_query_data() -> tuple[dict[str, dict], dict[str, str]]:
    """(elements by canonical identity per query, osm3s base timestamp per query)."""
    bindings = binding_successes(acquisition_queries())
    elements: dict[str, dict] = {}
    timestamps: dict[str, str] = {}
    for base, entry in bindings.items():
        raw = read_json(CACHE / entry["response_file"])
        elements[base] = {(e["type"], e["id"]): e for e in raw["elements"]}
        timestamps[base] = raw["osm3s"]["timestamp_osm_base"]
    return elements, timestamps


def selected_sources() -> tuple[dict[tuple[str, int], dict], dict[str, str]]:
    """The seven selected elements with the query that delivered each of them."""
    elements, timestamps = per_query_data()
    sources: dict[tuple[str, int], dict] = {}
    query_of: dict[tuple[str, int], str] = {}
    for identity in SELECTED.values():
        for base, per_query in elements.items():
            if identity in per_query:
                sources[identity] = per_query[identity]
                query_of[identity] = base
                break
        else:
            raise ValueError(f"{identity}: selected object absent from C6.1 acquisition evidence")
    return sources, query_of


def boundaries() -> dict:
    """Frozen Phase B + C2 district boundaries; no new boundaries are acquired."""
    base = read_json(ROOT / "docs/phase_b_boundary_evidence.json")
    extra = read_json(ROOT / "docs/phase_c2_boundary_evidence.json")
    return {**base, "boundary_count": base["boundary_count"] + extra["boundary_count"],
            "boundaries": base["boundaries"] + extra["boundaries"]}


def merged_snapshot() -> dict:
    """Union of every phase snapshot plus the C6.1 additions."""
    old = read_json(ROOT / "docs/osm_source_snapshot.json")
    c2 = read_json(ROOT / "docs/phase_c2_osm_source_snapshot.json")
    c3 = read_json(ROOT / "docs/phase_c3_osm_source_snapshot.json")
    new = read_json(ROOT / "docs/phase_c6_1_osm_source_snapshot.json")
    return {**old, "elements": old["elements"] + c2["elements"] + c3["elements"] + new["elements"]}
