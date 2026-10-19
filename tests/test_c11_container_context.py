"""Deployment guard: the Docker allowlist must carry every runtime dataset.

Docker's ``*``/``!data/`` pattern re-opens the directory but not the files
inside it. Each production dataset therefore needs its own allowlist entry;
missing one silently turns a working engine into an honest "unavailable".
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERIGNORE = ROOT / ".dockerignore"

RUNTIME_DATASETS = (
    "data/shade_buildings_spb_pilot.sqlite",
    "data/puddle_ways_spb_pilot.json",
    "data/quiet_spb_pilot.json.gz",
    "data/quiet_spb_pilot.manifest.json",
)


def test_docker_context_explicitly_reopens_every_runtime_dataset():
    rules = {
        line.strip()
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    for relative in RUNTIME_DATASETS:
        assert (ROOT / relative).is_file(), f"missing runtime dataset {relative}"
        assert f"!{relative}" in rules, f"{relative} is excluded from the image context"
