"""Offline C3 four-way evaluation. No DB, GPS, routing API or native-time invention.

Run from the repository root: python -m scripts.evaluate_phase_c3
The frozen C2 32-point matrix is preserved; 10 additional synthetic starts
explore the north/west priority districts. Coordinates/catalog/ordered results
are reproducible; native MapKit walking times are NOT_TESTED in this phase.
"""
from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
from statistics import mean, median
from time import perf_counter

from app.services.smart_walk_service import SmartWalkService as V2
from scripts.diagnose_root_causes import FILTERS, BANDS
from scripts.validate_phase_b_catalog import make_places, read_json
from scripts.evaluate_phase_c2 import validate_proposal, catalog_digest

ROOT = Path(__file__).resolve().parents[1]

# New exploratory synthetic starts, two per priority district. They are kept
# separate from the frozen C2 matrix and never replace it.
EXPLORATORY_STARTS = [
    {"id": "c3_primorsky_dolgoe", "district": "Приморский район", "lat": 60.0165, "lon": 30.2650},
    {"id": "c3_primorsky_golovinsky", "district": "Приморский район", "lat": 59.9815, "lon": 30.3210},
    {"id": "c3_kirovsky_narvskaya", "district": "Кировский район", "lat": 59.9010, "lon": 30.2740},
    {"id": "c3_kirovsky_ulyanka", "district": "Кировский район", "lat": 59.8470, "lon": 30.2400},
    {"id": "c3_krasnoselsky_sosnovaya", "district": "Красносельский район", "lat": 59.8310, "lon": 30.1340},
    {"id": "c3_krasnoselsky_ligovo", "district": "Красносельский район", "lat": 59.8240, "lon": 30.1850},
    {"id": "c3_kalininsky_piskaryovka", "district": "Калининский район", "lat": 60.0000, "lon": 30.4070},
    {"id": "c3_kalininsky_finlyandsky", "district": "Калининский район", "lat": 59.9560, "lon": 30.3560},
    {"id": "c3_vyborgsky_polyustrovo", "district": "Выборгский район", "lat": 59.9880, "lon": 30.3510},
    {"id": "c3_vyborgsky_ozerki", "district": "Выборгский район", "lat": 60.0600, "lon": 30.3300},
]


def evaluate():
    c2_catalog = read_json(ROOT / "docs/phase_c2_catalog_baseline.json")
    expanded = read_json(ROOT / "app/db/seed_places.json")
    assert expanded[:126] == c2_catalog and len(c2_catalog) == 126

    matrix = read_json(ROOT / "docs/phase_c2_test_starts.json")
    frozen_starts = matrix["starts"]
    assert len(frozen_starts) == 32 and len({s["id"] for s in frozen_starts}) == 32
    starts = frozen_starts + EXPLORATORY_STARTS
    assert len({s["id"] for s in starts}) == len(starts) == 42

    # Four-way comparison. The C3 routing-point substitution has zero entries
    # in Phase C3 (no verified public entrances), so those cells are computed
    # by the same mechanism and recorded as degenerate rather than invented.
    routing_points = {i["catalog_key"]: i for i in expanded if i.get("routing_latitude") is not None}

    def with_routing_points(catalog):
        substituted = []
        for item in catalog:
            entry = routing_points.get(item["catalog_key"])
            if entry:
                item = {**item, "latitude": entry["routing_latitude"],
                        "longitude": entry["routing_longitude"]}
            substituted.append(item)
        return substituted

    cells = [
        ("c2catalog_v2", c2_catalog, False),
        ("c3catalog_v2", expanded, False),
        ("c2catalog_v2_c3routing", with_routing_points(c2_catalog), True),
        ("c3catalog_v2_c3routing", with_routing_points(expanded), True),
    ]

    cases = []
    timings = defaultdict(list)
    for cell, catalog, uses_routing in cells:
        places = make_places(catalog)
        for start in starts:
            for budget in (60, 120, 240):
                for filter_id, categories in FILTERS.items():
                    before = perf_counter()
                    proposal = V2.propose_walk(start["lat"], start["lon"], budget, places,
                                               categories=categories, prefer_unusual=True)
                    timings[cell].append((perf_counter() - before) * 1000)
                    repeated = V2.propose_walk(start["lat"], start["lon"], budget,
                                               list(reversed(places)),
                                               categories=categories, prefer_unusual=True)
                    assert proposal == repeated, (cell, start["id"], budget, filter_id)
                    by_id, distance, visits, seconds = validate_proposal(
                        proposal, places, start, budget, categories)
                    ids = proposal["suggested_places_sequence"]
                    first = by_id[ids[0]] if ids else None
                    first_m = (calculate(start["lat"], start["lon"], first.latitude, first.longitude)
                               if first else None)
                    alternatives = proposal.get("alternatives", [])
                    assert len(alternatives) <= 2
                    sets = {frozenset(ids)}
                    for alternative in alternatives:
                        validate_proposal(alternative, places, start, budget, categories)
                        identity_set = frozenset(alternative["suggested_places_sequence"])
                        assert identity_set and identity_set not in sets
                        sets.add(identity_set)
                    eligible = [p for p in places
                                if not categories or p.category.value in categories]
                    bands = {str(b): sum(calculate(start["lat"], start["lon"], p.latitude, p.longitude) <= b
                                         for p in eligible) for b in BANDS}
                    cases.append({
                        "cell": cell, "point_id": start["id"], "district": start["district"],
                        "start_dataset": "frozen_c2_matrix" if not start["id"].startswith("c3_")
                        else "c3_exploratory",
                        "synthetic_start": [start["lat"], start["lon"]], "budget": budget,
                        "filter_id": filter_id, "categories": categories,
                        "eligible_count": len(eligible), "eligible_within_haversine_m": bands,
                        "status": proposal["status"],
                        "reason_code": proposal.get("reason_code"),
                        "stop_count": len(ids),
                        "first_haversine_m": round(first_m, 1) if first_m is not None else None,
                        "preliminary_haversine_m": round(distance, 1),
                        "editorial_visit_minutes": visits,
                        "preliminary_total_minutes": proposal["estimated_total_duration_minutes"],
                        "alternative_count": len(alternatives),
                        "short_walk_success": bool(ids) and budget == 60,
                        "routing_points_applied": sum(
                            1 for i in ids
                            if by_id[i].catalog_key in routing_points) if uses_routing else None,
                        "mapkit_status": "NOT_TESTED", "mapkit_walking_seconds": None,
                    })

    def summary(rows):
        success = [r for r in rows if r["status"] == "success"]
        return {"scenarios": len(rows), "success": len(success), "empty": len(rows) - len(success),
                "one_stop": sum(r["stop_count"] == 1 for r in success),
                "mean_stops_success": round(mean(r["stop_count"] for r in success), 3) if success else None,
                "median_first_preliminary_transfer_minutes_success":
                    round(median(r["first_haversine_m"] / 75 / 60 for r in success), 3) if success else None,
                "first_within_1km": sum(r["first_haversine_m"] <= 1000 for r in success),
                "native_status": "NOT_TESTED"}

    cell_names = [c[0] for c in cells]
    datasets = {"frozen_c2_matrix": frozen_starts, "c3_exploratory": EXPLORATORY_STARTS}
    summaries = {cell: {"overall": summary([r for r in cases if r["cell"] == cell]),
                        "frozen_matrix": summary([r for r in cases if r["cell"] == cell
                                                  and r["start_dataset"] == "frozen_c2_matrix"]),
                        "exploratory": summary([r for r in cases if r["cell"] == cell
                                                and r["start_dataset"] == "c3_exploratory"]),
                        "districts": {d: summary([r for r in cases if r["cell"] == cell
                                                  and r["district"] == d])
                                      for d in sorted({s["district"] for s in starts})},
                        "filters": {f: summary([r for r in cases if r["cell"] == cell
                                                and r["filter_id"] == f]) for f in FILTERS}}
                 for cell in cell_names}
    coverage = {label: {dataset: {str(b): sum(any(calculate(s["lat"], s["lon"],
                                                             p["latitude"], p["longitude"]) <= b
                                               for p in catalog) for s in starts_data) for b in BANDS}
                        for dataset, starts_data in datasets.items()}
                for label, catalog in (("c2_126", c2_catalog), ("c3_173", expanded))}
    timing = {cell: {"median_ms": round(median(values), 3), "max_ms": round(max(values), 3)}
              for cell, values in timings.items()}
    return {
        "base_commit": "d5531552cceb398944dd646414768c0378eea8ca",
        "c2_count": len(c2_catalog), "c3_count": len(expanded),
        "start_count": len(starts),
        "frozen_matrix_count": len(frozen_starts),
        "exploratory_count": len(EXPLORATORY_STARTS),
        "cases_per_cell": len(cases) // len(cell_names),
        "method": ("Haversine /75 m/min plus editorial visits; synthetic starts; "
                   "not street distances and not native MapKit times"),
        "four_way_note": ("C3 verified routing points: 0 records carry them, so the "
                          "routing cells are degenerate copies of their catalog cells; "
                          "native MapKit validation is NOT_TESTED and no walking-time "
                          "improvement is claimed."),
        "routing_points_available": len(routing_points),
        "start_dataset_sha256": hashlib.sha256(
            (ROOT / "docs/phase_c2_test_starts.json").read_bytes()).hexdigest(),
        "c2_catalog_sha256": catalog_digest(c2_catalog),
        "c3_catalog_sha256": catalog_digest(expanded),
        "algorithm_source_sha256": source_digests(),
        "repeatability": "Every case repeated with reversed input order; exact proposal equality",
        "coverage": coverage, "timing": timing, "summaries": summaries, "cases": cases,
    }


def calculate(lat1, lon1, lat2, lon2):
    from scripts.phase_c1_smart_walk_baseline import calculate_haversine_distance
    return calculate_haversine_distance(lat1, lon1, lat2, lon2)


def source_digests():
    def digest(path):
        return hashlib.sha256(path.read_text(encoding="utf-8-sig").encode("utf-8")).hexdigest()
    return {"v2": digest(ROOT / "app/services/smart_walk_service.py")}


def main():
    result = evaluate()
    (ROOT / "docs/phase_c3_geographic_evaluation.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for cell, data in result["summaries"].items():
        overall = data["overall"]
        print(f"{cell}: success {overall['success']}/{overall['scenarios']}, "
              f"mean stops {overall['mean_stops_success']}, "
              f"first_within_1km {overall['first_within_1km']}")
    print("frozen vs exploratory (c3catalog_v2):",
          result["summaries"]["c3catalog_v2"]["frozen_matrix"],
          result["summaries"]["c3catalog_v2"]["exploratory"])


if __name__ == "__main__":
    main()
