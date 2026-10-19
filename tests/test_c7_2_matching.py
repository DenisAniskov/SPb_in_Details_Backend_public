"""C7.2 surface classes, conservative matching and the read-only ways store.

The matching rules are the core honesty mechanism of v1: a slice may only
inherit surface facts from an UNAMBIGUOUS linear overlap with a walkable way.
These tests pin every refusal: crossing roads, parallel walkways, private
ways, unmapped surfaces and vertex density.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.puddle_engine.matching import (
    expand_bbox,
    point_in_bbox,
    sample_along,
    slice_match,
)
from app.services.puddle_engine.surfaces import (
    SURFACE_PERMEABLE,
    SURFACE_SEMI,
    SURFACE_SMOOTH,
    SURFACE_UNEVEN,
    surface_facts,
)
from app.services.puddle_engine.ways_store import (
    EmptyPuddleWaysProvider,
    JsonPuddleWaysProvider,
    PuddleWay,
    WALKABLE_HIGHWAY,
)
from tests.puddle_fixtures import latlon, way

REAL_DATASET = Path(__file__).resolve().parents[1] / "data/puddle_ways_spb_pilot.json"


@pytest.mark.parametrize("surface, expected", [
    ("asphalt", SURFACE_SMOOTH),
    ("paved", SURFACE_SMOOTH),
    ("paving_stones", SURFACE_UNEVEN),
    ("sett", SURFACE_UNEVEN),
    ("unhewn_cobblestone", SURFACE_UNEVEN),
    ("compacted", SURFACE_SEMI),
    ("gravel", SURFACE_SEMI),
    ("wood", SURFACE_SEMI),
    ("ground", SURFACE_PERMEABLE),
    ("grass", SURFACE_PERMEABLE),
])
def test_surface_values_map_to_behaviour_classes(surface, expected):
    assert surface_facts({"surface": surface}).surface_class == expected


def test_missing_surface_is_never_asphalt():
    facts = surface_facts({"highway": "footway"})
    assert facts.surface_class is None
    assert facts.surface_tag is None


def test_unknown_surface_value_stays_unknown_but_is_reported():
    facts = surface_facts({"surface": "building_passage"})
    assert facts.surface_class is None
    assert facts.surface_tag == "building_passage"  # → surface_not_supported


def test_covered_and_tunnel_mark_a_shelter_but_changing_nothing_else():
    assert surface_facts({"surface": "asphalt", "covered": "yes"}).covered is True
    assert surface_facts({"surface": "asphalt",
                          "tunnel": "building_passage"}).covered_by == "tunnel"
    plain = surface_facts({"surface": "asphalt"})
    assert plain.covered is False and plain.covered_by is None


def test_slice_along_a_way_is_confirmed_with_its_tags():
    route = way(1, 0.0, surface="paving_stones")
    match = slice_match(sample_along(route.geometry), [route])
    assert match.way is route
    assert match.ambiguous is False
    assert match.share == 1.0
    assert match.min_distance_m == pytest.approx(0.0, abs=0.05)


def test_slice_far_from_every_way_is_unmatched():
    route = way(1, 40.0, surface="asphalt")
    assert slice_match(sample_along(route.geometry),
                       [way(2, 0.0, surface="asphalt")]).way is None


def test_a_perpendicular_crossing_confirms_nothing():
    """A road crossing the sliced line shares only a point — no tags transfer."""
    geometry = tuple(latlon(0.0, y) for y in (-60.0, 0.0, 60.0))
    crossing = PuddleWay(
        osm_id=9, osm_version=1, osm_timestamp="2026-10-01T00:00:00Z",
        tags={"highway": "footway", "surface": "asphalt"}, geometry=geometry,
        bbox=(min(p[0] for p in geometry), min(p[1] for p in geometry),
              max(p[0] for p in geometry), max(p[1] for p in geometry)))
    walking = way(10, 0.0, x_from=-120.0, x_to=120.0, points=3)
    assert slice_match(sample_along(walking.geometry), [crossing]).way is None


def test_parallel_way_with_the_same_facts_is_not_ambiguous():
    best = way(1, 0.0, surface="asphalt")
    twin = way(2, 3.0, surface="asphalt")
    match = slice_match(sample_along(best.geometry), [twin, best])
    assert match.ambiguous is False
    assert match.way is best  # the closest / best-covering way wins


def test_parallel_way_with_different_facts_is_ambiguous():
    """Two walkways 3 m apart: the slice stays unknown, borrowing from none."""
    first = way(1, 0.0, surface="asphalt")
    second = way(2, 3.0, surface="paving_stones")
    match = slice_match(sample_along(first.geometry), [first, second])
    assert match.way is None
    assert match.ambiguous is True
    assert match.confirmed_count == 2


def test_identical_facts_on_a_twin_way_stay_resolvable():
    first = way(1, 0.0, surface="asphalt", covered="yes")
    second = way(2, 3.0, surface="asphalt", covered="yes")
    match = slice_match(sample_along(first.geometry), [first, second])
    assert match.way is first and match.ambiguous is False


@pytest.mark.parametrize("tags", [
    {"highway": "residential", "surface": "asphalt"},   # carriageway
    {"highway": "service", "surface": "asphalt"},       # service road
    {"highway": "footway", "surface": "asphalt", "foot": "no"},
    {"highway": "footway", "surface": "asphalt", "access": "private"},
])
def test_non_walkable_or_excluded_ways_never_confirm(tags):
    route = way(1, 0.0, **tags)
    assert route.matchable() is False
    assert slice_match(sample_along(route.geometry), [route]).way is None


def test_every_walkable_type_is_matchable():
    for highway in sorted(WALKABLE_HIGHWAY):
        assert way(1, 0.0, highway=highway).matchable() is True


def test_vertex_density_does_not_change_matching():
    sparse = way(1, 0.0, points=2, surface="asphalt")
    dense = way(2, 0.0, points=301, surface="asphalt")
    sparse_match = slice_match(sample_along(sparse.geometry), [sparse])
    dense_match = slice_match(sample_along(dense.geometry, max_samples=1000),
                              [dense])
    assert sparse_match.share == dense_match.share == pytest.approx(1.0)


def test_bbox_helpers_behave_in_lat_lon_terms():
    box = (59.90, 30.30, 59.95, 30.35)
    assert point_in_bbox(59.93, 30.32, box) is True
    assert point_in_bbox(59.93, 30.40, box) is False
    grown = expand_bbox(box, 1113.2)  # ≈ 0.01° of latitude
    assert grown[0] == pytest.approx(59.89, abs=0.011)
    assert grown[2] == pytest.approx(59.96, abs=0.011)


# ------------------------------------------------------------- the ways store
def test_real_pilot_dataset_loads_with_identity_and_coverage():
    provider = JsonPuddleWaysProvider(REAL_DATASET)
    assert provider.available() is True
    assert provider.dataset_version() == "c7_2_palace_pilot_v1"
    assert provider.pilot_bbox() == (59.9365, 30.307, 59.9445, 30.326)
    provenance = provider.provenance()
    assert provenance["license"] == "ODbL-1.0"
    assert provenance["attribution"] == "© OpenStreetMap contributors"
    assert provenance["way_count"] == 1094
    assert provenance["raw_sha256"]
    all_ways = provider.ways_in_bbox((59.0, 30.0, 60.0, 31.0))
    assert len(all_ways) == 1094
    # Geometry keeps the ORIGINAL vertices (no simplification at load time).
    raw = json.loads(REAL_DATASET.read_bytes())
    assert len(all_ways[0].geometry) == len(raw["ways"][0]["geometry"])


def test_dataset_with_wrong_schema_is_unavailable(tmp_path):
    payload = {"schema_version": 2}
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    provider = JsonPuddleWaysProvider(path)
    assert provider.available() is False
    assert provider.provenance()["available"] is False
    assert provider.ways_in_bbox((0.0, 0.0, 1.0, 1.0)) == []


def test_dataset_with_duplicate_ids_or_bad_geometry_is_unavailable(tmp_path):
    base = {
        "schema_version": 1, "dataset_version": "t",
        "pilot_bbox": [30.3, 59.9, 30.4, 60.0],
        "ways": [
            {"osm_id": 1, "osm_version": 1, "osm_timestamp": "2026-10-01T00:00:00Z",
             "tags": {"highway": "footway"},
             "geometry": [[30.31, 59.94], [30.32, 59.94]]},
            {"osm_id": 1, "osm_version": 1, "osm_timestamp": "2026-10-01T00:00:00Z",
             "tags": {"highway": "footway"},
             "geometry": [[30.33, 59.94], [30.34, 59.94]]},
        ],
    }
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps(base), encoding="utf-8")
    assert JsonPuddleWaysProvider(path).available() is False

    base["ways"][1]["osm_id"] = 2
    base["ways"][1]["geometry"] = [[999.0, 59.94], [30.34, 59.94]]
    path.write_text(json.dumps(base), encoding="utf-8")
    assert JsonPuddleWaysProvider(path).available() is False

    base["ways"][1]["geometry"] = [[30.33, 59.94]]
    path.write_text(json.dumps(base), encoding="utf-8")
    assert JsonPuddleWaysProvider(path).available() is False


def test_minimal_valid_dataset_is_available_with_oriented_bbox(tmp_path):
    payload = {
        "schema_version": 1, "dataset_version": "tiny",
        "pilot_bbox": [30.3, 59.9, 30.4, 60.0],  # west, south, east, north
        "ways": [{"osm_id": 7, "osm_version": 3,
                  "osm_timestamp": "2026-10-01T00:00:00Z",
                  "tags": {"highway": "footway", "surface": "asphalt"},
                  "geometry": [[30.31, 59.94], [30.32, 59.941]]}],
        "license": "ODbL-1.0",
    }
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    provider = JsonPuddleWaysProvider(path)
    assert provider.available() is True
    assert provider.pilot_bbox() == (59.9, 30.3, 60.0, 30.4)
    assert provider.provenance()["pilot_bbox"] == [30.3, 59.9, 30.4, 60.0]


def test_empty_provider_is_honest():
    provider = EmptyPuddleWaysProvider()
    assert provider.available() is False
    assert provider.dataset_version() is None
    assert provider.pilot_bbox() is None
    assert provider.ways_in_bbox((0.0, 0.0, 1.0, 1.0)) == []
