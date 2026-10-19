"""C7.1 time-aware shade engine (§10) — algorithmic and real-data tests.

Two groups:

* algorithmic tests use an explicitly synthetic provider (offline, exact
  expectations, no dataset needed);
* §19 control-scenario tests run against the committed pilot dataset with
  REAL OSM walking geometry taken verbatim from the acquisition evidence
  (`tests/fixtures/c7_1_scenarios.json`). Their expected shade values are
  model outputs at the recorded dataset version — regression anchors, NOT
  field measurements. They skip (with a reason) if the dataset is not built.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from shapely.geometry import LineString

from app.services.shade_engine.buildings import (
    BuildingFootprint,
    HeightProvenance,
    SyntheticBuildingProvider,
)
from app.services.shade_engine.geo import LocalFrame, haversine_distance_m
from app.services.shade_engine.solar import solar_position
from app.services.shade_engine.time_aware import (
    STATE_EPSILON_M,
    TIME_STEP_SECONDS,
    UNKNOWN_HEIGHT_ENVELOPE_M,
    TimeAwareShadeEngine,
    WalkLegInput,
    _build_slices,
    _spans_along,
    _subtract_spans,
)
from app.services.shade_engine.wiring import resolve_dataset_path

MSK = timezone(timedelta(hours=3))
FRAME = LocalFrame(lat0=59.9398, lon0=30.3146)
SCENARIOS = Path(__file__).parent / "fixtures/c7_1_scenarios.json"


def _block(x: float, y: float, width: float, depth: float, height: float | None,
           provenance: HeightProvenance) -> BuildingFootprint:
    corners = [(x - width / 2, y - depth / 2), (x + width / 2, y - depth / 2),
               (x + width / 2, y + depth / 2), (x - width / 2, y + depth / 2)]
    return BuildingFootprint(
        polygon_latlon=tuple(FRAME.to_latlon(px, py) for px, py in corners),
        height_m=height, provenance=provenance,
        source="SYNTHETIC TEST FIXTURE — NOT REAL SPb DATA")


BLOCKS = SyntheticBuildingProvider(footprints=(
    _block(0.0, 0.0, 40.0, 20.0, 25.0, HeightProvenance.OSM_DECLARED),
    _block(0.0, 60.0, 40.0, 20.0, 16.5, HeightProvenance.LEVELS_ESTIMATE),
    _block(0.0, 120.0, 40.0, 20.0, 20.0, HeightProvenance.UNAVAILABLE),
    _block(0.0, 180.0, 40.0, 20.0, 30.0, HeightProvenance.OSM_DECLARED),
))


def _ew(y: float, x_from: float = -80.0, x_to: float = 80.0) -> tuple[tuple[float, float], ...]:
    return (FRAME.to_latlon(x_from, y), FRAME.to_latlon(x_to, y))


@pytest.fixture()
def synthetic_engine() -> TimeAwareShadeEngine:
    return TimeAwareShadeEngine(building_provider=BLOCKS)


# ---------------------------------------------------------------- algorithm
def test_short_walk_uses_one_solar_bucket(synthetic_engine):
    departure = datetime(2026, 7, 1, 14, 0, tzinfo=MSK)
    legs = [WalkLegInput(geometry=_ew(27.0), walking_duration_seconds=300.0)]
    result = synthetic_engine.analyze(legs, departure)
    assert result.solar.bucket_count == 1
    assert result.status in ("available", "partial")
    assert result.coverage["coverage_share"] == 1.0


def test_four_hour_walk_sees_the_sun_move(synthetic_engine):
    """§10.1: a four-hour walk must NOT use the departure solar position.

    Around a July midday the elevation changes little (the sun is near its
    peak) but the azimuth swings tens of degrees — which is exactly what
    turns shadows during a long walk.
    """
    departure = datetime(2026, 7, 1, 12, 0, tzinfo=MSK)
    leg = WalkLegInput(geometry=_ew(27.0), walking_duration_seconds=4 * 3600.0)
    result = synthetic_engine.analyze([leg], departure)
    # Solar positions are computed per spatial slice (4 slices over 160 m here),
    # not per wall-clock step — no position is computed for ground nobody walks.
    assert result.solar.bucket_count >= 4
    assert result.solar.bucket_count <= 25
    assert result.solar.max_azimuth_deg - result.solar.min_azimuth_deg > 40.0
    assert abs(result.solar.max_elevation_deg - result.solar.min_elevation_deg) > 5.0


def test_visit_time_advances_the_sun_for_later_legs(synthetic_engine):
    """A 30-minute museum visit shifts the second leg's solar position."""
    departure = datetime(2026, 7, 1, 15, 0, tzinfo=MSK)
    geometry = _ew(27.0)
    without_visit = synthetic_engine.analyze(
        [WalkLegInput(geometry, 600.0), WalkLegInput(geometry, 600.0)], departure)
    with_visit = synthetic_engine.analyze(
        [WalkLegInput(geometry, 600.0, 1800.0), WalkLegInput(geometry, 600.0)], departure)
    assert with_visit.evaluated_period_end > without_visit.evaluated_period_end
    start = datetime.fromisoformat(with_visit.evaluated_period_start)
    end = datetime.fromisoformat(with_visit.evaluated_period_end)
    assert (end - start).total_seconds() == pytest.approx(600 + 1800 + 600, abs=1.0)


def test_no_phantom_visits_are_added_for_scenic_waypoints(synthetic_engine):
    """Scenic waypoints are interior vertices: they must add NO visit time."""
    departure = datetime(2026, 7, 1, 14, 0, tzinfo=MSK)
    multi_vertex = (FRAME.to_latlon(-80, 27), FRAME.to_latlon(-20, 27),
                    FRAME.to_latlon(20, 27), FRAME.to_latlon(80, 27))
    result = synthetic_engine.analyze([WalkLegInput(multi_vertex, 900.0)], departure)
    start = datetime.fromisoformat(result.evaluated_period_start)
    end = datetime.fromisoformat(result.evaluated_period_end)
    assert (end - start).total_seconds() == pytest.approx(900.0, abs=1.0)


def test_speed_change_scales_time_but_not_geometry(synthetic_engine):
    departure = datetime(2026, 7, 1, 14, 0, tzinfo=MSK)
    geometry = _ew(27.0)
    slow = synthetic_engine.analyze([WalkLegInput(geometry, 1200.0)], departure)
    fast = synthetic_engine.analyze([WalkLegInput(geometry, 300.0)], departure)
    assert slow.total_length_m == pytest.approx(fast.total_length_m)
    assert slow.solar.bucket_count >= fast.solar.bucket_count
    assert slow.shaded_length_m == pytest.approx(fast.shaded_length_m, rel=0.2)


def test_start_time_change_changes_the_estimate(synthetic_engine):
    geometry = _ew(27.0, x_from=-50, x_to=50)
    midday = synthetic_engine.analyze([WalkLegInput(geometry, 600.0)],
                                      datetime(2026, 7, 1, 13, 0, tzinfo=MSK))
    evening = synthetic_engine.analyze([WalkLegInput(geometry, 600.0)],
                                       datetime(2026, 7, 1, 21, 0, tzinfo=MSK))
    # Different solar geometry ⇒ different shadow bands (never the same number
    # unless the sun happened to be symmetric, which these times are not).
    assert midday.shaded_length_m != pytest.approx(evening.shaded_length_m, abs=0.5)


def test_analysis_is_deterministic(synthetic_engine):
    departure = datetime(2026, 7, 1, 14, 0, tzinfo=MSK)
    legs = [WalkLegInput(_ew(27.0), 600.0, 300.0), WalkLegInput(_ew(-40.0), 400.0)]
    first = synthetic_engine.analyze(legs, departure, include_slices=True)
    second = synthetic_engine.analyze(legs, departure, include_slices=True)
    assert first == second


def test_unknown_band_guards_against_claiming_sun(synthetic_engine):
    departure = datetime(2026, 7, 1, 14, 0, tzinfo=MSK)
    # The height-less block is at y = 120; a route crossing y = 100..140 lies in
    # its envelope and must be unknown, not exposed.
    route = (FRAME.to_latlon(0.0, 100.0), FRAME.to_latlon(0.0, 140.0))
    result = synthetic_engine.analyze([WalkLegInput(route, 600.0)], departure,
                                      include_slices=True)
    assert result.unknown_length_m > 0
    assert result.exposed_length_m < result.total_length_m
    assert any(s.state in ("unknown", "mixed") for s in result.slices)


def test_invariant_holds(synthetic_engine):
    departure = datetime(2026, 7, 1, 14, 0, tzinfo=MSK)
    legs = [WalkLegInput(_ew(27.0), 600.0, 900.0), WalkLegInput(_ew(-40.0), 600.0)]
    result = synthetic_engine.analyze(legs, departure)
    total = (result.shaded_length_m + result.exposed_length_m + result.unknown_length_m)
    assert total == pytest.approx(result.total_length_m, abs=1e-6)
    assert haversine_distance_m(*legs[0].geometry[0], *legs[0].geometry[1]) > 0


# ------------------------------------------------------- §19 real scenarios
def _dataset_path() -> Path | None:
    path = resolve_dataset_path()
    return path if path and path.exists() else None


def _scenarios() -> dict:
    return json.loads(SCENARIOS.read_text(encoding="utf-8"))["scenarios"]


@pytest.fixture()
def dataset_engine() -> TimeAwareShadeEngine:
    from app.services.shade_engine.building_store import SqliteBuildingGeometryProvider

    path = _dataset_path()
    if path is None:
        pytest.skip("pilot dataset not built (run scripts.build_phase_c7_1_buildings)")
    provider = SqliteBuildingGeometryProvider(path)
    if not provider.available():
        pytest.skip("pilot dataset not usable")
    return TimeAwareShadeEngine(building_provider=provider)


@pytest.mark.parametrize("scenario", ["gostiny_dvor_arcade", "palace_square_open", "isaac_blocks"])
def test_real_scenario_shade_is_plausible_and_invariant_holds(dataset_engine, scenario):
    entry = _scenarios()[scenario]
    geometry = tuple((lat, lon) for lat, lon in entry["geometry_lat_lon"])
    length = sum(haversine_distance_m(a[0], a[1], b[0], b[1])
                 for a, b in zip(geometry, geometry[1:]))
    result = dataset_engine.analyze(
        [WalkLegInput(geometry, length / 1.35)],  # 4.9 km/h walking pace
        datetime(2026, 7, 1, 14, 0, tzinfo=MSK))
    assert result.status in ("available", "partial")
    assert result.shaded_length_m + result.exposed_length_m + result.unknown_length_m \
        == pytest.approx(result.total_length_m, abs=1e-6)
    assert 0.0 <= result.shade_fraction <= 1.0
    assert result.coverage["coverage_share"] == 1.0  # scenarios sit inside mapped zones
    # Honesty: the pilot has no externally verified heights, so confidence can
    # never be "high" here (§9).
    assert result.confidence in ("medium", "low")


def test_real_scenarios_behave_as_their_character_describes(dataset_engine):
    """Physical sanity on real OSM geometry, per scenario character.

    `gostiny_dvor_arcade` is the enclosed arcade loop of Gostiny Dvor: shaded
    at any time of day. `palace_square_open` is the open square perimeter:
    sunlit in the morning and at midday, and only reached by the low evening
    sun. `isaac_blocks` runs through dense blocks: more shade in the morning
    (low sun, long shadows) than at midday.

    Values are model outputs at the recorded dataset version — regression
    anchors, not field measurements.
    """
    scenarios = _scenarios()

    def shade_at(key: str, hour: int) -> float:
        entry = scenarios[key]
        geometry = tuple((lat, lon) for lat, lon in entry["geometry_lat_lon"])
        length = sum(haversine_distance_m(a[0], a[1], b[0], b[1])
                     for a, b in zip(geometry, geometry[1:]))
        result = dataset_engine.analyze(
            [WalkLegInput(geometry, length / 1.35)],
            datetime(2026, 7, 1, hour, 0, tzinfo=MSK))
        assert result.shade_fraction is not None
        return result.shade_fraction

    arcade_morning = shade_at("gostiny_dvor_arcade", 9)
    arcade_midday = shade_at("gostiny_dvor_arcade", 14)
    assert arcade_morning > 0.8, "an enclosed arcade must be shaded"
    assert arcade_midday > 0.8

    square_morning = shade_at("palace_square_open", 9)
    square_midday = shade_at("palace_square_open", 14)
    assert square_midday < 0.2, "an open square must be sunlit at midday"
    assert square_morning < 0.2

    blocks_morning = shade_at("isaac_blocks", 9)
    blocks_midday = shade_at("isaac_blocks", 14)
    assert blocks_morning > blocks_midday, "low morning sun casts longer shadows"
    assert blocks_morning > 0.3
    assert square_midday < blocks_midday, "a square is more exposed than dense blocks"


def test_outside_pilot_coverage_is_unknown_not_sun(dataset_engine):
    # A real route far from every pilot zone (Kupchino is mapped, this is not).
    route = ((59.8800, 30.3400), (59.8810, 30.3400))  # between pilot zones
    result = dataset_engine.analyze([WalkLegInput(route, 300.0)],
                                    datetime(2026, 7, 1, 14, 0, tzinfo=MSK))
    assert result.status in ("outside_coverage", "partial")
    assert result.coverage["outside_coverage_meters"] > 0
    # Either the provider found no buildings at all (unavailable) or the
    # uncovered part is reported as unknown — never as sun.
    if result.status != "unavailable":
        assert result.unknown_length_m > 0


# =========================================================================
# C7.1.1 geometry hotfix regressions: turns inside slices, measured shade
# POSITION, unknown band minus confirmed shadow, honest whole-slice states.
# =========================================================================
def _local_route(*xy_points: tuple[float, float]) -> tuple[tuple[float, float], ...]:
    return tuple(FRAME.to_latlon(x, y) for x, y in xy_points)


def _down_sun_unit(azimuth_deg: float) -> tuple[float, float]:
    """Horizontal direction a building's shadow is displaced towards."""
    rad = math.radians(azimuth_deg)
    return (-math.sin(rad), -math.cos(rad))


def _bucket_mid_solar(departure: datetime):
    """The sun the engine actually uses for the FIRST 10-minute bucket.

    Slices are classified at their bucket's mid-time (§10.2), so fixtures must
    be placed against the same instant instead of the departure itself — the
    missing 5 minutes rotate the shadow by ~2°.
    """
    return solar_position(FRAME.lat0, FRAME.lon0,
                          departure + timedelta(seconds=TIME_STEP_SECONDS / 2))


def _local_rect(centre: tuple[float, float], *, half_u: float, half_v: float,
                u_axis: tuple[float, float], height: float | None,
                provenance: HeightProvenance) -> BuildingFootprint:
    """Rectangle whose axes follow the sun direction ``u`` and its normal."""
    ux, uy = u_axis
    vx, vy = -uy, ux
    cx, cy = centre
    corners = [(cx + a * half_u * ux + b * half_v * vx,
                cy + a * half_u * uy + b * half_v * vy)
               for a, b in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
    return BuildingFootprint(
        polygon_latlon=tuple(FRAME.to_latlon(x, y) for x, y in corners),
        height_m=height, provenance=provenance,
        source="SYNTHETIC TEST FIXTURE — NOT REAL SPb DATA")


def _assert_slice_contract(result) -> None:
    """Spans are complete, disjoint, inside the slice and sum to the lengths."""
    for slice_ in result.slices:
        spans = sorted([*slice_.shaded_spans_m, *slice_.unknown_spans_m])
        for start, end in spans:
            assert 0.0 <= start <= end <= slice_.length_m + 1e-6
        for (_a0, a1), (b0, _b1) in zip(spans, spans[1:]):
            assert a1 <= b0 + 1e-6, "спаны тени и «неизвестно» не пересекаются"
        assert sum(b - a for a, b in slice_.shaded_spans_m) == pytest.approx(
            slice_.shaded_m, abs=1e-6)
        assert sum(b - a for a, b in slice_.unknown_spans_m) == pytest.approx(
            slice_.unknown_m, abs=1e-6)
        state_matches = {
            "estimated_exposed":
                slice_.shaded_m <= STATE_EPSILON_M
                and slice_.unknown_m <= STATE_EPSILON_M,
            "estimated_shaded":
                slice_.length_m - slice_.shaded_m <= STATE_EPSILON_M
                and slice_.unknown_m <= STATE_EPSILON_M,
            "unknown":
                slice_.length_m - slice_.unknown_m <= STATE_EPSILON_M
                and slice_.shaded_m <= STATE_EPSILON_M,
            "mixed":
                (slice_.shaded_m > STATE_EPSILON_M
                 and slice_.length_m - slice_.shaded_m > STATE_EPSILON_M)
                or (slice_.unknown_m > STATE_EPSILON_M
                    and slice_.length_m - slice_.unknown_m > STATE_EPSILON_M),
        }
        assert state_matches[slice_.state], (
            f"состояние {slice_.state} не соответствует измерениям участка")


def test_slices_keep_every_turn_of_the_real_path():
    """C7.1.1: a slice is the REAL polyline, never a chord between two cuts.

    The route turns at 60 m, i.e. INSIDE the second 40 m slice. The old chord
    version silently replaced the corner by a diagonal and under-measured the
    walk; the polyline version keeps the corner vertex verbatim.
    """
    corner = (60.0, 0.0)
    leg = WalkLegInput(_local_route((0.0, 0.0), corner, (60.0, -60.0)), 600.0)
    slices = _build_slices([leg], zones=(), touched_zones=set())

    assert len(slices) == 3
    assert all(s.length_m == pytest.approx(40.0, abs=1e-6) for s in slices)

    turning = slices[1]
    local_points = [FRAME.to_xy(lat, lon) for lat, lon in turning.geometry]
    assert len(local_points) == 3, "поворот внутри участка сохраняется вершиной"
    assert local_points[1] == pytest.approx(corner, abs=1e-6)

    for slice_ in slices:
        polyline_length = sum(haversine_distance_m(a[0], a[1], b[0], b[1])
                              for a, b in zip(slice_.geometry, slice_.geometry[1:]))
        assert polyline_length == pytest.approx(slice_.length_m, abs=1e-6), (
            "длина участка равна длине его реальной полилинии, а не хорды")

    chain = [slices[0].geometry[0]]
    for slice_ in slices:
        chain.extend(slice_.geometry[1:])
    chain_local = [value for lat, lon in chain for value in FRAME.to_xy(lat, lon)]
    assert chain_local == pytest.approx(
        [0.0, 0.0, 40.0, 0.0, 60.0, 0.0, 60.0, -20.0, 60.0, -60.0], abs=1e-6)


def test_span_subtraction_is_exact_interval_arithmetic():
    assert _subtract_spans([(0.0, 10.0), (20.0, 30.0)], [(5.0, 25.0)]) == \
        [(0.0, 5.0), (25.0, 30.0)]
    assert _subtract_spans([(0.0, 40.0)], [(0.0, 40.0)]) == []
    assert _subtract_spans([(0.0, 40.0)], []) == [(0.0, 40.0)]


def test_spans_report_where_the_shadow_is_not_how_much():
    from shapely.geometry import box

    line = LineString([(0.0, 0.0), (40.0, 0.0)])
    assert _spans_along(line, line.intersection(box(20.0, -5.0, 80.0, 5.0))) \
        == [(20.0, 40.0)]
    assert _spans_along(line, line.intersection(box(-20.0, -5.0, 15.0, 5.0))) \
        == [(0.0, 15.0)]


def test_shadow_position_inside_a_slice_is_measured_and_labelled_mixed():
    """A slice with a band of shade in its MIDDLE is `mixed` with a span.

    The old contract marked the whole slice `estimated_shaded`, and the client
    painted its first 20 m — an invented boundary over genuinely sunlit ground.
    """
    departure = datetime(2026, 7, 1, 13, 0, tzinfo=MSK)
    solar = _bucket_mid_solar(departure)
    unit = _down_sun_unit(solar.azimuth_deg)
    shadow_length = 20.0 / math.tan(math.radians(solar.elevation_deg))
    # The block sits up-sun of the point (40, 0) of the route, so its shadow
    # is guaranteed to cover that point whatever the sun direction is.
    centre = (40.0 - unit[0] * (shadow_length / 2.0 + 5.0),
              0.0 - unit[1] * (shadow_length / 2.0 + 5.0))
    provider = SyntheticBuildingProvider(footprints=(
        _local_rect(centre, half_u=10.0, half_v=5.0, u_axis=unit, height=20.0,
                    provenance=HeightProvenance.OSM_DECLARED),
    ))
    engine = TimeAwareShadeEngine(building_provider=provider)
    # One 40 m slice covering x in [20, 60]: the guaranteed shaded point (40, 0)
    # lies 20 m into it, well inside the slice and well inside the band.
    result = engine.analyze(
        [WalkLegInput(_local_route((20.0, 0.0), (60.0, 0.0)), 600.0)],
        departure, include_slices=True)

    _assert_slice_contract(result)
    assert len(result.slices) == 1
    slice_ = result.slices[0]
    assert 0.0 < slice_.shaded_m < slice_.length_m - STATE_EPSILON_M
    assert slice_.state == "mixed", (
        "участок с полосой тени в середине не может считаться полностью теневым")
    assert any(start <= 20.0 <= end for start, end in slice_.shaded_spans_m), (
        "измеренный интервал содержит заведомо теневую точку маршрута")


def test_unknown_band_excludes_ground_already_measured_as_confirmed_shadow():
    """Overlapping envelope metres are attributed ONCE, to the confirmed shadow.

    Two blocks stand on the same sun axis: a known-height one (20 m) placed
    down-sun inside the envelope of an unknown-height one. Every metre of
    envelope that crosses the route is already measured shadow of the known
    block, so the route must report ``unknown = 0``. The old arithmetic added
    those metres a second time and moved genuinely sunlit ground into
    ``unknown`` while the sum-invariant still held.
    """
    departure = datetime(2026, 7, 1, 13, 0, tzinfo=MSK)
    solar = _bucket_mid_solar(departure)
    unit = _down_sun_unit(solar.azimuth_deg)
    known_shadow = 20.0 / math.tan(math.radians(solar.elevation_deg))
    envelope_reach = UNKNOWN_HEIGHT_ENVELOPE_M / math.tan(
        math.radians(solar.elevation_deg))
    assert 1.5 * known_shadow <= envelope_reach, (
        "предпосылка теста: тень известного блока целиком внутри конверта")
    provider = SyntheticBuildingProvider(footprints=(
        _local_rect((0.0, 0.0), half_u=10.0, half_v=30.0, u_axis=unit,
                    height=None, provenance=HeightProvenance.UNAVAILABLE),
        _local_rect((unit[0] * known_shadow / 2.0, unit[1] * known_shadow / 2.0),
                    half_u=10.0, half_v=30.0, u_axis=unit, height=20.0,
                    provenance=HeightProvenance.OSM_DECLARED),
    ))
    engine = TimeAwareShadeEngine(building_provider=provider)
    # The route crosses both 60 m bands strictly inside the known shadow AND
    # strictly inside the envelope: 20 m of it lies outside both.
    lateral = known_shadow / 2.0 + 15.0
    perp = (-unit[1], unit[0])
    start = (perp[0] * -40.0 + unit[0] * lateral, perp[1] * -40.0 + unit[1] * lateral)
    end = (perp[0] * 40.0 + unit[0] * lateral, perp[1] * 40.0 + unit[1] * lateral)
    result = engine.analyze([WalkLegInput(_local_route(start, end), 600.0)],
                            departure, include_slices=True)

    _assert_slice_contract(result)
    assert result.shaded_length_m == pytest.approx(60.0, abs=1.5)
    # The old arithmetic reported ~20 m of `unknown` here (the overlap metres
    # counted a second time); the residual micrometres are float dust of the
    # planar→geodesic renormalisation, not unclassified ground.
    assert result.unknown_length_m < 0.01, (
        "метры, уже измеренные как тень известного здания, не засчитываются дважды")
    assert result.exposed_length_m == pytest.approx(
        result.total_length_m - result.shaded_length_m, abs=1.5)
    assert all(end - start < 0.01
               for s in result.slices for start, end in s.unknown_spans_m)


def test_outside_coverage_slice_is_wholly_unknown_with_a_full_span():
    class _ZonedProvider(SyntheticBuildingProvider):
        def zone_boxes(self):  # test double: declares coverage elsewhere
            return (("elsewhere", 0.0, 0.0, 0.1, 0.1),)

    provider = _ZonedProvider(footprints=(
        _block(0.0, 0.0, 40.0, 20.0, 25.0, HeightProvenance.OSM_DECLARED),))
    engine = TimeAwareShadeEngine(building_provider=provider)
    result = engine.analyze([WalkLegInput(_ew(27.0), 600.0)],
                            datetime(2026, 7, 1, 14, 0, tzinfo=MSK),
                            include_slices=True)
    _assert_slice_contract(result)
    assert result.status == "outside_coverage"
    assert all(s.state == "unknown" for s in result.slices)
    assert all(s.unknown_spans_m == ((0.0, s.length_m),) for s in result.slices)
    assert all(s.shaded_spans_m == () for s in result.slices)
