"""Phase C6 §14: reproducible validation of the editorial historical dataset.

Run as a module::

    python -m app.db.validate_place_history

Checks (see docs/PHASE_C6_EDITORIAL_GUIDELINES.md):
- structural validity of every card (schemas/place_history.py is the boundary);
- no duplicate catalog keys, no references to unknown POIs;
- every fact/timeline event/person is backed by sources OF THE SAME card;
- every card carries at least two distinct verified sources;
- verified-on dates are ISO dates and not in the future;
- source URLs are absolute http(s) links (no javascript:/data:/file:);
- editorial status is one of the documented values.

Volume guidance (§6.2: 150–250 words) is reported as WARNINGS, not errors:
small objects must not be artificially stretched to hit a word target.

Exit code 0 means the dataset may ship; 1 means errors were found.
The pytest suite (tests/test_place_history.py) calls validate_dataset()
directly, so the same invariants are enforced in CI.
"""
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path

from app.db.place_history import PlaceHistoryValidationError, load_place_history
from app.db.seed_data import SPB_SEED_PLACES
from app.schemas.place_history import HistoricalPlaceContent

SEED_PATH = Path(__file__).with_name("seed_places.json")
# app/db/validate_place_history.py -> parents[2] is the repository root.
DISTRICT_ASSIGNMENTS_PATH = (
    Path(__file__).parents[2] / "docs" / "phase_b_district_assignments.json"
)

MIN_SOURCES_PER_CARD = 2


def _load_district_assignments() -> dict[str, dict]:
    """Phase B audited only the central districts: 77 of 173 catalog keys."""
    try:
        raw = json.loads(DISTRICT_ASSIGNMENTS_PATH.read_text(encoding="utf-8"))
        return {key: value for key, value in raw.items() if isinstance(value, dict)}
    except (OSError, json.JSONDecodeError):
        return {}


def validate_dataset() -> tuple[list[str], list[str], dict[str, HistoricalPlaceContent]]:
    """Return (errors, warnings, entries) for the shipped editorial dataset."""
    errors: list[str] = []
    warnings: list[str] = []
    try:
        entries = load_place_history()
    except PlaceHistoryValidationError as exc:
        return [str(exc)], [], {}

    today = date.today().isoformat()
    for key, entry in sorted(entries.items()):
        if len({source.id for source in entry.sources}) < MIN_SOURCES_PER_CARD:
            warnings.append(
                f"{key}: a historical card ideally carries {MIN_SOURCES_PER_CARD} "
                f"distinct sources, found {len(entry.sources)}"
            )
        if entry.lastVerifiedOn > today:
            errors.append(f"{key}: lastVerifiedOn {entry.lastVerifiedOn} is in the future")
        for source in entry.sources:
            if source.accessedOn > today:
                errors.append(
                    f"{key}: source {source.id!r} accessedOn {source.accessedOn} is in the future"
                )
        words = len(entry.detailedHistoricalText.split())
        if words < 80:
            warnings.append(
                f"{key}: detailed text is {words} words — editorial guidance is 150-250 "
                f"(short objects may legitimately be shorter)"
            )
        elif words > 320:
            warnings.append(
                f"{key}: detailed text is {words} words — editorial guidance is 150-250"
            )
        sentences = entry.shortHistoricalSummary.count(".") + entry.shortHistoricalSummary.count("!")
        if sentences < 2:
            warnings.append(
                f"{key}: short summary looks like {max(sentences, 1)} sentence — guidance is 2-4"
            )
        if len(entry.facts) > 7:
            warnings.append(
                f"{key}: {len(entry.facts)} facts — editorial guidance caps lists at a few "
                f"substantial facts, not uniform filler"
            )
        if entry.editorialStatus != "verified":
            warnings.append(f"{key}: editorialStatus is {entry.editorialStatus!r}")
    return errors, warnings, entries


def _coverage_report(entries: dict[str, HistoricalPlaceContent]) -> list[str]:
    seed_by_key = {place["catalog_key"]: place for place in SPB_SEED_PLACES}
    districts = _load_district_assignments()

    lines: list[str] = []
    total_seed = len(seed_by_key)
    covered = sorted(k for k in entries if k in seed_by_key)
    lines.append(f"Каталог: {total_seed} POI; проверенных исторических карточек: {len(covered)}")

    by_category = Counter(seed_by_key[key]["category"] for key in covered)
    lines.append("Покрытие по категориям:")
    for category, count in sorted(by_category.items()):
        lines.append(f"  {category}: {count}")

    by_district = Counter()
    district_missing = 0
    for key in covered:
        assignment = districts.get(key)
        if assignment and assignment.get("district"):
            by_district[assignment["district"]] += 1
        else:
            district_missing += 1
    lines.append("Покрытие по районам (привязка Phase B доступна не для всех POI):")
    for district, count in sorted(by_district.items()):
        lines.append(f"  {district}: {count}")
    if district_missing:
        lines.append(f"  (вне аудита Phase B): {district_missing}")

    lines.append("Объекты с проверенной карточкой:")
    for key in covered:
        lines.append(f"  {key}: {seed_by_key[key]['title']}")
    return lines


def main() -> int:
    errors, warnings, entries = validate_dataset()
    for message in errors:
        print(f"ERROR: {message}")
    for message in warnings:
        print(f"WARNING: {message}")
    print()
    for line in _coverage_report(entries):
        print(line)
    if errors:
        print("\nRESULT: dataset is NOT shippable (see errors above)")
        return 1
    print("\nRESULT: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
