"""Verify the published snapshot against its recorded extraction provenance.

`docs/A5_EXTRACTION_MANIFEST.json` records the reviewed extraction of this
backend from the pre-split monorepo revision `f09219ae`, including the SHA-256
of every imported file. That private history is deliberately not carried into
this public repository, so the pinned revision checked here is the repository's
own initial release commit: the published tree must match the recorded
extraction, and every difference — whether it happened before publication or
after it — needs an explicit reason in one of the reviewed maps below. Files
that are intentionally not published are listed separately, also with a reason.

Scenic JSON equivalence is proved by replaying the committed C5 builder in
memory against `data/scenic_features.json`, exactly as the private extraction
tool did. The client Dart blob hash is recorded here as a cross-repository
reference; it is re-verified only when `--source-dart` points at a local copy
of `lib/data/scenic/scenic_dataset.dart` from the client repository.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = "docs/A5_EXTRACTION_MANIFEST.json"
SOURCE_SCENIC_PATH = "lib/data/scenic/scenic_dataset.dart"
SOURCE_SCENIC_REPOSITORY = "https://github.com/DenisAniskov/SPb_in_Details_public"
# Both references describe the same 187 records: the blob hash recorded by the
# extraction, and the published client file whose regenerated header comment
# names the public provenance directory instead of the old monorepo path.
CLIENT_DART_REFERENCES = {
    "monorepo-blob-before-publication": "f6755fbb95c597f96d206e39f5fa2fa9573027dd1921062b0160e557c84166ad",
    "published-client-file": "50a89b22d3805a011f14b543db4b5996a235ed0c63ad3a104d228eb0225a099b",
}
PATH_ADAPTATION = "Resolve repository-root paths after flattening backend/; unchanged data and algorithms."
SCRIPT_PATHS = (
    "acquire_phase_c2_osm.py", "acquire_phase_c3_osm.py", "acquire_phase_c6_1_osm.py",
    "acquire_phase_c7_1_osm.py", "build_phase_c2_catalog.py", "build_phase_c3_catalog.py",
    "build_phase_c3_entrance_audit.py", "build_phase_c6_1_catalog.py", "build_phase_c7_1_buildings.py",
    "diagnose_root_causes.py", "evaluate_phase_c2.py", "evaluate_phase_c3.py",
    "check_phase_c3_current_objects.py", "check_phase_c6_1_current_objects.py",
    "extract_c7_1_test_fixture.py", "validate_phase_b_catalog.py", "validate_phase_c2_catalog.py",
    "validate_phase_c3_catalog.py", "validate_phase_c6_1_catalog.py",
    "_phase_c2_candidate_review.py", "_phase_c3_candidate_review.py", "_phase_c6_1_candidate_review.py",
)

# Adaptations reviewed while the standalone backend was being extracted. The
# reasons are the ones recorded by the private extraction tool and stay valid.
EXTRACTION_ADAPTATIONS = {
    **{f"scripts/{name}": PATH_ADAPTATION for name in SCRIPT_PATHS},
    **{f"tests/test_{phase}_catalog.py": PATH_ADAPTATION
       for phase in ("phase_b", "phase_c2", "phase_c3", "phase_c6_1")},
    "scripts/extract_c7_1_1_response_fixture.py":
        "Rehome the unchanged cross-language response fixture to tests/fixtures; update generator paths.",
    "app/db/validate_place_history.py":
        "Resolve the existing district-assignment evidence at the new repository root.",
    "app/db/README.md": "Repair relative links to the preserved ODbL license supplements.",
    "docs/phase_c5_acquisition/build_scenic_dataset.py":
        "Export the same 187 selected records as JSON, freeze original date, and verify raw manifest hashes.",
    "docs/phase_c5_acquisition/evaluate_scenic_geography.py":
        "Read equivalent JSON records and support non-writing verification against the frozen evaluation.",
    "Dockerfile":
        "Copy the backup CLI and stdlib privilege-drop entrypoint for a writable mounted volume.",
    ".dockerignore":
        "Allow only the entrypoint, backup CLI, and exact read-only runtime pilots "
        "(shade SQLite, puddle ways, quiet gzip/manifest) required by Docker COPY.",
    ".env.example":
        "Append the reviewed C7.2/C7.3, C11 Quiet and C13 assistant optional settings "
        "(disabled by default, credential-free); no existing value changed.",
    "app/api/v1/api.py":
        "Mount the reviewed C7.2 puddle, C7.3.1 pollen and read-only C11 quiet routers.",
    "app/core/config.py":
        "Add bounded C7.2/C7.3 and C11 settings; external model sources and the "
        "quiet engine default to disabled.",
    "app/schemas/place.py":
        "Expose the nullable immutable catalog_key in read schemas (C8.1); no write-field change.",
    "app/schemas/route.py":
        "Point the legacy quietness placeholder to the separate read-only Quiet endpoint; "
        "no routing, distance or threshold change.",
}

# Changes made in the backend repository after the extraction was verified.
# Commit d255cd0 (C13 free-only assistant) touched these two files and the
# private extraction tool was never updated for them; they are recorded here
# explicitly instead of being left as unreviewed drift.
POST_EXTRACTION_CHANGES = {
    "app/main.py":
        "C13 assistant lifecycle (pre-migration backup hook, assistant service shutdown) "
        "added by commit d255cd0; no catalog, routing, Shade, Puddle, Pollen or Quiet change.",
    "app/db/session.py":
        "C13 assistant quota schema initialization added next to the upstream quota ledger "
        "by commit d255cd0; no catalog seed or place-model change.",
}

# Documentation edits made for this public release: monorepo paths resolved
# against the repository root and links to internal reports removed, because
# those reports are not published here.
PUBLICATION_DOCUMENTATION_CHANGES = """
Publication cleanup of this document: monorepo `backend/` path prefixes resolved
against the repository root; links to internal phase reports that are not
published in this repository removed. No data, model or contract statement
changed.
""".strip()

DOCUMENTATION_FILES = (
    "docs/PLACES_AUDIT.md", "docs/SMART_WALK_ALGORITHM.md",
    "docs/PHASE_C2_LOCAL_ROUTING_ALGORITHM.md", "docs/PHASE_C2_GEOGRAPHIC_EVALUATION.md",
    "docs/PHASE_C7_1_API_CONTRACT.md", "docs/PHASE_C7_1_ARCHITECTURE.md",
    "docs/PHASE_C7_1_OSM_BUILDING_ACQUISITION.md", "docs/PHASE_C7_1_GEOGRAPHIC_COVERAGE.md",
    "docs/PHASE_C7_2_ARCHITECTURE.md", "docs/PHASE_C7_2_DATA_PROVENANCE.md",
    "docs/PHASE_C11_ARCHITECTURE.md", "docs/SEARCH_INTEGRATION.md",
    "docs/PHASE_B_SOURCE_LICENSES.md", "docs/PHASE_C2_SOURCE_LICENSES.md",
    "docs/PHASE_C3_SOURCE_LICENSES.md", "docs/PHASE_C3_ROUTING_POINT_DESIGN.md",
    "docs/PHASE_C6_ARCHITECTURE.md", "docs/PHASE_C6_HISTORICAL_DATA_MODEL.md",
    "docs/PHASE_C6_1_CATALOG_EXTENSION.md", "docs/phase_c2_acquisition/README.md",
)

OPERATIONS_REPORT = ("Internal operations report (deployment, extraction, hosting or CI "
                     "evidence for the private development setup); not published.")
PHASE_REPORT = ("Internal phase report (validation, audit or handover narrative); not "
                "published because this release ships the code, the machine-readable "
                "evidence and user-facing documentation instead.")
EXCLUDED_FROM_PUBLICATION = {
    **{path: OPERATIONS_REPORT for path in (
        "docs/A5_API_CONTRACT.md", "docs/A5_BASELINE_VALIDATION.json", "docs/A5_CI_CD.md",
        "docs/A5_DOCKER_VALIDATION.json", "docs/A5_ENVIRONMENT_VARIABLES.md",
        "docs/A5_EXTRACTION_REPORT.md", "docs/A5_EXTRACTION_VERIFICATION.json",
        "docs/A5_LOCAL_HTTP_VALIDATION.json", "docs/A5_LOCAL_PERSISTENCE_VALIDATION.json",
        "docs/A5_OSM_EQUIVALENCE.json", "docs/A5_RAILWAY_DEPLOYMENT.md",
        "docs/A5_RESOURCE_BUDGET.md", "docs/A5_ROLLBACK.md", "docs/A5_RUNTIME_VALIDATION.md",
        "docs/A5_SECURITY.md", "docs/A5_STORAGE_AND_BACKUP.md",
        "docs/BACKEND_DEPENDENCY_AUDIT.md", "docs/BACKEND_DEPLOYMENT.md",
        "docs/BACKEND_HOSTING_OPTIONS.md", "docs/BACKEND_SOURCE_OF_TRUTH.md",
        "docs/C8_1_1_BACKEND_SYNC_MANIFEST.json", "docs/PHASE_C11_DEPLOYMENT.md",
        "docs/PHASE_C11_LIVE_API_SMOKE.md", "docs/PHASE_C7_2_FINAL_READINESS_AUDIT.md",
    )},
    **{path: PHASE_REPORT for path in (
        "docs/PHASE_A1_A3_VALIDATION.md", "docs/PHASE_B_CATALOG_AUDIT.md",
        "docs/PHASE_B_GEOGRAPHIC_COVERAGE.md", "docs/PHASE_B_VALIDATION.md",
        "docs/PHASE_C2_CATALOG_AUDIT.md", "docs/PHASE_C2_ROOT_CAUSE_ANALYSIS.md",
        "docs/PHASE_C2_VALIDATION.md", "docs/PHASE_C3_CATALOG_AUDIT.md",
        "docs/PHASE_C3_VALIDATION.md", "docs/PHASE_C6_VALIDATION.md",
        "docs/PHASE_C7_1_1_HOTFIX.md", "docs/PHASE_C7_1_2_NAVIGATION_REFRESH.md",
        "docs/PHASE_C7_1_ANDROID_CHECKLIST.md", "docs/PHASE_C7_1_PERFORMANCE.md",
        "docs/PHASE_C7_1_PORTING_AUDIT.md", "docs/PHASE_C7_1_ROUTE_COMPARISON.md",
        "docs/PHASE_C7_1_SIMULATOR_INTEGRATION.md", "docs/PHASE_C7_1_VALIDATION.md",
        "docs/PHASE_C7_2_VALIDATION.md", "docs/PHASE_C7_3_VALIDATION.md",
        "docs/SEARCH_AUDIT.md", "docs/SMART_WALK_AUDIT.md",
    )},
}

REVIEWED_CHANGES = {
    **EXTRACTION_ADAPTATIONS,
    **POST_EXTRACTION_CHANGES,
    **{path: PUBLICATION_DOCUMENTATION_CHANGES for path in DOCUMENTATION_FILES},
}


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def canonical(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def pinned_commit(requested: str | None) -> str:
    """The repository's own initial release commit is the pinned revision here."""
    if requested:
        return requested
    roots = subprocess.check_output(
        ["git", "rev-list", "--max-parents=0", "HEAD"], cwd=ROOT, text=True).split()
    if len(roots) != 1:
        raise RuntimeError("Expected exactly one root commit in this repository")
    return roots[0]


def release_tree(commit: str) -> dict[str, bytes]:
    archived = subprocess.check_output(["git", "archive", "--format=tar", commit], cwd=ROOT)
    with tarfile.open(fileobj=io.BytesIO(archived)) as stream:
        return {item.name: stream.extractfile(item).read() for item in stream.getmembers() if item.isfile()}


def replay_original_scenic(tree: dict[str, bytes]) -> dict:
    """Replay the pinned committed builder in check mode; it never writes.

    The builder itself refuses to pass unless the committed export equals the
    records rebuilt from the pinned raw acquisitions, and unless those raw
    acquisitions still match the frozen per-district SHA-256 manifest.
    """
    relative = "docs/phase_c5_acquisition/build_scenic_dataset.py"
    source = tree[relative].decode("utf-8")
    namespace = {"__name__": "extraction_scenic_replay", "__file__": str(ROOT / relative)}
    exec(compile(source, relative, "exec"), namespace)
    manifest = json.loads(tree["docs/phase_c5_scenic_dataset_manifest.json"])
    namespace["print"] = lambda *args, **kwargs: None
    captured_features: list[dict] = []

    def capture_return(frame, event, arg):
        if event == "return" and frame.f_globals is namespace and frame.f_code.co_name == "main":
            captured_features.extend(frame.f_locals["ordered"])
        return capture_return

    previous_trace = sys.gettrace()
    try:
        sys.settrace(capture_return)
        with patch.object(sys, "argv", [relative, "--check"]):
            namespace["main"]()
    finally:
        sys.settrace(previous_trace)

    export = json.loads((ROOT / "data/scenic_features.json").read_text(encoding="utf-8"))
    proof = {
        "checked_entry_point": f"{relative} --check",
        "source_feature_count": len(captured_features),
        "export_feature_count": len(export["features"]),
        "source_feature_semantic_sha256": sha256(canonical(captured_features)),
        "export_feature_semantic_sha256": sha256(canonical(export["features"])),
        "features_equal": captured_features == export["features"],
        "client_dataset_reference": {
            "repository": SOURCE_SCENIC_REPOSITORY,
            "path": SOURCE_SCENIC_PATH,
            "git_blob_sha256_before_publication": CLIENT_DART_REFERENCES["monorepo-blob-before-publication"],
            "published_file_sha256": CLIENT_DART_REFERENCES["published-client-file"],
            "verified_here": False,
        },
    }
    proof["passed"] = (
        proof["features_equal"]
        and proof["source_feature_count"] == proof["export_feature_count"] == 187
        and export["featureCount"] == 187
        and export["datasetVersion"] == manifest["datasetVersion"]
        and export["generatedOn"] == manifest["generatedOn"]
        and export.get("attribution") == "© OpenStreetMap contributors"
        and export.get("license") == "ODbL 1.0"
    )
    return proof


def verify_source_dart(path: Path) -> dict:
    digest = sha256(path.read_bytes())
    matched = sorted(name for name, value in CLIENT_DART_REFERENCES.items() if value == digest)
    return {"path": str(path), "sha256": digest, "matched_references": matched,
            "matches_recorded_reference": bool(matched)}


def verify(commit: str) -> dict:
    tree = release_tree(commit)
    manifest = json.loads(tree[MANIFEST_PATH])
    failures: list[str] = []
    unchanged = 0
    reviewed: list[dict] = []
    changed_since_release: list[dict] = []
    excluded: list[dict] = []
    for record in manifest["files"]:
        relative = record["path"]
        destination = (ROOT / relative).resolve()
        if not destination.is_relative_to(ROOT):
            failures.append(f"Unsafe manifest path: {relative}")
            continue
        snapshot = tree.get(relative)
        if snapshot is None:
            reason = EXCLUDED_FROM_PUBLICATION.get(relative)
            if reason is None:
                failures.append(f"Missing from the published snapshot without a review note: {relative}")
            else:
                excluded.append({"path": relative, "reason": reason})
            continue
        initial_hash = sha256(snapshot)
        current_hash = sha256(destination.read_bytes()) if destination.is_file() else None
        reason = REVIEWED_CHANGES.get(relative)
        if initial_hash != record["extracted_sha256"]:
            if reason is None:
                failures.append(f"Unreviewed drift from the recorded extraction: {relative}")
            else:
                reviewed.append({"path": relative, "kind": "reviewed-change",
                                 "recorded_sha256": record["extracted_sha256"],
                                 "pinned_sha256": initial_hash, "current_sha256": current_hash,
                                 "reason": reason})
        if current_hash != initial_hash:
            if reason is None or current_hash is None:
                failures.append(f"File differs from the pinned release commit without a review note: {relative}")
            else:
                changed_since_release.append({"path": relative, "pinned_sha256": initial_hash,
                                              "current_sha256": current_hash, "reason": reason})
        if current_hash == initial_hash == record["extracted_sha256"]:
            unchanged += 1
    for record in manifest["files"]:
        if re.match(r"^[A-Za-z]:[\/]", str(record.get("source_path", ""))):
            failures.append(f"Local machine path published in the manifest: {record['path']}")
    try:
        scenic = replay_original_scenic(tree)
        if not scenic["passed"]:
            failures.append("Scenic export differs from the committed builder output or the frozen manifest")
    except Exception as error:
        scenic = {"passed": False, "error": str(error)}
        failures.append("Scenic proof could not be completed")
    return {
        "source_repository": manifest["source_repository"],
        "source_commit": manifest["source_commit"],
        "destination_repository": manifest["destination_repository"],
        "pinned_release_commit": commit,
        "initial_imported_file_count": len(manifest["files"]),
        "recorded_byte_identical_file_count": unchanged,
        "reviewed_change_count": len(reviewed),
        "reviewed_changes": reviewed,
        "changed_since_release": changed_since_release,
        "excluded_from_publication_count": len(excluded),
        "excluded_from_publication": excluded,
        "scenic_equivalence": scenic,
        "failures": failures,
        "passed": not failures,
        "scope": ("Imported files and scenic data equivalence. Newly written public "
                  "documentation and the public CI are validated separately."),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-commit", help="Pinned release commit (default: this repository's root commit)")
    parser.add_argument("--source-dart", type=Path, help="Optional client copy of lib/data/scenic/scenic_dataset.dart")
    parser.add_argument("--output", type=Path, help="Optional JSON verification report")
    args = parser.parse_args()
    report = verify(pinned_commit(args.release_commit))
    if args.source_dart:
        extra = verify_source_dart(args.source_dart)
        report["source_dart_check"] = extra
        report["scenic_equivalence"]["client_dataset_reference"]["verified_here"] = extra["matches_recorded_reference"]
        if not extra["matches_recorded_reference"]:
            report["failures"].append("Client Dart blob does not match the recorded reference")
            report["passed"] = False
    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({key: report[key] for key in (
        "pinned_release_commit", "initial_imported_file_count", "recorded_byte_identical_file_count",
        "reviewed_change_count", "excluded_from_publication_count", "scenic_equivalence",
        "failures", "passed")}, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
