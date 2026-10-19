"""Offline four-way evaluation. No DB, GPS, routing API or native-time invention.

Run from the repository root: python -m scripts.evaluate_phase_c2
Runtime measurements vary; coordinates/catalog/ordered results are reproducible.
"""
from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
from statistics import mean, median
from time import perf_counter

from app.services.smart_walk_service import SmartWalkService as V2
from scripts.phase_c1_smart_walk_baseline import SmartWalkService as C1, calculate_haversine_distance
from scripts.validate_phase_b_catalog import make_places
from scripts.diagnose_root_causes import FILTERS, BANDS

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def catalog_digest(catalog):
    canonical = json.dumps(catalog, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def source_digest(path):
    # Portable across Windows autocrlf and Unix checkouts, including BOM removal.
    return hashlib.sha256(path.read_text(encoding="utf-8-sig").encode("utf-8")).hexdigest()


def validate_proposal(proposal, places, start, budget, categories):
    ids = proposal["suggested_places_sequence"]
    assert len(ids) == len(set(ids)) <= 6
    assert [i["place"].id for i in proposal["items"]] == ids
    by_id = {p.id: p for p in places}
    lat, lon = start["lat"], start["lon"]
    distance, visits = 0., 0
    for identity in ids:
        p = by_id[identity]
        assert not categories or p.category.value in categories
        distance += calculate_haversine_distance(lat, lon, p.latitude, p.longitude)
        visits += p.recommended_visit_minutes
        lat, lon = p.latitude, p.longitude
    seconds = math.ceil(distance / 75 * 60)
    assert seconds + visits * 60 <= budget * 60
    assert proposal["estimated_walking_duration_seconds"] == seconds
    assert proposal["estimated_visit_duration_minutes"] == visits
    assert proposal["estimated_total_duration_minutes"] == math.ceil(seconds / 60 + visits)
    assert proposal["estimated_total_distance_meters"] == round(distance, 1)
    return by_id, distance, visits, seconds


def evaluate():
    old = read(ROOT / "docs/phase_c1_catalog_baseline.json")
    expanded = read(ROOT / "app/db/seed_places.json")
    assert expanded[:77] == old and len(old) == 77
    starts_document = read(ROOT / "docs/phase_c2_test_starts.json")
    starts = starts_document["starts"]
    assert len(starts) == 32 and len({s["id"] for s in starts}) == 32
    cases = []
    timings = defaultdict(list)
    for cell, catalog, algorithm in (("old77_c1", old, C1), ("expanded_c1", expanded, C1),
                                     ("old77_v2", old, V2), ("expanded_v2", expanded, V2)):
        places = make_places(catalog)
        for start in starts:
            for budget in (60, 120, 240):
                for filter_id, categories in FILTERS.items():
                    before = perf_counter()
                    proposal = algorithm.propose_walk(start["lat"], start["lon"], budget, places,
                                                      categories=categories, prefer_unusual=True)
                    timings[cell].append((perf_counter() - before) * 1000)
                    repeated = algorithm.propose_walk(start["lat"], start["lon"], budget, list(reversed(places)),
                                                      categories=categories, prefer_unusual=True)
                    assert proposal == repeated, (cell, start["id"], budget, filter_id)
                    by_id, distance, visits, seconds = validate_proposal(proposal, places, start, budget, categories)
                    ids = proposal["suggested_places_sequence"]
                    first = by_id[ids[0]] if ids else None
                    first_m = calculate_haversine_distance(start["lat"], start["lon"],
                                                          first.latitude, first.longitude) if first else None
                    alternatives = proposal.get("alternatives", [])
                    assert len(alternatives) <= 2
                    sets = {frozenset(ids)}
                    for alternative in alternatives:
                        validate_proposal(alternative, places, start, budget, categories)
                        identity_set = frozenset(alternative["suggested_places_sequence"])
                        assert identity_set and identity_set not in sets
                        sets.add(identity_set)
                    eligible = [p for p in places if not categories or p.category.value in categories]
                    bands = {str(b): sum(calculate_haversine_distance(start["lat"], start["lon"],
                                                                      p.latitude, p.longitude) <= b for p in eligible)
                             for b in BANDS}
                    cases.append({"cell": cell, "point_id": start["id"], "district": start["district"],
                                  "synthetic_start": [start["lat"], start["lon"]], "budget": budget,
                                  "filter_id": filter_id, "categories": categories, "eligible_count": len(eligible),
                                  "eligible_within_haversine_m": bands, "status": proposal["status"],
                                  "reason_code": proposal.get("reason_code") or (None if ids else "preliminary_budget_infeasible"),
                                  "sequence": [by_id[i].catalog_key for i in ids], "stop_count": len(ids),
                                  "first_haversine_m": round(first_m, 1) if first_m is not None else None,
                                  "first_preliminary_transfer_seconds": math.ceil(first_m / 75 * 60) if first_m is not None else None,
                                  "preliminary_haversine_m": round(distance, 1),
                                  "preliminary_transfer_seconds": seconds, "editorial_visit_minutes": visits,
                                  "preliminary_total_minutes": proposal["estimated_total_duration_minutes"],
                                  "preliminary_slack_seconds": budget * 60 - seconds - visits * 60 if ids else None,
                                  "distinct_categories": len({by_id[i].category.value for i in ids}),
                                  "alternative_sequences": [[by_id[i].catalog_key for i in a["suggested_places_sequence"]]
                                                            for a in alternatives],
                                  "mapkit_status": "NOT_TESTED", "mapkit_walking_seconds": None,
                                  "refinement_removed_count": None, "native_final_result": "NOT_TESTED",
                                  "native_request_count": None})

    def summary(rows):
        success = [r for r in rows if r["status"] == "success"]
        return {"scenarios": len(rows), "success": len(success), "empty": len(rows) - len(success),
                "one_stop": sum(r["stop_count"] == 1 for r in success),
                "mean_stops_success": round(mean(r["stop_count"] for r in success), 3) if success else None,
                "median_first_preliminary_transfer_minutes_success": round(median(r["first_preliminary_transfer_seconds"] / 60
                                                                                  for r in success), 3) if success else None,
                "first_within_1km": sum(r["first_haversine_m"] <= 1000 for r in success),
                "slack_below_5min": sum(r["preliminary_slack_seconds"] < 300 for r in success),
                "native_status": "NOT_TESTED"}
    cells = sorted({r["cell"] for r in cases})
    summaries = {cell: {"overall": summary([r for r in cases if r["cell"] == cell]),
                        "presets": {str(b): summary([r for r in cases if r["cell"] == cell and r["budget"] == b])
                                    for b in (60, 120, 240)},
                        "districts": {d: {str(b): summary([r for r in cases if r["cell"] == cell and r["district"] == d
                                                          and r["budget"] == b]) for b in (60, 120, 240)}
                                      for d in sorted({s["district"] for s in starts})},
                        "filters": {f: summary([r for r in cases if r["cell"] == cell and r["filter_id"] == f])
                                    for f in FILTERS}}
                 for cell in cells}
    coverage = {label: {str(b): sum(any(calculate_haversine_distance(s["lat"], s["lon"], p["latitude"], p["longitude"]) <= b
                                        for p in catalog) for s in starts) for b in BANDS}
                for label, catalog in (("old77", old), ("expanded", expanded))}
    timing = {cell: {"median_ms": round(median(values), 3), "max_ms": round(max(values), 3)}
              for cell, values in timings.items()}
    return {"base_commit": "91f11840be20f86af0131ecbe7c8cbdb49883bce", "old_count": len(old),
            "expanded_count": len(expanded), "start_count": len(starts), "cases_per_cell": len(cases) // 4,
            "method": "Haversine /75 m/min plus unchanged editorial visits; synthetic starts; not street distances or native times",
            "start_dataset_sha256": hashlib.sha256((ROOT / "docs/phase_c2_test_starts.json").read_bytes()).hexdigest(),
            "original_catalog_sha256": catalog_digest(old),
            "expanded_catalog_sha256": catalog_digest(expanded),
            "algorithm_source_hash_format": "UTF-8 without BOM; normalized LF line endings",
            "algorithm_source_sha256": {
                "c1": source_digest(ROOT / "scripts/phase_c1_smart_walk_baseline.py"),
                "v2": source_digest(ROOT / "app/services/smart_walk_service.py"),
            },
            "repeatability": "Every case repeated with reversed input order; exact proposal equality",
            "invariants": "All main/alternative sequences validated for categories, <=6 unique stops and exact seconds budget",
            "near_start_coverage_out_of_32": coverage, "timing": timing, "summaries": summaries, "cases": cases}


def write_markdown(result):
    summaries = result["summaries"]
    count = result["expanded_count"]
    labels = {"old77_c1": "77 + C1", "expanded_c1": f"{count} + C1",
              "old77_v2": "77 + v2", "expanded_v2": f"{count} + v2"}
    cells = tuple(labels)
    lines = ["# Phase C2 geographic evaluation", "",
             "10 October 2026. Exact base: `91f11840be20f86af0131ecbe7c8cbdb49883bce`.", "",
             "Reproduce: `python -m scripts.evaluate_phase_c2`.",
             "[Complete rows](phase_c2_geographic_evaluation.json), [frozen starts](phase_c2_test_starts.json),",
             "[C1 catalog](phase_c1_catalog_baseline.json), and frozen C1 service in",
             "`scripts/phase_c1_smart_walk_baseline.py` preserve the comparison.", "",
             "JSON also binds the rows to canonical catalog digests and algorithm source hashes",
             "with UTF-8/LF normalization for portable Windows/Unix reproduction.", "",
             "32 starts were frozen before selecting new POIs: five Nevsky, three each in",
             "eight other priority districts and three central regression starts. The central",
             "group includes the old center, Vasilyevsky and Petrogradsky starts. Each has",
             "three unchanged budgets and four filters (all, nature, history/architecture,",
             "mixed nature/history/art): **384 scenarios per cell; 1,536 total**.",
             "All coordinates are synthetic public-area approximations, not surveyed entrances",
             "or physical visits. Every request enables the C1 wizard's unusual preference.", "",
             "Every main and alternative sequence was recomputed and checked for strict",
             "categories, at most six unique stops, exact rounded-up seconds budget, and",
             "identical output on reversed catalog input. Algorithm scores are not empirical",
             "tourist satisfaction. First transfer, stop count/category variety and spare time",
             "are descriptive quality proxies, not proof of street access.", "",
             "**All figures below are Haversine/75 m/min plus editorial visits. They are not",
             "pedestrian distances or native walking times. Real MapKit: NOT_TESTED.**", "",
             "## Four-way feasibility", "",
             "| Catalog + algorithm | Preset budget | Success / scenarios | Empty | One-stop successes | Median first preliminary transfer, min (successes only) |",
             "|---|---:|---:|---:|---:|---:|"]
    for cell in cells:
        for budget in (60, 120, 240):
            s = summaries[cell]["presets"][str(budget)]
            lines.append(f"| {labels[cell]} | {budget} | {s['success']} / {s['scenarios']} | {s['empty']} | {s['one_stop']} | {s['median_first_preliminary_transfer_minutes_success']} |")
    lines += ["", "| Catalog + algorithm | All successes / 384 | Empty | Mean stops among successes | First stop within 1 km | Preliminary slack below 5 min |",
              "|---|---:|---:|---:|---:|---:|"]
    for cell in cells:
        s = summaries[cell]["overall"]
        lines.append(f"| {labels[cell]} | {s['success']} | {s['empty']} | {s['mean_stops_success']} | {s['first_within_1km']} | {s['slack_below_5min']} |")
    lines += ["", "An unchanged catalog cannot gain new individually feasible first destinations",
              "from ranking alone: C1 already finds a feasible singleton when one exists.",
              "Catalog additions account for new preliminary successes. V2 changes ordering,",
              "combination quality and offers alternate POI sets; its extra stops may consume",
              "spare time. Native fallback must establish the actual result. Counts of low",
              "slack are disclosed above, including any increase; no detour multiplier or",
              "fabricated native success rate is used.", "",
              "## District comparison", "",
              "Successes / all scenarios in that district/preset, including failed filters.",
              "Empty count is denominator minus numerator; Nevsky denominator is 20, other",
              "groups 12. This measures these starts, not every neighborhood of the district.", "",
              f"| Scenario district/group | Budget | 77 + C1 | {count} + C1 | 77 + v2 | {count} + v2 |",
              "|---|---:|---:|---:|---:|---:|"]
    for district in summaries["old77_c1"]["districts"]:
        for budget in (60, 120, 240):
            fields = []
            for cell in cells:
                s = summaries[cell]["districts"][district][str(budget)]
                fields.append(f"{s['success']} / {s['scenarios']}")
            lines.append(f"| {district} | {budget} | " + " | ".join(fields) + " |")
    lines += ["", "## Interest filters", "",
              f"| Filter | 77 + C1 | {count} + C1 | 77 + v2 | {count} + v2 |",
              "|---|---:|---:|---:|---:|"]
    for f in FILTERS:
        fields = [f"{summaries[c]['filters'][f]['success']} / 96" for c in cells]
        lines.append(f"| {f} | " + " | ".join(fields) + " |")
    lines += ["", "## Local catalog opportunity", "",
              "Number of starts with at least one POI within each geometric band, out of 32.",
              "These counts do not impose hard geographic limits or prove a route/visit fits.", "",
              f"| Haversine radius, m | Old 77 | Expanded {count} |", "|---:|---:|---:|"]
    for band in BANDS:
        coverage = result["near_start_coverage_out_of_32"]
        lines.append(f"| {band} | {coverage['old77'][str(band)]} | {coverage['expanded'][str(band)]} |")
    lines += ["", "## Nevsky case study (all categories)", "",
              "Count/total preliminary minutes, and first preliminary transfer minutes.",
              "Empty results retain their explicit reason in JSON. The user's physical start",
              "is unknown. The Phase B reference and four additional starts are all included.", "",
              f"| Start | Budget | 77 + C1 (stops / total / first) | {count} + v2 (stops / total / first) |",
              "|---|---:|---|---|"]
    for old in result["cases"]:
        if old["cell"] != "old77_c1" or not old["point_id"].startswith("nevsky_") or old["filter_id"] != "all":
            continue
        new = next(r for r in result["cases"] if r["cell"] == "expanded_v2" and r["point_id"] == old["point_id"]
                   and r["budget"] == old["budget"] and r["filter_id"] == "all")
        def value(row):
            return (f"{row['stop_count']} / {row['preliminary_total_minutes']} / "
                    f"{row['first_preliminary_transfer_seconds'] / 60:.2f}" if row["stop_count"] else "empty")
        lines.append(f"| {old['point_id']} | {old['budget']} | {value(old)} | {value(new)} |")
    lines += ["", "## Computation and request volume", "",
              "Measured Python wall times (one run, local Python3.14; not a device benchmark):", "",
              "| Cell | Median ms | Max ms |", "|---|---:|---:|"]
    for cell in cells:
        timing = result["timing"][cell]
        lines.append(f"| {labels[cell]} | {timing['median_ms']} | {timing['max_ms']} |")
    lines += ["", "V2 searches at most 24 candidates, 32 states per depth and six depths. Python",
              "evaluation makes zero native requests. Flutter allows at most three supplied",
              "POI sets, six total route builds and twelve native leg requests; reservations",
              "are conservative if a build fails early. The C1 removal-only worst case was",
              "six builds of 6+5+4+3+2+1 legs (21 native requests). Fake tests exercise the new",
              "cap, failure classes, alternate acceptance and stale-response rejection.", "",
              "## Remaining gaps and native acceptance", "",
              "Accepted additions concentrate on Nevsky, Frunzensky and Krasnogvardeysky.",
              "Western/northern districts have no new verified additions in this phase:",
              "source errors prevented defensible expansion there. Existing long visits and",
              "restrictive history/nature filters still yield empty quick cases, as disclosed",
              "in every district row. A positive preliminary result several kilometers away",
              "is not evidence of useful near-start coverage.", "",
              "All 1,536 real MapKit results, removed-stop counts and actual request counts are",
              "`NOT_TESTED`/null. No Android device was attached. The",
              "[physical checklist](PHASE_C2_ANDROID_CHECKLIST.md) requires real captured",
              "route times, snapped access inspection and C1 GPS/navigation regressions.",
              "Bounded search is approximate; unverified representative points, topology,",
              "closures and SDK/network availability remain limitations.", ""]
    (ROOT / "docs/PHASE_C2_GEOGRAPHIC_EVALUATION.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    result = evaluate()
    (ROOT / "docs/phase_c2_geographic_evaluation.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_markdown(result)
    print(json.dumps({k: result[k] for k in ("old_count", "expanded_count", "start_count", "cases_per_cell",
                                          "near_start_coverage_out_of_32", "timing", "summaries")}, ensure_ascii=False, indent=2))
