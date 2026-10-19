"""C11 quiet model: geometry-based proxy, honest unknown, no acoustic claims."""
from __future__ import annotations

import pytest

from app.services.quiet_engine.engine import (
    QuietAnalysisInputError,
    QuietEngine,
    QuietLegInput,
)
from tests.c11_quiet_fixtures import line, quiet_dataset, way

LAT = 59.94
LON = 30.33


def _engine(*ways, bbox_dataset=True):
    return QuietEngine(quiet_dataset(list(ways)))


def _route(*points) -> list[QuietLegInput]:
    return [QuietLegInput(geometry=tuple(points))]


def _primary(lat=LAT, lon=LON, points=2, **tags):
    return way(1, "primary", line(30.30, lat, 30.36, lat, points), **tags)


def test_route_on_a_primary_road_has_high_proxy_and_full_coverage():
    result = _engine(_primary()).analyze(
        _route((LAT, 30.31), (LAT, 30.35)))
    assert result.status == "available"
    assert result.known_m == pytest.approx(result.route_length_m, abs=0.2)
    assert result.unknown_m == pytest.approx(0.0, abs=0.2)
    assert result.route_noise_exposure_index == pytest.approx(0.85, abs=0.03)
    assert result.low_exposure_share == pytest.approx(0.0, abs=0.01)
    assert result.major_road_adjacent_share == pytest.approx(1.0, abs=0.01)
    assert result.max_road_class_nearby == "primary"


def test_distance_decay_and_a_valid_zero_far_from_roads():
    engine = _engine(_primary())
    near = engine.analyze(_route((59.9405, 30.31), (59.9405, 30.35)))
    assert 0.20 < near.route_noise_exposure_index < 0.35
    far = engine.analyze(_route((59.9455, 30.31), (59.9455, 30.35)))
    assert far.status == "available"
    assert far.known_m == pytest.approx(far.route_length_m, abs=0.2)
    assert far.route_noise_exposure_index == 0.0
    assert far.low_exposure_share == 1.0
    assert far.major_road_adjacent_share == 0.0


def test_functional_class_changes_the_proxy_not_only_proximity():
    primary = _engine(_primary()).analyze(
        _route((LAT, 30.31), (LAT, 30.35))).route_noise_exposure_index
    residential = _engine(
        way(2, "residential", line(30.30, LAT, 30.36, LAT))
    ).analyze(_route((LAT, 30.31), (LAT, 30.35))).route_noise_exposure_index
    living = _engine(
        way(3, "living_street", line(30.30, LAT, 30.36, LAT))
    ).analyze(_route((LAT, 30.31), (LAT, 30.35))).route_noise_exposure_index
    assert primary > residential > living
    assert residential == pytest.approx(0.30, abs=0.02)
    assert living == pytest.approx(0.15, abs=0.02)


def test_one_crossing_does_not_make_the_whole_walk_noisy():
    crossing = way(1, "primary", line(LON, 59.935, LON, 59.945))
    result = _engine(crossing).analyze(
        _route((LAT, 30.31), (LAT, 30.35)))
    assert result.status == "available"
    assert result.route_noise_exposure_index < 0.10
    assert result.major_road_adjacent_share < 0.10
    assert result.low_exposure_share > 0.80


def test_parallel_residential_does_not_mask_a_primary_corridor():
    primary = _primary(lat=59.9398)
    residential = way(2, "residential", line(30.30, 59.9405, 30.36, 59.9405))
    result = _engine(primary, residential).analyze(
        _route((59.9405, 30.31), (59.9405, 30.35)))
    # The nearest road is residential; the primary ~78 m away still has a
    # smaller contribution. The proxy uses MAX, not a sum, so a route is not
    # automatically labelled noisy merely because another corridor exists.
    # The interpreted class reports the highest class actually nearby.
    assert 0.25 < result.route_noise_exposure_index < 0.40
    assert result.max_road_class_nearby == "primary"


def test_densifying_the_same_geometry_does_not_change_the_result():
    engine = _engine(_primary())
    coarse = engine.analyze(_route((LAT, 30.31), (LAT, 30.35)))
    dense = engine.analyze(_route(*[(point[1], point[0]) for point in
                                    line(30.31, LAT, 30.35, LAT, points=101)]))
    assert coarse.route_length_m == pytest.approx(dense.route_length_m, rel=1e-6)
    assert coarse.route_noise_exposure_index == pytest.approx(
        dense.route_noise_exposure_index, abs=1e-6)


def test_ground_outside_the_pilot_is_unknown_not_zero():
    engine = _engine(_primary())
    outside = engine.analyze(_route((LAT, 30.40), (LAT, 30.41)))
    assert outside.status == "outside_coverage"
    assert outside.known_m == 0.0
    assert outside.unknown_m == pytest.approx(outside.route_length_m, abs=0.2)
    assert outside.route_noise_exposure_index is None
    assert outside.low_exposure_share is None
    assert "route_outside_pilot" in outside.reasons


def test_crossing_the_pilot_boundary_reports_partial_coverage():
    engine = _engine(_primary())
    result = engine.analyze(_route((LAT, 30.379), (LAT, 30.381)))
    assert result.status == "partial"
    assert 0.0 < result.known_m < result.route_length_m
    assert result.unknown_m > 0.0
    assert result.known_m + result.unknown_m == pytest.approx(
        result.route_length_m, abs=0.2)
    assert result.route_noise_exposure_index is not None


def test_access_no_is_not_a_noise_source_but_a_destination_road_is():
    excluded = way(1, "primary", line(30.30, LAT, 30.36, LAT), access="no")
    # Keep the dataset non-empty with a road far outside the route's influence.
    far = way(9, "residential", line(30.30, 59.96, 30.36, 59.96))
    result = _engine(excluded, far).analyze(_route((LAT, 30.31), (LAT, 30.35)))
    assert result.route_noise_exposure_index == 0.0
    destination = way(2, "primary", line(30.30, LAT, 30.36, LAT),
                      access="destination")
    result = _engine(destination).analyze(_route((LAT, 30.31), (LAT, 30.35)))
    assert result.route_noise_exposure_index == pytest.approx(0.85, abs=0.03)


def test_a_tunnel_surface_is_discounted_not_treated_as_a_street_source():
    tunnel = way(1, "primary", line(30.30, LAT, 30.36, LAT), tunnel="yes")
    result = _engine(tunnel).analyze(_route((LAT, 30.31), (LAT, 30.35)))
    assert result.route_noise_exposure_index == pytest.approx(0.1275, abs=0.01)


def test_per_leg_lengths_sum_and_leg_indexes_are_preserved():
    result = _engine(_primary()).analyze([
        QuietLegInput(geometry=((LAT, 30.31), (LAT, 30.33))),
        QuietLegInput(geometry=((LAT, 30.33), (LAT, 30.35))),
    ])
    assert [leg.leg_index for leg in result.legs] == [0, 1]
    assert sum(leg.length_meters for leg in result.legs) == pytest.approx(
        result.route_length_m, abs=0.2)
    assert all(leg.road_noise_exposure_index is not None for leg in result.legs)


def test_invalid_and_degenerate_route_geometry_is_refused():
    engine = _engine(_primary())
    with pytest.raises(QuietAnalysisInputError):
        engine.analyze([])
    with pytest.raises(QuietAnalysisInputError):
        engine.analyze(_route((LAT, 30.31),))
    with pytest.raises(QuietAnalysisInputError):
        engine.analyze(_route((LAT, 30.31), (LAT, 30.31)))
    with pytest.raises(QuietAnalysisInputError):
        engine.analyze(_route((float("nan"), 30.31), (LAT, 30.31)))


def test_disabled_dataset_returns_unavailable_without_numbers():
    from app.services.quiet_engine.source import QuietDataset
    result = QuietEngine(QuietDataset.unavailable("quiet_engine_disabled")).analyze(
        _route((LAT, 30.31), (LAT, 30.35)))
    assert result.status == "unavailable"
    assert result.route_noise_exposure_index is None
    assert result.known_m == 0.0
    assert result.unknown_m == pytest.approx(result.route_length_m, abs=0.2)
    assert result.reasons == ("quiet_engine_disabled",)
