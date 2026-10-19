"""Validate the shipped C11 quiet pilot without network access.

From backend: python -m scripts.validate_phase_c11_quiet_data
Exit code 0 only when the manifest, gzip, canonical JSON, bbox, source licence,
way ids, class filter and coordinates all agree.
"""
from __future__ import annotations

import json
import sys

from scripts.acquire_phase_c11_quiet_data import (
    BBOX,
    CORE_HIGHWAYS,
    MANIFEST,
    verify,
)


def main() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    result = verify()
    if list(manifest["pilot_bbox"]) != list(BBOX):
        raise ValueError("manifest bbox mismatch")
    if manifest["license"] != "ODbL-1.0":
        raise ValueError("unexpected licence")
    if manifest["way_count"] < 3000:
        raise ValueError("pilot way count is implausibly small")
    if not set(manifest["class_counts"]) <= CORE_HIGHWAYS:
        raise ValueError("manifest contains a non-road class")
    print(json.dumps({
        **result,
        "license": manifest["license"],
        "attribution": manifest["attribution"],
        "pilot_bbox": manifest["pilot_bbox"],
        "source_sha256": manifest["source_sha256"],
        "osm_base_timestamp": manifest["osm_base_timestamp"],
    }, ensure_ascii=False))
    sys.exit(0)


if __name__ == "__main__":
    main()
