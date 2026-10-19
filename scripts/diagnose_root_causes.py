"""Offline C1 diagnostics: synthetic starts only; no GPS/network/database.

Run: python -m scripts.diagnose_root_causes
Native result is NOT_TESTED, never an invented multiplier or street distance.
"""
import json
import math
from collections import Counter
from pathlib import Path
from scripts.phase_c1_smart_walk_baseline import SmartWalkService, calculate_haversine_distance
from scripts.validate_phase_b_catalog import make_places

ROOT = Path(__file__).resolve().parents[1]
STARTS = (
    ("nevsky_phase_b", "Невский район", 59.877, 30.447),
    ("nevsky_bolshevikov", "Невский район", 59.919, 30.468),
    ("nevsky_dybenko", "Невский район", 59.907, 30.483),
    ("nevsky_rybatskoye", "Невский район", 59.831, 30.500),
    ("nevsky_elizarovskaya", "Невский район", 59.897, 30.423),
    ("frunzensky_mezhdunarodnaya", "Фрунзенский район", 59.870, 30.379),
    ("frunzensky_kupchino", "Фрунзенский район", 59.829, 30.375),
    ("primorsky_komendantsky", "Приморский район", 60.008, 30.252),
    ("primorsky_begovaya", "Приморский район", 59.987, 30.201),
    ("kirovsky_avtovo", "Кировский район", 59.867, 30.261),
    ("krasnogvardeysky_ladozhskaya", "Красногвардейский район", 59.932, 30.440),
    ("central_phase_b", "Центральная часть города", 59.939, 30.3158),
)
FILTERS = {"all": [], "nature": ["nature"], "history": ["history_architecture"],
           "mixed": ["nature", "history_architecture", "art"]}
BANDS = (500, 1000, 2000, 3000, 5000)

def diagnose():
    catalog = json.loads((ROOT / "docs/phase_c1_catalog_baseline.json").read_text(encoding="utf-8-sig"))
    places = make_places(catalog)
    cases = []
    for point_id, district, lat, lon in STARTS:
        for filter_id, categories in FILTERS.items():
            eligible = [p for p in places if not categories or p.category.value in categories]
            nearby = sorted(((calculate_haversine_distance(lat, lon, p.latitude, p.longitude), p)
                             for p in eligible), key=lambda pair: (pair[0], pair[1].id))
            for budget in (60, 120, 240):
                result = SmartWalkService.propose_walk(lat, lon, budget, places,
                                                       categories=categories, prefer_unusual=True)
                feasible = [(d, p) for d, p in nearby
                            if math.ceil((d / 75 + p.recommended_visit_minutes) * 60) <= budget * 60]
                reason = None
                if not result["items"]:
                    if not eligible:
                        reason = "category_filter_empty" if categories else "no_eligible_places"
                    elif all(p.recommended_visit_minutes >= budget for p in eligible):
                        reason = "editorial_duration_exhausts_budget"
                    else:
                        reason = "preliminary_budget_infeasible"
                cases.append({
                    "point_id": point_id, "district_label": district, "synthetic": True,
                    "start": [lat, lon], "time_budget_minutes": budget, "filter_id": filter_id,
                    "categories": categories, "eligible_count": len(eligible),
                    "eligible_within_haversine_m": {str(b): sum(d <= b for d, _ in nearby) for b in BANDS},
                    "individually_preliminary_feasible_count": len(feasible),
                    "nearest_candidates": [{"catalog_key": p.catalog_key, "haversine_m": round(d, 1),
                                            "preliminary_transfer_minutes": round(d / 75, 3),
                                            "editorial_visit_minutes": p.recommended_visit_minutes}
                                           for d, p in nearby[:5]],
                    "sequence": [places[place_id - 1].catalog_key for place_id in result["suggested_places_sequence"]],
                    "preliminary_haversine_m": result["estimated_total_distance_meters"],
                    "preliminary_transfer_seconds": result["estimated_walking_duration_seconds"],
                    "editorial_visit_minutes": result["estimated_visit_duration_minutes"],
                    "preliminary_total_minutes": result["estimated_total_duration_minutes"],
                    "status": result["status"], "reason_code": reason,
                    "mapkit_status": "NOT_TESTED", "mapkit_walking_seconds": None,
                    "refinement_removed_count": None, "native_final_result": "NOT_TESTED",
                })
    return {"base_commit": "91f11840be20f86af0131ecbe7c8cbdb49883bce", "catalog_count": len(catalog),
            "coordinate_kind_counts": dict(Counter(p["coordinate_kind"] for p in catalog)),
            "method": "C1 frozen service; Haversine /75 m/min; no native route calls",
            "case_count": len(cases), "cases": cases}

if __name__ == "__main__":
    report = diagnose()
    (ROOT / "docs/phase_c2_diagnostics_raw.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"catalog_count": report["catalog_count"], "case_count": report["case_count"],
                      "coordinate_kind_counts": report["coordinate_kind_counts"],
                      "nevsky_all": [{k: c[k] for k in ("point_id", "time_budget_minutes", "status", "sequence",
                                      "preliminary_total_minutes", "eligible_within_haversine_m")}
                                     for c in report["cases"] if c["point_id"].startswith("nevsky_")
                                     and c["filter_id"] == "all"]}, ensure_ascii=False, indent=2))
