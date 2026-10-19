"""Build the C7.1 pilot building dataset from the cached OSM evidence.

Run from the repository root: python -m scripts.build_phase_c7_1_buildings [--out PATH]
                  [--check-reproducible]

Input: the raw responses cached by ``scripts.acquire_phase_c7_1_osm``
(``docs/phase_c7_1_acquisition/*.osm.xml``) plus their SHA-256 manifest.
Output: one SQLite dataset (default ``data/shade_buildings_spb_pilot.sqlite``)
with an R-tree spatial index, per-building provenance columns, and a JSON
coverage audit report.

HONESTY RULES encoded here:

* Only real, cached OSM responses are read — the builder never fabricates
  geometry and fails loudly if the cache or manifest is missing.
* ``building:part`` polygons are NOT treated as standalone buildings (they
  are components of a parent outline; using both would double-count mass).
  They are counted in the audit as a documented limitation.
* ``building=roof`` features are not vertical prisms and are excluded, counted.
* A height is never guessed: only a parseable OSM ``height`` tag
  (``osm_declared_height``) or ``building:levels`` × a documented 3.3 m
  floor assumption (``levels_estimate``) produces a usable height; everything
  else is stored as ``unavailable`` and casts no shadow.
* Geometry that cannot be turned into a valid ring is stored as
  ``invalid``/``repaired`` status, excluded from shadow casting by the
  provider, and reported in the audit.
"""
from __future__ import annotations

from datetime import datetime, timezone
import argparse
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import xml.etree.ElementTree as ET

from shapely.geometry import Polygon
from shapely.validation import explain_validity

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "docs/phase_c7_1_acquisition"
DEFAULT_OUT = ROOT / "data/shade_buildings_spb_pilot.sqlite"
DEFAULT_AUDIT = ROOT / "docs/PHASE_C7_1_COVERAGE_AUDIT.json"

DATASET_VERSION = "c7_1_pilot_v1"
FLOOR_HEIGHT_ASSUMPTION_M = 3.3   # documented assumption for building:levels
MAX_VERTICES_PER_RING = 2000      # §7 protection against pathological geometries
MIN_POLYGON_AREA_M2 = 4.0         # smaller than a shed: degenerate for shading
RING_CLOSE_TOLERANCE_M = 25.0     # documented: closes way gaps, else invalid
PLAUSIBLE_HEIGHT_RANGE_M = (2.0, 400.0)
MAX_PLAUSIBLE_LEVELS = 60.0
EARTH_RADIUS_METERS = 6_371_000.0

_HEIGHT_RE = re.compile(r"^[~≈]?\s*([0-9]+(?:[.,][0-9]+)?)\s*(?:m|м|meters|metres)?$",
                        re.IGNORECASE)


class DatasetBuildError(RuntimeError):
    """Raised when the cached evidence required for a build is missing."""


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    import math
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = (math.sin(d_phi / 2.0) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2.0) ** 2)
    a = min(1.0, max(0.0, a))
    return EARTH_RADIUS_METERS * 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def parse_height_tag(raw: str | None) -> float | None:
    """OSM ``height`` → meters, or None when absent/unparseable/implausible."""
    if raw is None:
        return None
    match = _HEIGHT_RE.fullmatch(raw.strip())
    if not match:
        return None
    value = float(match.group(1).replace(",", "."))
    low, high = PLAUSIBLE_HEIGHT_RANGE_M
    if not low <= value <= high:
        return None
    return value


def parse_levels_tag(raw: str | None) -> float | None:
    """OSM ``building:levels`` → floors, or None when absent/unparseable."""
    if raw is None:
        return None
    try:
        value = float(raw.strip().replace(",", "."))
    except ValueError:
        return None
    if not 0.0 < value <= MAX_PLAUSIBLE_LEVELS:
        return None
    return value


def classify_height(tags: dict[str, str]) -> tuple[float | None, str, float | None, float | None, str | None]:
    """(height_m, provenance, levels, floor_assumption, note) from OSM tags.

    Only two honest paths exist in the pilot dataset: a parseable declared
    ``height`` tag, or ``building:levels`` × the documented floor assumption.
    """
    height = parse_height_tag(tags.get("height"))
    if height is not None:
        return height, "osm_declared_height", None, None, None
    raw_height = tags.get("height")
    levels = parse_levels_tag(tags.get("building:levels") or tags.get("levels"))
    if levels is not None:
        return (levels * FLOOR_HEIGHT_ASSUMPTION_M, "levels_estimate",
                levels, FLOOR_HEIGHT_ASSUMPTION_M, None)
    note = "height_tag_implausible" if raw_height else None
    return None, "unavailable", None, None, note


def _ring_quality(ring: list[tuple[float, float]]) -> str:
    """'ok' | 'repaired' | 'degenerate' | 'invalid' for one closed ring."""
    if len(ring) < 4:
        return "degenerate"
    poly = Polygon(ring)
    if poly.is_valid:
        return "ok"
    if not poly.is_valid and "Self-intersection" in explain_validity(poly):
        repaired = poly.buffer(0)
        if repaired.is_empty or repaired.geom_type not in ("Polygon", "MultiPolygon"):
            return "invalid"
        return "repaired"
    return "invalid"


def ring_area_m2(ring: list[tuple[float, float]]) -> float:
    """Approximate planar area in m² at the ring's mean latitude."""
    if len(ring) < 4:
        return 0.0
    lat0 = sum(p[0] for p in ring) / len(ring)
    import math
    kx = math.cos(math.radians(lat0))
    pts = [((lon - ring[0][1]) * kx, (lat - ring[0][0])) for lat, lon in ring]
    area = 0.0
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0 * (EARTH_RADIUS_METERS ** 2) * (3.141592653589793 / 180.0) ** 2


def close_ring(points: list[tuple[float, float]]) -> tuple[list[tuple[float, float]], str]:
    """Ensure (lat, lon) ring closure; returns (ring, closure_status)."""
    if len(points) >= 2 and points[0] == points[-1]:
        return points, "closed"
    if len(points) >= 3:
        gap = haversine_m(points[-1][0], points[-1][1], points[0][0], points[0][1])
        if gap <= RING_CLOSE_TOLERANCE_M:
            return points + [points[0]], "closed_by_tolerance"
    return points, "unclosed"


def way_coords(way: ET.Element, nodes: dict[int, tuple[float, float]]) -> list[tuple[float, float]] | None:
    coords = []
    for nd in way.findall("nd"):
        ref = int(nd.get("ref"))
        if ref not in nodes:
            return None  # node outside the bbox — the way is not fully known
        coords.append(nodes[ref])
    return coords


def _chain_rings(refs_list: list[list[int]]) -> tuple[list[list[int]], list[list[int]]]:
    """Assemble closed node-ref rings from multipolygon member ways.

    OSM multipolygon outer/inner rings are routinely split across several
    open member ways (the Winter Palace outline is four open ways). Chaining
    them by shared endpoint node ids reproduces the intended ring; a chain
    that cannot be closed is returned as an open chain and the relation is
    reported as incomplete instead of being half-drawn.
    """
    pool = [list(r) for r in refs_list if len(r) >= 2]
    rings: list[list[int]] = []
    open_chains: list[list[int]] = []
    while pool:
        chain = pool.pop(0)
        progress = True
        while chain and chain[0] != chain[-1] and pool and progress:
            progress = False
            for index, candidate in enumerate(pool):
                if candidate[0] == chain[-1]:
                    chain.extend(candidate[1:])
                elif candidate[-1] == chain[-1]:
                    chain.extend(reversed(candidate[:-1]))
                elif candidate[-1] == chain[0]:
                    chain = candidate[:-1] + chain
                elif candidate[0] == chain[0]:
                    chain = list(reversed(candidate[1:])) + chain
                else:
                    continue
                pool.pop(index)
                progress = True
                break
        if chain and chain[0] == chain[-1] and len(chain) >= 4:
            rings.append(chain)
        else:
            open_chains.append(chain)
    return rings, open_chains


def relation_polygons(
    relation: ET.Element,
    ways_refs: dict[int, list[int]],
    nodes: dict[int, tuple[float, float]],
) -> tuple[list[tuple[list[tuple[float, float]], list[list[tuple[float, float]]]]], str]:
    """(shell/holes pairs, status) for a multipolygon building relation.

    Member ways missing from the bbox cache, or chains that do not close, make
    the relation *incomplete*: it is skipped and counted, never half-drawn.
    Member ways nested as relations are ignored (not used in the pilot).
    """
    outer_refs: list[list[int]] = []
    inner_refs: list[list[int]] = []
    for member in relation.findall("member"):
        if member.get("type") != "way":
            continue
        refs = ways_refs.get(int(member.get("ref")))
        if refs is None:
            return [], "incomplete"
        if member.get("role") == "inner":
            inner_refs.append(refs)
        else:
            outer_refs.append(refs)
    if not outer_refs:
        return [], "no_outer"
    outer_rings, outer_open = _chain_rings(outer_refs)
    if outer_open and not outer_rings:
        return [], "unclosed_outer"
    if not outer_rings:
        return [], "degenerate_outer"
    inner_rings, _inner_open = _chain_rings(inner_refs)

    def to_coords(refs: list[int]) -> list[tuple[float, float]]:
        return [nodes[r] for r in refs]

    pairs = []
    for outer in outer_rings:
        shell_coords = to_coords(outer)
        holes = []
        shell_poly = Polygon(shell_coords)
        for inner in inner_rings:
            inner_coords = to_coords(inner)
            try:
                if Polygon(inner_coords).within(shell_poly):
                    holes.append(inner_coords)
            except ValueError:
                continue
        pairs.append((shell_coords, holes))
    if outer_open:
        return pairs, "partial_open_outer"
    return pairs, "ok"


def build_records(cache_dir: Path = CACHE) -> tuple[list[dict], dict]:
    """Parse every cached zone response into building records + audit counters."""
    manifest_path = cache_dir / "manifest.json"
    if not manifest_path.exists():
        raise DatasetBuildError(
            f"Acquisition manifest missing at {manifest_path}; run "
            "scripts.acquire_phase_c7_1_osm first — the builder never invents data.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    snapshot_date = manifest["generated_at_utc"][:10]

    records: list[dict] = []
    seen: set[tuple[str, int]] = set()
    audit_zones: dict[str, dict] = {}

    for zone in manifest["zones"]:
        zone_id = zone["zone_id"]
        path = cache_dir / zone["file"]
        payload = path.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        if digest != zone["sha256"]:
            raise DatasetBuildError(
                f"{zone['file']} hash mismatch (manifest {zone['sha256'][:12]}, "
                f"file {digest[:12]}): the evidence chain is broken, refusing to build.")
        root = ET.fromstring(payload)

        nodes: dict[int, tuple[float, float]] = {}
        for node in root.findall("node"):
            nodes[int(node.get("id"))] = (float(node.get("lat")), float(node.get("lon")))

        ways: dict[int, list[tuple[float, float]]] = {}
        ways_refs: dict[int, list[int]] = {}
        way_tags: dict[int, dict[str, str]] = {}
        for way in root.findall("way"):
            tags = {t.get("k"): t.get("v") for t in way.findall("tag")}
            way_tags[int(way.get("id"))] = tags
            refs = [int(nd.get("ref")) for nd in way.findall("nd")]
            ways_refs[int(way.get("id"))] = refs
            coords = way_coords(way, nodes)
            if coords is not None:
                ways[int(way.get("id"))] = coords

        counters = {
            "buildings_total": 0, "with_osm_height": 0, "with_verified_height": 0,
            "with_levels_estimate": 0, "without_height": 0, "invalid_geometries": 0,
            "repaired_geometries": 0, "multipolygon_buildings": 0,
            "parts_excluded": 0, "roof_only_excluded": 0, "incomplete_relations": 0,
            "implausible_height_tags": 0, "vertex_count_total": 0,
            "status_counts": {},
        }

        # --- ways ---
        for way in root.findall("way"):
            way_id = int(way.get("id"))
            tags = way_tags.get(way_id, {})
            if "building:part" in tags:
                counters["parts_excluded"] += 1
                continue
            if "building" not in tags or tags.get("building") == "no":
                continue
            if tags.get("building") == "roof":
                counters["roof_only_excluded"] += 1
                continue
            key = ("way", way_id)
            if key in seen:
                continue
            seen.add(key)
            coords = ways.get(way_id)
            if coords is None:
                counters["invalid_geometries"] += 1
                counters["status_counts"]["way_missing_nodes"] = (
                    counters["status_counts"].get("way_missing_nodes", 0) + 1)
                continue
            _append(counters, records, zone_id, "way", way_id, int(way.get("version") or 0),
                    coords, [], tags, snapshot_date)

        # --- multipolygon relations ---
        for relation in root.findall("relation"):
            rel_id = int(relation.get("id"))
            tags = {t.get("k"): t.get("v") for t in relation.findall("tag")}
            if "building" not in tags or tags.get("building") == "no":
                continue
            key = ("relation", rel_id)
            if key in seen:
                continue
            seen.add(key)
            pairs, status = relation_polygons(relation, ways_refs, nodes)
            if status != "ok":
                counters["incomplete_relations"] += 1
                counters["invalid_geometries"] += 1
                counters["status_counts"][f"relation_{status}"] = (
                    counters["status_counts"].get(f"relation_{status}", 0) + 1)
                continue
            counters["multipolygon_buildings"] += 1
            for shell, holes in pairs:
                _append(counters, records, zone_id, "relation", rel_id,
                        int(relation.get("version") or 0), shell, holes, tags,
                        snapshot_date)

        audit_zones[zone_id] = {**counters, "source_sha256": digest,
                                "source_bytes": len(payload)}

    zone_boxes = [{"zone_id": z["zone_id"], "description": z["description"],
                   "bbox": z["bbox_s_w_n_e"]} for z in manifest["zones"]]
    return records, {"snapshot_date": snapshot_date, "manifest": manifest,
                     "zones": audit_zones, "zone_boxes": zone_boxes}


def _append(counters: dict, records: list[dict], zone_id: str, osm_type: str, osm_id: int,
            version: int, shell: list[tuple[float, float]],
            holes: list[list[tuple[float, float]]], tags: dict[str, str],
            snapshot_date: str) -> None:
    """Validate one polygon and append its record (or count it as invalid)."""
    counters["buildings_total"] += 1
    ring, closure = close_ring(shell)
    status = "ok"
    if closure == "unclosed":
        status = "invalid_unclosed"
    elif len(ring) > MAX_VERTICES_PER_RING:
        status = "invalid_too_many_vertices"
    else:
        quality = _ring_quality(ring)
        if quality == "repaired":
            counters["repaired_geometries"] += 1
            status = "repaired"
        elif quality in ("invalid", "degenerate"):
            status = "invalid_self_intersection" if quality == "invalid" else "invalid_degenerate"
        elif ring_area_m2(ring) < MIN_POLYGON_AREA_M2:
            status = "invalid_degenerate"

    usable_holes: list[list[tuple[float, float]]] = []
    if status in ("ok", "repaired"):
        for hole in holes:
            hole_ring, hole_closure = close_ring(hole)
            if hole_closure == "unclosed" or len(hole_ring) > MAX_VERTICES_PER_RING:
                continue
            if _ring_quality(hole_ring) == "invalid":
                continue
            usable_holes.append(hole_ring)

    if status not in ("ok", "repaired"):
        counters["invalid_geometries"] += 1
    counters["status_counts"][status] = counters["status_counts"].get(status, 0) + 1

    height_m, provenance, levels, floor_assumption, note = classify_height(tags)
    if provenance == "unavailable":
        counters["without_height"] += 1
        if note == "height_tag_implausible":
            counters["implausible_height_tags"] += 1
    elif provenance == "osm_declared_height":
        counters["with_osm_height"] += 1
    elif provenance == "levels_estimate":
        counters["with_levels_estimate"] += 1

    lats = [p[0] for p in ring]
    lons = [p[1] for p in ring]
    counters["vertex_count_total"] += len(ring)
    records.append({
        "zone_id": zone_id, "osm_type": osm_type, "osm_id": osm_id,
        "source_version": version, "shell": ring, "holes": usable_holes,
        "min_lat": min(lats), "max_lat": max(lats),
        "min_lon": min(lons), "max_lon": max(lons),
        "height_m": height_m, "height_provenance": provenance,
        "building_levels": levels, "floor_height_assumption": floor_assumption,
        "geometry_source": f"OpenStreetMap {osm_type}/{osm_id} (ODbL)",
        "source_snapshot_date": snapshot_date,
        "geometry_validation_status": status, "vertex_count": len(ring),
    })


SCHEMA = """
CREATE TABLE dataset_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE zones (
    zone_id TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    min_lat REAL NOT NULL, min_lon REAL NOT NULL,
    max_lat REAL NOT NULL, max_lon REAL NOT NULL,
    source_sha256 TEXT NOT NULL,
    source_bytes INTEGER NOT NULL,
    buildings_total INTEGER NOT NULL
);
CREATE TABLE buildings (
    id INTEGER PRIMARY KEY,
    zone_id TEXT NOT NULL,
    osm_type TEXT NOT NULL,
    osm_id INTEGER NOT NULL,
    source_version INTEGER NOT NULL,
    geometry_wkb BLOB NOT NULL,
    min_lat REAL NOT NULL, min_lon REAL NOT NULL,
    max_lat REAL NOT NULL, max_lon REAL NOT NULL,
    height_m REAL,
    height_provenance TEXT NOT NULL,
    building_levels REAL,
    floor_height_assumption REAL,
    geometry_source TEXT NOT NULL,
    source_snapshot_date TEXT NOT NULL,
    geometry_validation_status TEXT NOT NULL,
    vertex_count INTEGER NOT NULL
);
CREATE VIRTUAL TABLE buildings_rtree USING rtree(id, min_lat, max_lat, min_lon, max_lon);
CREATE INDEX idx_buildings_zone ON buildings(zone_id);
"""


def _safe_polygon(shell: list[tuple[float, float]],
                  holes: list[list[tuple[float, float]]]):
    """WKB polygon for storage, from (lat, lon) rings.

    Coordinates are written in the WKB x/y = (lon, lat) convention so the
    stored geometry is a normally-oriented GIS geometry; the provider flips
    them back to (lat, lon) on read. Invalid records keep their ring as a
    degenerate placeholder (they carry validation status 'invalid*' and are
    excluded from shadow casting by the provider — the geometry column is
    diagnostic only for them and must never crash the build).
    """
    def xy(ring):
        pts = [(lon, lat) for lat, lon in ring]
        while len(pts) < 4:
            pts.append(pts[-1] if pts else (0.0, 0.0))
        if pts[0] != pts[-1]:
            pts.append(pts[0])
        return pts

    try:
        return Polygon(xy(shell), [xy(ring) for ring in holes])
    except (ValueError, TypeError):
        base = xy(shell)[0]
        return Polygon([base, (base[0] + 1e-6, base[1]), (base[0], base[1] + 1e-6), base])


def write_dataset(out_path: Path, records: list[dict], meta: dict, audit: dict) -> None:
    from shapely import wkb
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()
    con = sqlite3.connect(out_path)
    try:
        con.executescript(SCHEMA)
        for key, value in meta.items():
            con.execute("INSERT INTO dataset_meta VALUES (?, ?)", (key, json.dumps(value, ensure_ascii=False)))
        con.execute("INSERT INTO dataset_meta VALUES (?, ?)",
                    ("coverage_audit", json.dumps(
                        {k: v for k, v in audit.items() if k != "generated_at_utc"},
                        ensure_ascii=False, sort_keys=True)))
        zone_by_id = {z["zone_id"]: z for z in audit.get("zone_boxes", [])}
        for zone_id, counters in sorted(audit["zones"].items()):
            box = zone_by_id[zone_id]
            con.execute(
                "INSERT INTO zones VALUES (?,?,?,?,?,?,?,?,?)",
                (zone_id, box["description"],
                 box["bbox"][0], box["bbox"][1], box["bbox"][2], box["bbox"][3],
                 counters["source_sha256"], counters["source_bytes"],
                 counters["buildings_total"]))
        for row_id, rec in enumerate(sorted(records, key=lambda r: (r["zone_id"], r["osm_type"], r["osm_id"])), start=1):
            polygon = _safe_polygon(rec["shell"], rec["holes"])
            blob = wkb.dumps(polygon)
            con.execute(
                "INSERT INTO buildings (id, zone_id, osm_type, osm_id, source_version,"
                " geometry_wkb, min_lat, min_lon, max_lat, max_lon, height_m,"
                " height_provenance, building_levels, floor_height_assumption,"
                " geometry_source, source_snapshot_date, geometry_validation_status,"
                " vertex_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row_id, rec["zone_id"], rec["osm_type"], rec["osm_id"], rec["source_version"],
                 blob, rec["min_lat"], rec["min_lon"], rec["max_lat"], rec["max_lon"],
                 rec["height_m"], rec["height_provenance"], rec["building_levels"],
                 rec["floor_height_assumption"], rec["geometry_source"],
                 rec["source_snapshot_date"], rec["geometry_validation_status"],
                 rec["vertex_count"]))
            con.execute("INSERT INTO buildings_rtree VALUES (?,?,?,?,?)",
                        (row_id, rec["min_lat"], rec["max_lat"], rec["min_lon"], rec["max_lon"]))
        con.commit()
        con.execute("VACUUM")
    finally:
        con.close()


def build(out_path: Path = DEFAULT_OUT, audit_path: Path = DEFAULT_AUDIT,
          cache_dir: Path = CACHE) -> dict:
    records, parsed = build_records(cache_dir)
    audit = {
        "dataset_version": DATASET_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "OpenStreetMap via the official OSM API 0.6 map call",
        "license": "ODbL 1.0 — © OpenStreetMap contributors",
        "snapshot_note": ("source_snapshot_date is the acquisition date recorded in "
                          "docs/phase_c7_1_acquisition/manifest.json; per-building OSM "
                          "element versions are stored in source_version"),
        "floor_height_assumption_m": FLOOR_HEIGHT_ASSUMPTION_M,
        "zones": parsed["zones"],
        "zone_boxes": parsed["zone_boxes"],
    }
    totals = {}
    for zone in audit["zones"].values():
        for key, value in zone.items():
            if isinstance(value, int):
                totals[key] = totals.get(key, 0) + value
    totals["buildings_stored"] = len(records)
    totals["invalid_geometries"] = sum(
        1 for r in records if r["geometry_validation_status"] not in ("ok", "repaired"))
    totals["with_verified_height"] = sum(
        1 for r in records if r["height_provenance"] == "externally_verified_height")
    audit["totals"] = totals

    meta = {
        "dataset_version": DATASET_VERSION,
        "source": audit["source"],
        "license": audit["license"],
        "attribution": "© OpenStreetMap contributors (https://www.openstreetmap.org/copyright)",
        "snapshot_date": parsed["snapshot_date"],
        "floor_height_assumption_m": FLOOR_HEIGHT_ASSUMPTION_M,
        "acquisition_manifest_sha256": hashlib.sha256(
            json.dumps(parsed["manifest"], ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest(),
        "zones": sorted(audit["zones"].keys()),
        "buildings_stored": len(records),
    }
    write_dataset(out_path, records, meta, audit)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=1) + "\n",
                          encoding="utf-8")
    print(f"dataset: {len(records)} buildings → {out_path}")
    print(f"audit:   {audit_path}")
    return audit


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--check-reproducible", action="store_true",
                        help="build twice and compare dataset SHA-256")
    args = parser.parse_args(argv)
    build(args.out, args.audit)
    if args.check_reproducible:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            other = Path(tmp) / "repeat.sqlite"
            build(other, Path(tmp) / "audit.json")
            first = hashlib.sha256(args.out.read_bytes()).hexdigest()
            second = hashlib.sha256(other.read_bytes()).hexdigest()
            print(f"reproducible: {'yes' if first == second else 'NO'}")
            print(f"sha256: {first}")
            return 0 if first == second else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
