"""C7.2 engine scenarios (ТЗ §6): the deterministic v1 assessment of a walk.

Synthetic fixtures prove processing logic; the two real-dataset tests prove
that the same engine reads the pinned OSM pilot correctly. Accuracy of the
risk model itself is NOT claimed by any test here (see PHASE_C7_2_MODEL.md).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.services.puddle_engine.engine import (
    METHOD,
    METHOD_VERSION,
    PuddleAnalysisInputError,
    PuddleRiskEngine,
    _bin_inputs,
    _build_slices,
    _freezing_or_snow,
    _history_complete,
)
from app.services.puddle_engine.ways_store import (
    EmptyPuddleWaysProvider,
    JsonPuddleWaysProvider,
)
from app.services.puddle_engine.weather import WeatherHour, WeatherSeries
from tests.puddle_fixtures import latlon, leg, series, way, SyntheticWaysProvider

T0 = datetime(2026, 6, 1, 14, 0, tzinfo=timezone.utc)
REAL_DATASET = Path(__file__).resolve().parents[1] / "data/puddle_ways_spb_pilot.json"


def engine(ways, *, now=T0, **kwargs):
    return PuddleRiskEngine(ways_provider=SyntheticWaysProvider(tuple(ways)),
                            clock=lambda: now, **kwargs)


def route(provider_way, *, seconds=300.0):
    return [leg(provider_way.geometry, seconds)]


def analyze(ways, weather=None, *, departure=T0, now=T0, legs=None, **kwargs):
    ways = tuple(ways)
    run = engine(ways, now=now, **kwargs)
    return run.analyze(legs if legs is not None else route(ways[0]), departure,
                       weather if weather is not None else series(departure))


def test_dry_weather_is_low_with_full_coverage():
    asphalt = way(1, 0.0, surface="asphalt")
    result = analyze([asphalt])
    assert result.status == "available"
    assert result.risk_level == "low"
    assert result.risk_index == 0.0
    assert result.coverage_share == 1.0
    assert result.unknown_meters == 0.0
    assert result.confidence == "low"
    assert "model_precipitation_history" in result.reasons
    assert "experimental_uncalibrated" in result.reasons


def test_heavy_recent_rain_raises_the_index_for_impervious_surfaces():
    asphalt = way(1, 0.0, surface="asphalt")
    result = analyze([asphalt], series(T0, rain=lambda age: 1.5 if 0 <= age < 2 else 0.0))
    assert result.status == "available"
    assert result.risk_level in {"moderate", "high"}
    assert result.risk_index > 0.5


def test_ponding_prone_paving_scores_strictly_higher_than_asphalt():
    asphalt = way(1, 0.0, surface="asphalt")
    stones = way(2, 0.0, surface="paving_stones")
    weather = series(T0, rain=lambda age: 0.6 if 0 <= age < 2 else 0.0)
    asphalt_result = analyze([asphalt], weather)
    stones_result = analyze([stones], weather)
    assert stones_result.risk_index > asphalt_result.risk_index
    assert stones_result.known_meters == pytest.approx(asphalt_result.known_meters)


def test_rain_that_stopped_long_ago_lowers_the_index():
    asphalt = way(1, 0.0, surface="asphalt")
    now_rain = analyze([asphalt], series(T0, rain=lambda age: 2.0 if 0 <= age < 2 else 0.0))
    old_rain = analyze([asphalt], series(T0, rain=lambda age: 2.0 if 8 <= age < 10 else 0.0))
    assert old_rain.risk_index < now_rain.risk_index


def test_prolonged_rain_is_wetter_than_a_short_shower():
    asphalt = way(1, 0.0, surface="asphalt")
    short = analyze([asphalt], series(T0, rain=lambda age: 1.0 if 0 <= age < 1 else 0.0))
    long_ = analyze([asphalt], series(T0, rain=lambda age: 1.0 if 0 <= age < 5 else 0.0))
    assert long_.risk_index >= short.risk_index


def test_unmapped_surface_is_unknown_and_never_low():
    bare = way(1, 0.0)  # highway=footway with no surface tag at all
    result = analyze([bare], series(T0, rain=lambda age: 1.5 if 0 <= age < 2 else 0.0))
    assert result.status == "unavailable"
    assert result.risk_level == "unknown"
    assert result.known_meters == 0.0
    assert result.unknown_meters == pytest.approx(result.total_length_m)
    assert "surface_unknown" in result.reasons
    assert result.risk_index is None


def test_unmapped_surface_beside_a_mapped_one_gives_partial_with_no_fake_low():
    mapped = way(1, 0.0, surface="asphalt")
    bare = way(2, 12.0)  # same corridor cannot match at 12 m — a second leg
    weather = series(T0, rain=lambda age: 1.5 if 0 <= age < 2 else 0.0)
    legs = [leg(mapped.geometry), leg(bare.geometry)]
    result = analyze([mapped, bare], weather, legs=legs)
    assert result.status == "partial"
    assert result.known_meters > 0 and result.unknown_meters > 0
    assert result.known_meters + result.unknown_meters == pytest.approx(result.total_length_m)
    assert "surface_unknown" in result.reasons
    assert result.risk_level != "unknown"  # a level exists for the KNOWN part


def test_unsupported_surface_value_is_reported_distinctly():
    passage = way(1, 0.0, surface="building_passage")
    result = analyze([passage])
    assert "surface_not_supported" in result.reasons


def test_covered_passage_lowers_but_never_zeroes_the_index():
    open_way = way(1, 0.0, surface="paving_stones")
    roofed = way(2, 0.0, surface="paving_stones", covered="yes")
    weather = series(T0, rain=lambda age: 2.0 if 0 <= age < 3 else 0.0)
    open_result = analyze([open_way], weather)
    roof_result = analyze([roofed], weather)
    assert roof_result.risk_index < open_result.risk_index
    assert roof_result.risk_index > 0.0
    assert "covered_not_waterproof" in roof_result.reasons


def test_ambiguous_parallels_stay_unknown():
    first = way(1, 0.0, surface="asphalt")
    second = way(2, 3.0, surface="paving_stones")
    result = analyze([first, second])
    assert result.status == "unavailable"
    assert result.known_meters == 0.0
    assert "ambiguous_osm_way" in result.reasons
    assert result.diagnostics["ambiguous_slices"] == result.diagnostics["slices"]


def test_unmatched_slices_are_unknown_not_dry():
    distant = way(1, 300.0, surface="asphalt")
    near = way(2, 0.0, surface="asphalt")
    legs = [leg(near.geometry)]
    result = analyze([distant, near], legs=legs)
    assert result.status == "available"  # near matches
    far = analyze([distant], legs=[leg(distant.geometry)])
    assert far.status == "available"

    # A route with no matching way at all:
    alone = analyze([distant], legs=[leg(near.geometry)])
    assert alone.status == "unavailable"
    assert alone.known_meters == 0.0
    assert "unmatched_osm_way" in alone.reasons


def test_route_entirely_outside_pilot_is_outside_coverage():
    far_away = (latlon(5000.0, 5000.0), latlon(5100.0, 5000.0))
    ways = [way(1, 0.0, surface="asphalt")]
    result = analyze(ways, legs=[leg(far_away)])
    assert result.status == "outside_coverage"
    assert result.risk_level == "unknown"
    assert result.known_meters == 0.0
    assert "outside_pilot" in result.reasons


def test_route_partly_outside_pilot_is_partial_with_outside_reason():
    inside = way(1, 0.0, surface="asphalt")
    outside_point = latlon(5000.0, 5000.0)
    legs = [leg(inside.geometry), leg((outside_point, latlon(5100.0, 5000.0)))]
    result = analyze([inside], legs=legs)
    assert result.status == "partial"
    assert "outside_pilot" in result.reasons
    assert result.known_meters > 0


def test_poi_visit_shifts_later_legs_into_the_rain():
    asphalt = way(1, 0.0, surface="asphalt")
    # Rain bins cover (T0+1h, T0+2h] and (T0+2h, T0+3h]: the walk starts dry,
    # so the FIRST leg must be dry no matter what, and only the visit can push
    # the second leg into the rainy hours.
    weather = series(T0, rain=lambda age: 2.5 if age in (-2, -3) else 0.0)
    no_visit = engine([asphalt]).analyze(
        [leg(asphalt.geometry, seconds=600.0)], T0, weather)
    with_visit = engine([asphalt]).analyze(
        [leg(asphalt.geometry, seconds=600.0, visit=7200.0),
         leg(asphalt.geometry, seconds=600.0)], T0, weather)
    assert no_visit.risk_index == 0.0
    assert with_visit.risk_index > no_visit.risk_index


def test_departure_time_changes_the_assessment():
    asphalt = way(1, 0.0, surface="asphalt")
    weather = series(T0, rain=lambda age: 2.5 if 3 <= age < 5 else 0.0)
    in_rain = engine([asphalt]).analyze(
        [leg(asphalt.geometry)], T0 + timedelta(hours=3.5), weather)
    after_rain = engine([asphalt]).analyze(
        [leg(asphalt.geometry)], T0 + timedelta(hours=9), weather)
    assert in_rain.risk_index > after_rain.risk_index


def _one_bin(ends_at: datetime, mm: float) -> WeatherSeries:
    return WeatherSeries(
        hours=(WeatherHour(ends_at=ends_at, precipitation_mm=mm,
                           temperature_c=10.0, snowfall_cm=0.0),),
        retrieved_at=T0, valid_until=T0 + timedelta(minutes=15))


def test_a_still_falling_bin_contributes_only_its_elapsed_share():
    """Review C7.2.1 #3: at 12:01 the bin covering (12:00, 13:00] must not be
    counted in full as current rain — only the already-fallen part counts."""
    series_ = _one_bin(T0 + timedelta(hours=1), 6.0)  # bin (T0, T0+1h]
    history, current = _bin_inputs(series_, T0)
    assert (history, current) == ([], 0.0)  # not started: nothing has fallen

    history, current = _bin_inputs(series_, T0 + timedelta(minutes=1))
    assert history == [] and current == pytest.approx(6.0 / 60.0)

    history, current = _bin_inputs(series_, T0 + timedelta(minutes=30))
    assert history == [] and current == pytest.approx(3.0)


def test_a_finished_bin_is_fully_counted_and_later_decayed():
    series_ = _one_bin(T0 + timedelta(hours=1), 6.0)
    end = T0 + timedelta(hours=1)

    history, current = _bin_inputs(series_, end)  # exactly at its end
    assert history == [] and current == pytest.approx(6.0)

    history, current = _bin_inputs(series_, end + timedelta(minutes=59))
    assert history == [] and current == pytest.approx(6.0)

    history, current = _bin_inputs(series_, end + timedelta(hours=1))
    assert current == 0.0 and len(history) == 1
    amount, age = history[0]
    assert amount == pytest.approx(6.0) and age == pytest.approx(1.0)


def test_forecast_for_the_current_hour_does_not_count_in_full():
    """The reviewer's exact case at the engine level: a 3 mm forecast for the
    hour the walk starts in must not lift the walk to "high" on the strength
    of the whole forecast hour."""
    asphalt = way(1, 0.0, surface="asphalt")
    weather = series(T0, rain=lambda age: 3.0 if age == -1 else 0.0)
    result = engine([asphalt]).analyze(
        [leg(asphalt.geometry, seconds=300.0)], T0, weather)
    assert result.status == "available"
    assert result.risk_level == "low"
    # Deterministic: asphalt saturates at 3 mm, so the index of each 40 m
    # slice equals the fallen share of its crossing midpoint, and the walk's
    # index is the metre-weighted mean of those shares = 150 s / 3600 s. The
    # old rule (whole bin as current) would have produced 1.0 = "high".
    assert result.risk_index == pytest.approx(150.0 / 3600.0, rel=1e-9)


def test_the_same_rain_counts_fully_once_it_has_fallen():
    asphalt = way(1, 0.0, surface="asphalt")
    weather = series(T0, rain=lambda age: 3.0 if age == -1 else 0.0)
    during = engine([asphalt]).analyze([leg(asphalt.geometry)], T0, weather)
    after = engine([asphalt]).analyze(
        [leg(asphalt.geometry)], T0 + timedelta(hours=2), weather)
    assert after.risk_index > during.risk_index
    assert after.risk_level in {"moderate", "high"}


def test_vertex_density_of_the_same_physical_line_changes_nothing():
    sparse = way(1, 0.0, points=2, surface="paving_stones")
    dense = way(2, 0.0, points=301, surface="paving_stones")
    weather = series(T0, rain=lambda age: 1.0 if 0 <= age < 3 else 0.0)
    sparse_result = engine([sparse]).analyze([leg(sparse.geometry)], T0, weather)
    dense_result = engine([dense]).analyze([leg(dense.geometry)], T0, weather)
    assert sparse_result.total_length_m == pytest.approx(
        dense_result.total_length_m, rel=1e-6)
    assert sparse_result.risk_index == pytest.approx(dense_result.risk_index, rel=1e-6)
    assert sparse_result.risk_level == dense_result.risk_level


def test_meter_weighted_aggregation_over_two_surfaces():
    asphalt = way(1, 0.0, surface="asphalt")
    stones = way(2, 60.0, surface="paving_stones")
    weather = series(T0, rain=lambda age: 0.5 if 0 <= age < 4 else 0.0)
    legs = [leg(asphalt.geometry), leg(stones.geometry)]
    run = engine([asphalt, stones])
    result = run.analyze(legs, T0, weather)
    assert result.known_meters == pytest.approx(result.total_length_m)
    assert (result.low_meters + result.moderate_meters
            + result.high_meters) == pytest.approx(result.known_meters)
    assert result.high_meters > 0  # stones pond sooner than the same rain on asphalt

    # The route index is the METRE-weighted mean of the segment indices —
    # never a mean over vertices or segment counts.
    detailed = run.analyze(legs, T0, weather, include_segments=True)
    classified = [segment for segment in detailed.segments
                  if segment.risk_index is not None]
    weighted = sum(segment.risk_index * segment.length_m for segment in classified)
    known = sum(segment.length_m for segment in classified)
    assert result.risk_index == pytest.approx(weighted / known, abs=2e-3)


def test_snow_or_freezing_makes_the_model_inapplicable():
    asphalt = way(1, 0.0, surface="asphalt")
    result = analyze([asphalt], series(T0, temperature=-3.0))
    assert result.status == "unavailable"
    assert result.risk_level == "unknown"
    assert result.known_meters == 0.0
    assert "weather_snow_or_freezing" in result.reasons


def test_snowfall_alone_is_also_refused():
    asphalt = way(1, 0.0, surface="asphalt")
    result = analyze([asphalt], series(T0, snowfall=0.4))
    assert "weather_snow_or_freezing" in result.reasons


def test_missing_weather_is_unavailable_with_its_reason():
    asphalt = way(1, 0.0, surface="asphalt")
    result = engine([asphalt]).analyze([leg(asphalt.geometry)], T0, None,
                                       "weather_not_configured")
    assert result.status == "unavailable"
    assert result.reasons == ("weather_not_configured",)
    assert result.risk_index is None
    assert result.coverage_share == 0.0


def test_stale_weather_is_refused_instead_of_reused():
    asphalt = way(1, 0.0, surface="asphalt")
    weather = series(T0, retrieved_at=T0)
    result = engine([asphalt], now=T0 + timedelta(hours=2)).analyze(
        [leg(asphalt.geometry)], T0, weather)
    assert result.status == "unavailable"
    assert "weather_stale" in result.reasons


def test_departure_outside_the_weather_window_is_refused():
    asphalt = way(1, 0.0, surface="asphalt")
    weather = series(T0, hours_after=12)
    result = engine([asphalt]).analyze(
        [leg(asphalt.geometry)], T0 + timedelta(days=3), weather)
    assert "weather_outside_window" in result.reasons


def test_incomplete_history_never_produces_numbers():
    """Review C7.2.1 #2: a lookback window with missing hours cannot bound the
    decayed contribution of earlier rain, so the affected slices stay unknown.
    A numeric estimate there could UNDERSTATE the risk, which is exactly what
    the model must not do."""
    asphalt = way(1, 0.0, surface="asphalt")
    weather = series(T0, hours_before=6,
                     rain=lambda age: 1.0 if 0 <= age < 2 else 0.0)
    result = engine([asphalt], history_required_hours=12.0).analyze(
        [leg(asphalt.geometry)], T0, weather)
    assert result.status == "unavailable"
    assert result.risk_level == "unknown"
    assert result.known_meters == 0.0
    assert result.risk_index is None
    assert result.sensitivity == {}
    assert "weather_incomplete" in result.reasons
    assert result.diagnostics["history_incomplete_slices"] > 0


def test_only_the_slices_with_a_complete_history_are_estimated():
    asphalt = way(1, 0.0, surface="asphalt")
    # The series starts 9 h before the walk, so the 12-hour lookback is
    # incomplete for the walk's first half and complete for its second.
    weather = series(T0, hours_before=8,
                     rain=lambda age: 2.0 if 0 <= age < 2 else 0.0)
    walk = [leg(asphalt.geometry, seconds=6 * 3600.0)]
    run = engine([asphalt])
    result = run.analyze(walk, T0, weather)
    assert result.status == "partial"
    assert result.known_meters > 0 and result.unknown_meters > 0
    assert result.known_meters + result.unknown_meters == pytest.approx(
        result.total_length_m)
    assert result.risk_index is not None
    assert "weather_incomplete" in result.reasons

    detailed = run.analyze(walk, T0, weather, include_segments=True)
    assert detailed.segments[0].state == "unknown"
    assert detailed.segments[0].risk_index is None
    assert detailed.segments[0].reasons == ("weather_incomplete",)
    assert detailed.segments[-1].state in {"low", "moderate", "high"}
    assert detailed.segments[-1].risk_index is not None


def test_history_completeness_is_contiguous_coverage_not_a_record_count():
    """Method c7_2.3: the lookback window must be an unbroken chain of hourly
    bins. A count of records overstates coverage: with hourly-aligned bins a
    series can hold twelve records covering (T0−13h, T0−1h] — including one
    that touches the window's left edge but covers none of it — while the
    (T0−1h, T0] hour is missing entirely."""
    copied = [hour for age in range(1, 13) for hour in series(T0).hours
              if hour.ends_at == T0 - timedelta(hours=age)]
    assert len(copied) == 12
    fake = WeatherSeries(hours=tuple(copied), retrieved_at=T0,
                         valid_until=T0 + timedelta(minutes=15))
    assert _history_complete(fake, T0, 12.0) is False
    assert sorted(hour.ends_at for hour in fake.hours)[0] == T0 - timedelta(hours=12)
    assert _history_complete(series(T0), T0, 12.0) is True


def _contiguous_series(anchor: datetime, *, end_offset_hours: int = 48,
                       start_offset_hours: int = -12, rain=None) -> WeatherSeries:
    """Contiguous hourly bins ending ``anchor - offset`` for offsets
    ``end_offset_hours … start_offset_hours`` (explicit control over gaps)."""
    rain = rain or (lambda offset: 0.0)
    hours = tuple(
        WeatherHour(ends_at=anchor - timedelta(hours=offset),
                    precipitation_mm=float(rain(offset)),
                    temperature_c=12.0, snowfall_cm=0.0)
        for offset in range(end_offset_hours, start_offset_hours - 1, -1))
    return WeatherSeries(hours=hours, retrieved_at=T0,
                         valid_until=T0 + timedelta(minutes=15))


def test_missing_current_hour_is_not_fabricated_as_complete_history():
    """A series can carry a full set of older records, still cover the walk's
    window with later bins, and yet have no bin for the hour the walk starts
    in — the chain is broken exactly at the assessment moment. There must be
    no numbers: a zero-filled guess for the current hour would understate the
    most recent rain."""
    asphalt = way(1, 0.0, surface="asphalt")
    holed = tuple(hour for hour in series(T0).hours
                  if hour.ends_at != T0)  # the (T0-1h, T0] bin is missing
    weather = WeatherSeries(hours=holed, retrieved_at=T0,
                            valid_until=T0 + timedelta(minutes=15))
    result = engine([asphalt]).analyze([leg(asphalt.geometry)], T0, weather)
    assert result.status == "unavailable"
    assert result.known_meters == 0.0
    assert result.risk_index is None
    assert "weather_incomplete" in result.reasons
    assert result.diagnostics["history_incomplete_slices"] > 0


def test_an_internal_gap_in_the_window_leaves_slices_unknown():
    """A hole INSIDE the window breaks the chain even though the total number
    of records over the whole series stays large."""
    asphalt = way(1, 0.0, surface="asphalt")
    full = _contiguous_series(T0, rain=lambda offset: 0.4)
    holed = WeatherSeries(
        hours=tuple(hour for hour in full.hours
                    if hour.ends_at != T0 - timedelta(hours=6)),
        retrieved_at=T0, valid_until=T0 + timedelta(minutes=15))
    result = engine([asphalt]).analyze([leg(asphalt.geometry)], T0, holed)
    assert result.status == "unavailable"
    assert result.known_meters == 0.0
    assert result.risk_index is None
    assert "weather_incomplete" in result.reasons


def test_a_fractional_assessment_moment_uses_the_bin_covering_it():
    """The assessment moment need not lie on an exact hour: hour bins still
    COVER the whole window, numbers are given, and a bin still falling at the
    fractional moment enters only with its elapsed share."""
    asphalt = way(1, 0.0, surface="asphalt")
    # 3 mm in the bin (T0-1h, T0]; the rest of the contiguous series is dry.
    weather = _contiguous_series(
        T0, rain=lambda offset: 3.0 if offset == 0 else 0.0)
    when = T0 + timedelta(minutes=30)
    assert _history_complete(weather, when, 12.0) is True

    mid_fall = engine([asphalt]).analyze(
        [leg(asphalt.geometry, seconds=300.0)], T0 - timedelta(minutes=30), weather)
    half_past = engine([asphalt]).analyze(
        [leg(asphalt.geometry, seconds=300.0)], when, weather)
    # The T0-30 min walk crosses (midpoint T0-27:30) inside the still-falling
    # (T0-1h, T0] 3 mm bin; elapsed share there = 1950 s/3600 s and nothing
    # else is in the index.
    assert mid_fall.status == "available"
    assert mid_fall.risk_index == pytest.approx(1950.0 / 3600.0)
    # The T0+30 min walk: the same bin has ended and sits in the history at
    # age 0.5 h with a small decay, so the same leg is strictly wetter — the
    # fractional moment is processed as a fractional moment, not snapped.
    assert half_past.status == "available"
    assert half_past.risk_index > mid_fall.risk_index


def test_a_transition_across_the_hour_boundary_loses_nothing():
    """A bin falling at a moment becomes history the second it is a full hour
    old (the model's current/history cut at age 1.0 h): at T0+1h the 2 mm of
    the (T0-1h, T0] bin already sit in history at age exactly 1.0, one second
    later at age 1.00028 — the same drops, never lost and never counted
    twice, and the index sees them at every moment."""
    # Rain only in the bin ending at T0: 2 mm there, the rest of the series dry.
    weather = _contiguous_series(
        T0, rain=lambda offset: 2.0 if offset == 0 else 0.0)

    when_before = T0 - timedelta(seconds=1)
    history, current = _bin_inputs(weather, when_before)
    assert all(amount == 0.0 for amount, _ in history)  # nothing fell earlier
    assert current == pytest.approx(2.0 * (3599 / 3600))

    at_end = T0 + timedelta(hours=1)
    history, current = _bin_inputs(weather, at_end)
    assert current == 0.0  # the (T0, T0+1h] bin is dry
    fell = [(amount, age) for amount, age in history if amount > 0.0]
    assert len(fell) == 1 and fell[0][0] == pytest.approx(2.0)
    assert fell[0][1] == pytest.approx(1.0)  # the cut: exactly an hour old

    when_after = at_end + timedelta(seconds=1)
    history, current = _bin_inputs(weather, when_after)
    assert current == 0.0  # the next (all-future) bin has no fallen share
    aged = [(amount, age) for amount, age in history if amount > 0.0]
    assert len(aged) == 1 and aged[0][0] == pytest.approx(2.0)
    assert aged[0][1] > fell[0][1]  # the same drops, one second older


def test_a_gap_after_the_assessment_moment_does_not_hide_a_complete_lookback():
    """Final audit c7_2.4: a forecast gap AFTER the walk cannot invalidate the
    rain that already fell. Only bins starting before the assessment moment
    may enter the lookback; otherwise a missing future hour (for example a
    null Open-Meteo cell beyond the walk) would turn a complete past window
    into ``weather_incomplete`` and hide a legitimate estimate."""
    asphalt = way(1, 0.0, surface="asphalt")
    full = _contiguous_series(
        T0, rain=lambda offset: 1.0 if 0 <= offset < 2 else 0.0)
    # The bins covering the 5-minute walk (ending T0 and T0+1h) are present;
    # remove a bin two hours after the walk. This gap must be irrelevant.
    future_gap = WeatherSeries(
        hours=tuple(hour for hour in full.hours
                    if hour.ends_at != T0 + timedelta(hours=2)),
        retrieved_at=T0, valid_until=T0 + timedelta(minutes=15))
    assert _history_complete(future_gap, T0, 12.0) is True
    result = engine([asphalt]).analyze(
        [leg(asphalt.geometry, seconds=300.0)], T0, future_gap)
    assert result.status == "available"
    assert result.known_meters == pytest.approx(result.total_length_m)
    assert "weather_incomplete" not in result.reasons

    # The cut must not ignore the bin that covers a fractional moment: for a
    # 12:30 assessment the (12:00, 13:00] bin is required, and removing it
    # still leaves the window incomplete.
    when = T0 + timedelta(minutes=30)
    missing_current = WeatherSeries(
        hours=tuple(hour for hour in full.hours
                    if hour.ends_at != T0 + timedelta(hours=1)),
        retrieved_at=T0, valid_until=T0 + timedelta(minutes=15))
    assert _history_complete(full, when, 12.0) is True
    assert _history_complete(missing_current, when, 12.0) is False


def test_freezing_bin_overlapping_the_walk_end_is_refused():
    """Final audit c7_2.4: a bin that covers the walk's last minutes but ends
    after them must still stop the wet-surface model. Checking only bin ENDS
    inside the period could publish numbers while the walk was freezing."""
    asphalt = way(1, 0.0, surface="asphalt")
    full = _contiguous_series(T0)
    hours = tuple(
        WeatherHour(
            ends_at=hour.ends_at,
            precipitation_mm=hour.precipitation_mm,
            temperature_c=(-5.0 if hour.ends_at == T0 + timedelta(hours=1)
                           else 12.0),
            snowfall_cm=0.0)
        for hour in full.hours)
    weather = WeatherSeries(hours=hours, retrieved_at=T0,
                            valid_until=T0 + timedelta(minutes=15))
    result = engine([asphalt]).analyze(
        [leg(asphalt.geometry, seconds=1800.0)], T0, weather)
    assert result.status == "unavailable"
    assert result.risk_level == "unknown"
    assert result.known_meters == 0.0
    assert result.risk_index is None
    assert "weather_snow_or_freezing" in result.reasons


def test_a_freezing_bin_starting_after_the_walk_does_not_refuse():
    """Boundary control: a bin whose interval starts exactly at the walk's
    last moment covers only the time after the walk and is irrelevant."""
    bin_start = T0 + timedelta(minutes=30)
    bin_end = bin_start + timedelta(hours=1)
    weather = WeatherSeries(
        hours=(WeatherHour(ends_at=bin_end, precipitation_mm=0.0,
                           temperature_c=-5.0, snowfall_cm=0.0),),
        retrieved_at=T0, valid_until=T0 + timedelta(minutes=15))
    assert _freezing_or_snow(weather, T0, bin_start) is False
    assert _freezing_or_snow(weather, T0, bin_start + timedelta(seconds=1)) is True


def test_no_dataset_is_unavailable_and_never_invented():
    result = PuddleRiskEngine(ways_provider=EmptyPuddleWaysProvider(),
                              clock=lambda: T0).analyze(
        [leg(way(1, 0.0).geometry)], T0, series(T0))
    assert result.status == "unavailable"
    assert result.reasons == ("dataset_unavailable",)


@pytest.mark.parametrize("geometry, seconds, points", [
    (((59.94, 30.31),), 300.0, 1),                       # 1 point
    ((latlon(0, 0), latlon(0, 0)), 0.0, 2),              # non-positive duration
    ((latlon(0, 0), latlon(0, 0)), 300.0, 2),            # zero length
])
def test_invalid_inputs_are_refused(geometry, seconds, points):
    with pytest.raises(PuddleAnalysisInputError):
        PuddleRiskEngine(ways_provider=SyntheticWaysProvider(),
                         clock=lambda: T0).analyze(
            [leg(geometry, seconds)], T0, series(T0))


def test_naive_departure_is_refused():
    with pytest.raises(PuddleAnalysisInputError):
        PuddleRiskEngine(ways_provider=SyntheticWaysProvider()).analyze(
            [leg(way(1, 0.0).geometry)], datetime(2026, 6, 1, 14, 0), series(T0))


def test_too_many_points_are_refused():
    dense = tuple(latlon(index * 0.1, 0.0) for index in range(5001))
    with pytest.raises(PuddleAnalysisInputError):
        PuddleRiskEngine(ways_provider=SyntheticWaysProvider()).analyze(
            [leg(dense)], T0, series(T0))


def test_too_long_walk_is_refused_by_slice_cap():
    # Inside the 5000-point budget but far beyond the slice budget: three
    # 25 km legs with 2 points each.
    long_line = (latlon(-12_500.0, 0.0), latlon(12_500.0, 0.0))
    legs = [leg(long_line, seconds=600.0) for _ in range(3)]
    with pytest.raises(PuddleAnalysisInputError, match="too long"):
        PuddleRiskEngine(ways_provider=SyntheticWaysProvider()).analyze(
            legs, T0, series(T0))


def test_classified_segments_keep_real_geometry_and_states():
    asphalt = way(1, 0.0, surface="asphalt", points=5)
    weather = series(T0, rain=lambda age: 1.5 if 0 <= age < 2 else 0.0)
    result = engine([asphalt]).analyze([leg(asphalt.geometry)], T0, weather,
                                       include_segments=True)
    assert result.segments
    # Each reported length is rounded to 0.1 m, so the sum can drift by half
    # a rounding step per segment.
    rounding = 0.05 * len(result.segments) + 1e-6
    assert sum(segment.length_m for segment in result.segments) == pytest.approx(
        result.total_length_m, abs=rounding)
    for segment in result.segments:
        assert segment.state in {"low", "moderate", "high", "unknown"}
        assert len(segment.geometry) >= 2
        assert segment.ends_at_seconds > segment.starts_at_seconds
    first = result.segments[0]
    assert first.risk_index is not None and 0.0 <= first.risk_index <= 1.0


def test_sensitivity_reports_parameter_dependence():
    asphalt = way(1, 0.0, surface="asphalt")
    weather = series(T0, rain=lambda age: 1.2 if 0 <= age < 3 else 0.0)
    result = analyze([asphalt], weather)
    sensitivity = result.sensitivity
    assert sensitivity["baseline"] == result.risk_level
    assert {"drying_faster_0_5x", "drying_slower_2x",
            "ponding_sooner_saturation_0_75x"} <= set(sensitivity)
    # Slower drying cannot lower the level; faster drying cannot raise it.
    order = {"low": 0, "moderate": 1, "high": 2}
    assert order[sensitivity["drying_faster_0_5x"]] <= order[sensitivity["baseline"]]
    assert order[sensitivity["drying_slower_2x"]] >= order[sensitivity["baseline"]]
    assert "не калиброваны" in sensitivity["note"]


def test_sensitivity_is_absent_without_numbers():
    result = PuddleRiskEngine(ways_provider=EmptyPuddleWaysProvider(),
                              clock=lambda: T0).analyze(
        [leg(way(1, 0.0).geometry)], T0, series(T0))
    assert result.sensitivity == {}


def test_slice_times_follow_length_share_of_native_duration():
    straight = tuple(latlon(x, 0.0) for x in (0.0, 100.0))
    slices = _build_slices([leg(straight, seconds=100.0, visit=30.0),
                            leg(straight, seconds=50.0)])
    first_leg = [item for item in slices if item.leg_index == 0]
    second_leg = [item for item in slices if item.leg_index == 1]
    assert first_leg[0].t0 == 0.0
    assert first_leg[-1].t1 == pytest.approx(100.0)
    # The visit shifts the second leg, which then keeps its own duration.
    assert second_leg[0].t0 == pytest.approx(130.0)
    assert second_leg[-1].t1 == pytest.approx(180.0)


# ----------------------------------------------------------- real pilot data
def _real_provider() -> JsonPuddleWaysProvider:
    provider = JsonPuddleWaysProvider(REAL_DATASET)
    assert provider.available()
    return provider


def _real_walkable_ways(provider: JsonPuddleWaysProvider, limit: int = 40):
    pilot = provider.pilot_bbox()
    margin = 0.0005

    def well_inside(point: tuple[float, float]) -> bool:
        return (pilot[0] + margin <= point[0] <= pilot[2] - margin
                and pilot[1] + margin <= point[1] <= pilot[3] - margin)

    ways = [way_ for way_ in provider.ways_in_bbox(pilot)
            if way_.matchable() and way_.tags.get("surface")
            and len(way_.geometry) >= 3
            and all(well_inside(point) for point in way_.geometry)]
    return sorted(ways, key=lambda way_: way_.osm_id)[:limit]


def test_real_pilot_lines_are_processed_with_their_real_tags():
    """The engine reads the pinned OSM pilot: real tags, real geometry.

    Walking exactly along a real mapped way can still legitimately end as
    unknown (a parallel differently-tagged way within the corridor is a real
    situation) — every non-available outcome must carry the honesty reason,
    and the overwhelming majority of clean, well-inside ways must classify.
    """
    provider = _real_provider()
    weather = series(T0, rain=lambda age: 1.5 if 0 <= age < 2 else 0.0)
    run = PuddleRiskEngine(ways_provider=provider, clock=lambda: T0)

    reasons_seen: set[str] = set()
    deterministic_key = None
    classified = 0
    ways = _real_walkable_ways(provider)
    assert len(ways) >= 20
    for way_ in ways:
        result = run.analyze([leg(way_.geometry, seconds=240.0)], T0, weather)
        assert result.method == METHOD and result.method_version == METHOD_VERSION
        assert result.known_meters + result.unknown_meters == pytest.approx(
            result.total_length_m)
        if result.status == "available":
            classified += 1
            assert result.known_meters == pytest.approx(result.total_length_m)
            assert result.coverage_share == 1.0
            assert result.risk_level in {"low", "moderate", "high"}
            assert result.risk_index is not None
            if deterministic_key is None:
                deterministic_key = (result.risk_index, result.risk_level)
                again = run.analyze([leg(way_.geometry, seconds=240.0)], T0, weather)
                assert (again.risk_index, again.risk_level) == deterministic_key
        else:
            reasons_seen |= set(result.reasons)
            # A non-available outcome is either partially classified (a level
            # exists for the known part) or fully unknown with zero numbers.
            assert result.status in {"partial", "unavailable", "outside_coverage"}
            if result.status == "partial":
                assert result.risk_index is not None and result.known_meters > 0
            else:
                assert result.risk_level == "unknown"
                assert result.known_meters == 0.0
    assert classified >= int(0.8 * len(ways))
    assert reasons_seen <= {
        "ambiguous_osm_way", "unmatched_osm_way", "surface_unknown",
        "surface_not_supported", "outside_pilot", "weather_incomplete",
        "covered_not_waterproof", "model_precipitation_history",
        "experimental_uncalibrated"}


def test_real_pilot_unknown_share_never_becomes_low():
    provider = _real_provider()
    pilot = provider.pilot_bbox()
    # A synthetic line crossing the pilot box: it will meet SOME mapped ways
    # and miss others — the unknown share must stay unknown and the numbers
    # must add up exactly.
    lat0 = (pilot[0] + pilot[2]) / 2.0
    cross = tuple((lat0 + offset, pilot[1] + 0.0005 * index + 0.001)
                  for index, offset in enumerate((0.0, 0.0002, 0.0004)))
    weather = series(T0, rain=lambda age: 2.0 if 0 <= age < 2 else 0.0)
    result = PuddleRiskEngine(ways_provider=provider, clock=lambda: T0).analyze(
        [leg(cross, seconds=120.0)], T0, weather)
    assert result.known_meters + result.unknown_meters == pytest.approx(
        result.total_length_m)
    if result.status == "partial":
        assert result.risk_index is not None
        assert {"outside_pilot", "unmatched_osm_way", "ambiguous_osm_way",
                "surface_unknown", "surface_not_supported"} & set(result.reasons)
    if result.status in {"unavailable", "outside_coverage"}:
        assert result.risk_level == "unknown"
        assert result.known_meters == 0.0
