"""C7.3 engine contract: exposure math, coverage honesty, and non-claims.

The properties asserted here are the ones the whole feature rests on:

* pollen bins are ``[t, t+1h)`` — the OPPOSITE of the C7.2 rain bins;
* a ``null`` hour is unknown and the mean is NOT diluted towards zero by it;
* a confirmed ``0.0`` is reported as a value (the real off-season case) and
  never as ``unknown``;
* the value of a leg depends on its HOUR only, never on where it is, so two
  neighbouring streets can never be given an artificial difference and vertex
  density cannot change the answer;
* a POI visit shifts later legs but is not itself accumulated;
* a single-cell fetch is never applied outside its own 0.1° cell;
* an ``unavailable`` answer publishes no numbers at all — per-species numbers
  included — while the withholding keeps each species' real coverage
  (``known_share``) and states which failure it was (C7.3.1);
* no species is summed, no threshold is applied and no medical category is
  emitted anywhere.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.services.pollen_engine.engine import (
    BASE_ASSUMPTIONS,
    BASE_LIMITATIONS,
    PollenAnalysisInputError,
    PollenEngine,
    PollenLegInput,
)
from app.services.pollen_engine.source import SPECIES_CODES
from tests.pollen_fixtures import (
    OTHER_CELL_LAT,
    OTHER_CELL_LON,
    constant,
    leg,
    series,
    straight,
)

BASE = dt.datetime(2026, 7, 17, 11, 0, tzinfo=dt.timezone.utc)
HOUR = dt.timedelta(hours=1)

#: Every reason code C7.3 may emit. Anything outside this set is a defect.
REASON_CATALOGUE = {
    "pollen_not_configured", "pollen_unavailable", "pollen_source_invalid",
    "pollen_stale", "pollen_outside_window", "pollen_no_data",
    "pollen_no_complete_panel", "outside_source_cell", "pollen_incomplete",
    "modeled_pollen_concentration", "experimental_uncalibrated",
}


def _engine(base: dt.datetime = BASE) -> PollenEngine:
    return PollenEngine(clock=lambda: base)


def _species(result, code):
    (item,) = [entry for entry in result.species if entry.code == code]
    return item


def _one(code: str, value: float, default: float = 0.0):
    return lambda species_code, starts_at: (
        float(value) if species_code == code else float(default))


def test_a_one_hour_walk_gets_its_own_bin_value():
    result = _engine().analyze([leg(straight(100.0), seconds=3600.0)], BASE,
                               series(BASE, concentration=_one("mugwort_pollen", 20.0)))
    assert result.status == "available"
    assert result.confidence == "low"
    assert result.coverage_share == pytest.approx(1.0)
    assert result.unknown_meters == pytest.approx(0.0)
    mugwort = _species(result, "mugwort_pollen")
    assert mugwort.status == "available"
    assert mugwort.mean_grains_m3 == pytest.approx(20.0)
    assert mugwort.peak_grains_m3 == pytest.approx(20.0)
    assert mugwort.exposure_grains_hours_m3 == pytest.approx(20.0)
    assert mugwort.unit == "grains/m³"
    assert mugwort.peak_at == BASE.isoformat()
    # A confirmed zero is a VALUE, not an unknown.
    alder = _species(result, "alder_pollen")
    assert alder.status == "available"
    assert alder.mean_grains_m3 == pytest.approx(0.0)
    assert alder.exposure_grains_hours_m3 == pytest.approx(0.0)
    assert alder.known_share == pytest.approx(1.0)


def test_instantaneous_bins_are_not_precipitation_bins():
    """The load-bearing semantic difference from C7.2.

    A value stamped ``t`` is valid for ``[t, t+1h)``. Under the C7.2 rain
    convention the same stamp would describe ``(t-1h, t]`` and a walk starting
    exactly at ``t`` would have received ZERO — the model would have missed the
    very hour the user is walking in.
    """
    marks = {BASE - HOUR: 4.0, BASE: 7.0, BASE + HOUR: 9.0}
    concentration = (lambda code, starts_at:
                     marks.get(starts_at, 0.0) if code == "ragweed_pollen" else 0.0)
    weather = series(BASE, concentration=concentration)
    engine = _engine()

    at_base = engine.analyze([leg(straight(100.0), seconds=3600.0)], BASE, weather)
    assert _species(at_base, "ragweed_pollen").mean_grains_m3 == pytest.approx(7.0)

    earlier = engine.analyze([leg(straight(100.0), seconds=3600.0)],
                             BASE - HOUR, weather)
    assert _species(earlier, "ragweed_pollen").mean_grains_m3 == pytest.approx(4.0)

    later = engine.analyze([leg(straight(100.0), seconds=3600.0)],
                           BASE + HOUR, weather)
    assert _species(later, "ragweed_pollen").mean_grains_m3 == pytest.approx(9.0)


def test_a_leg_crossing_an_hour_boundary_integrates_both_bins_exactly():
    concentration = (lambda code, starts_at:
                     4.0 if starts_at == BASE else 8.0
                     if starts_at == BASE + HOUR else 0.0)
    result = _engine().analyze(
        [leg(straight(100.0), seconds=3600.0)], BASE + dt.timedelta(minutes=30),
        series(BASE, concentration=concentration))
    # 30 min at 4.0 + 30 min at 8.0  →  time-weighted mean 6.0
    assert result.status == "available"
    mean = _species(result, "alder_pollen").mean_grains_m3
    assert mean == pytest.approx(6.0)
    assert _species(result, "alder_pollen").exposure_grains_hours_m3 \
        == pytest.approx(6.0)


def test_a_leg_that_starts_mid_hour_still_uses_the_covering_bin():
    result = _engine().analyze(
        [leg(straight(100.0), seconds=1800.0)], BASE + dt.timedelta(minutes=30),
        series(BASE, concentration=constant(5.0)))
    item = _species(result, "grass_pollen")
    assert item.status == "available"
    assert item.known_share == pytest.approx(1.0)
    assert item.mean_grains_m3 == pytest.approx(5.0)
    assert item.exposure_grains_hours_m3 == pytest.approx(2.5)


def test_an_unknown_hour_never_becomes_zero_and_never_dilutes_the_mean():
    weather = series(BASE, concentration=_one("birch_pollen", 10.0),
                     missing={BASE + HOUR})
    result = _engine().analyze([leg(straight(100.0), seconds=7200.0)], BASE,
                               weather)
    assert result.status == "partial"
    assert "pollen_incomplete" in result.reasons
    birch = _species(result, "birch_pollen")
    assert birch.status == "partial"
    assert birch.known_share == pytest.approx(0.5)
    # The mean is over the KNOWN hour only: 10.0, never 5.0.
    assert birch.mean_grains_m3 == pytest.approx(10.0)
    assert birch.exposure_grains_hours_m3 == pytest.approx(10.0)
    # The other species are known only in the same surviving hour, so the
    # route-level gap is identical for the whole panel.
    assert result.coverage_share == pytest.approx(0.5)
    assert result.known_meters == pytest.approx(result.total_length_m * 0.5)


def test_coverage_meters_are_proportional_to_the_unknown_time_share():
    weather = series(BASE, missing={BASE + HOUR})
    result = _engine().analyze(
        [leg(straight(1000.0), seconds=3600.0),
         leg(straight(1000.0, x_from=-2000.0), seconds=3600.0)], BASE, weather)
    assert result.status == "partial"
    assert result.known_meters == pytest.approx(result.total_length_m * 0.5)
    assert result.unknown_meters == pytest.approx(result.total_length_m * 0.5)
    assert result.coverage_share == pytest.approx(0.5)


def test_the_value_of_a_leg_depends_on_the_hour_only_not_on_where_it_is():
    """ТЗ C7.3 §9: one spatial pixel must not create street-to-street noise."""
    west = leg(straight(200.0, x_from=-900.0), seconds=600.0)
    east = leg(straight(200.0, x_from=900.0, y_m=300.0), seconds=600.0)
    result = _engine().analyze([west, east], BASE,
                               series(BASE, concentration=constant(3.0)))
    assert len(result.legs) == 0  # legs are opt-in
    detailed = _engine().analyze([west, east], BASE,
                                 series(BASE, concentration=constant(3.0)),
                                 include_legs=True)
    first, second = detailed.legs
    assert first.inside_source_cell and second.inside_source_cell
    # Different places, different windows, same hour → identical values.
    assert [item.mean_grains_m3 for item in first.species] == \
        [item.mean_grains_m3 for item in second.species]
    assert first.species[0].mean_grains_m3 == pytest.approx(3.0)


def test_vertex_density_of_the_same_polyline_changes_nothing():
    coarse = leg(straight(500.0, points=2), seconds=900.0)
    dense = leg(straight(500.0, points=2000), seconds=900.0)
    engine = _engine()
    weather = series(BASE, concentration=constant(6.5))
    first = engine.analyze([coarse], BASE, weather)
    second = engine.analyze([dense], BASE, weather)
    assert first.total_length_m == pytest.approx(second.total_length_m, abs=0.1)
    assert first.coverage_share == pytest.approx(second.coverage_share)
    assert [item.mean_grains_m3 for item in first.species] == \
        [item.mean_grains_m3 for item in second.species]
    assert [item.exposure_grains_hours_m3 for item in first.species] == \
        [item.exposure_grains_hours_m3 for item in second.species]


def test_a_poi_visit_shifts_later_legs_and_is_not_accumulated():
    concentration = (lambda code, starts_at:
                     4.0 if starts_at == BASE else 9.0
                     if starts_at == BASE + HOUR else 0.0)
    result = _engine().analyze(
        [leg(straight(200.0), seconds=1800.0, visit=3600.0),
         leg(straight(200.0, x_from=400.0), seconds=1800.0)],
        BASE, series(BASE, concentration=concentration), include_legs=True)
    first, second = result.legs
    assert first.starts_at_seconds == pytest.approx(0.0)
    assert first.ends_at_seconds == pytest.approx(1800.0)
    # The visit shifts the second leg by exactly one hour.
    assert second.starts_at_seconds == pytest.approx(5400.0)
    assert second.ends_at_seconds == pytest.approx(7200.0)
    assert first.species[0].mean_grains_m3 == pytest.approx(4.0)
    assert second.species[0].mean_grains_m3 == pytest.approx(9.0)
    # Walking time only: 2 × 1800 s. The dwell hour is not accumulated.
    assert result.walking_seconds == pytest.approx(3600.0)
    assert result.elapsed_seconds == pytest.approx(7200.0)
    assert _species(result, "alder_pollen").total_seconds == pytest.approx(3600.0)
    # 0.5 h at 4.0 + 0.5 h at 9.0 — NOT (0.5+1+0.5) h of accumulated air.
    assert _species(result, "alder_pollen").exposure_grains_hours_m3 \
        == pytest.approx(6.5)


def test_a_leg_outside_the_source_cell_is_unknown_not_borrowed():
    inside = leg(straight(600.0), seconds=1800.0)
    outside = leg(((OTHER_CELL_LAT, OTHER_CELL_LON),
                   (OTHER_CELL_LAT + 0.001, OTHER_CELL_LON + 0.001)), seconds=1800.0)
    result = _engine().analyze([inside, outside], BASE,
                               series(BASE, concentration=constant(50.0)),
                               include_legs=True)
    assert result.status == "partial"
    assert "outside_source_cell" in result.reasons
    assert "pollen_incomplete" in result.reasons
    first, second = result.legs
    assert first.inside_source_cell is True
    assert second.inside_source_cell is False
    assert second.known_meters == pytest.approx(0.0)
    assert second.species == ()
    # Time share and metre share differ here ON PURPOSE: both legs last 1800 s
    # (so half the walking time is covered), but the far leg is much shorter,
    # so more than half the metres are. Neither number is padded to match the
    # other; coverage_share is the metre share, as documented.
    assert result.coverage_share == pytest.approx(
        result.known_meters / result.total_length_m)
    assert result.known_meters == pytest.approx(first.length_m, abs=0.1)
    assert result.coverage_share > 0.5
    # The known leg keeps its number; the far leg gets NO number at all.
    assert _species(result, "alder_pollen").mean_grains_m3 == pytest.approx(50.0)
    assert _species(result, "alder_pollen").known_share == pytest.approx(0.5)


def test_a_walk_entirely_outside_the_cell_is_outside_coverage():
    result = _engine().analyze(
        [leg(((OTHER_CELL_LAT, OTHER_CELL_LON),
              (OTHER_CELL_LAT + 0.001, OTHER_CELL_LON + 0.001)), seconds=600.0)],
        BASE, series(BASE, concentration=constant(50.0)))
    assert result.status == "outside_coverage"
    assert result.confidence == "unavailable"
    assert result.reasons == ("outside_source_cell",)
    assert result.known_meters == pytest.approx(0.0)
    assert all(item.status == "unknown" and item.mean_grains_m3 is None
               for item in result.species)


def test_a_walk_past_the_horizon_is_partial_with_an_unknown_tail():
    # Three known bins: [BASE, BASE+3h). The fourth walking hour is past the
    # model's horizon and its values are null.
    weather = series(BASE, hours_before=0, hours_after=3,
                     concentration=constant(8.0))
    legs = [leg(straight(300.0, x_from=-300.0 * index), seconds=3600.0)
            for index in range(4)]
    result = _engine().analyze(legs, BASE, weather, include_legs=True)
    assert result.status == "partial"
    assert result.coverage_share == pytest.approx(0.75)
    assert result.legs[2].inside_source_cell is True
    assert result.legs[3].known_meters == pytest.approx(0.0)
    assert _species(result, "grass_pollen").known_share == pytest.approx(0.75)
    # The mean stays over the known hours: extrapolating the fourth hour would
    # invent data the source did not publish.
    assert _species(result, "grass_pollen").mean_grains_m3 == pytest.approx(8.0)


def test_a_walk_entirely_past_the_horizon_has_no_numbers():
    weather = series(BASE, hours_before=0, hours_after=3)
    result = _engine().analyze([leg(straight(100.0), seconds=600.0)],
                               BASE + dt.timedelta(hours=6), weather)
    assert result.status == "unavailable"
    assert "pollen_outside_window" in result.reasons
    assert result.known_meters == pytest.approx(0.0)
    assert all(item.mean_grains_m3 is None for item in result.species)


def test_an_all_null_panel_yields_no_numbers():
    weather = series(BASE, missing={BASE + dt.timedelta(hours=offset)
                                    for offset in range(-2, 19)})
    result = _engine().analyze([leg(straight(100.0), seconds=600.0)], BASE,
                               weather)
    assert result.status == "unavailable"
    assert "pollen_no_data" in result.reasons
    assert result.confidence == "unavailable"
    assert all(item.status == "unknown" and item.mean_grains_m3 is None
               for item in result.species)
    assert result.known_meters == pytest.approx(0.0)


def test_a_full_panel_of_zeros_is_available_and_not_unknown():
    """The real October state: the source reports 0.0, not null.

    Treating that as "no data" would hide a genuine value; treating it as a
    statement of safety would be a medical claim. It is reported as a number.
    """
    result = _engine().analyze([leg(straight(100.0), seconds=3600.0)], BASE,
                               series(BASE, concentration=constant(0.0)))
    assert result.status == "available"
    assert result.confidence == "low"
    assert result.coverage_share == pytest.approx(1.0)
    for item in result.species:
        assert item.status == "available"
        assert item.mean_grains_m3 == pytest.approx(0.0)
        assert item.exposure_grains_hours_m3 == pytest.approx(0.0)
    assert "experimental_uncalibrated" in result.reasons
    assert "modeled_pollen_concentration" in result.reasons


def test_a_stale_series_is_refused():
    weather = series(BASE, retrieved_at=BASE - dt.timedelta(hours=3))
    result = _engine().analyze([leg(straight(100.0), seconds=600.0)], BASE,
                               weather)
    assert result.status == "unavailable"
    assert "pollen_stale" in result.reasons
    assert result.known_meters == pytest.approx(0.0)


def test_a_missing_source_is_unavailable_with_its_reason():
    result = _engine().analyze([leg(straight(100.0), seconds=600.0)], BASE,
                               None, "pollen_not_configured")
    assert result.status == "unavailable"
    assert result.reasons == ("pollen_not_configured",)
    assert result.confidence == "unavailable"
    assert result.total_length_m > 0
    assert result.unknown_meters == pytest.approx(result.total_length_m)
    assert all(item.status == "unknown" for item in result.species)


def test_species_are_reported_separately_and_never_summed():
    values = {"alder_pollen": 1.0, "birch_pollen": 2.0, "grass_pollen": 3.0,
              "mugwort_pollen": 4.0, "ragweed_pollen": 5.0, "olive_pollen": 6.0}
    result = _engine().analyze(
        [leg(straight(100.0), seconds=3600.0)], BASE,
        series(BASE, concentration=lambda code, starts_at: values[code]))
    for code, value in values.items():
        assert _species(result, code).mean_grains_m3 == pytest.approx(value)
    # 21.0 would be the forbidden "allergy risk" sum: it must appear nowhere.
    numeric = [item.mean_grains_m3 for item in result.species]
    assert numeric == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    assert sum(numeric) == 21.0
    assert not hasattr(result, "risk_level")
    assert not hasattr(result, "risk_index")
    for item in result.species:
        assert item.status in {"available", "partial", "unknown"}


def test_reason_codes_stay_inside_the_documented_catalogue():
    positive = _engine().analyze([leg(straight(100.0), seconds=600.0)], BASE,
                                 series(BASE))
    negative = _engine().analyze([leg(straight(100.0), seconds=600.0)], BASE,
                                 None, "pollen_not_configured")
    for result in (positive, negative):
        assert set(result.reasons) <= REASON_CATALOGUE
        assert not ({"low", "moderate", "high"} & set(result.reasons))


def test_numbers_are_always_published_with_their_assumptions():
    result = _engine().analyze([leg(straight(100.0), seconds=3600.0)], BASE,
                               series(BASE), include_legs=True)
    assert result.assumptions == BASE_ASSUMPTIONS
    assert any("доля его длины" in text for text in result.assumptions)
    assert any("ячейк" in text and "CAMS" in text for text in result.assumptions)
    assert any("времени ходьбы" in text for text in result.assumptions)
    assert result.limitations == BASE_LIMITATIONS
    # Legs are opt-in and never appear unrequested.
    silent = _engine().analyze([leg(straight(100.0), seconds=3600.0)], BASE,
                               series(BASE))
    assert silent.legs == ()


@pytest.mark.parametrize("build, expected", [
    (lambda: ([leg(straight(100.0), seconds=600.0)],
              dt.datetime(2026, 7, 17, 11, 0)), "naive departure"),
    (lambda: ([leg(((59.94, 30.31),), seconds=600.0)], BASE), "one point"),
    (lambda: ([leg(straight(100.0), seconds=0.0)], BASE), "zero duration"),
    (lambda: ([leg(straight(100.0), seconds=-5.0)], BASE), "negative duration"),
    (lambda: ([leg(straight(100.0), seconds=600.0, visit=-1.0)], BASE),
     "negative visit"),
    (lambda: ([leg(straight(100.0, points=2000), seconds=600.0)] * 3, BASE),
     "too many points"),
    (lambda: ([PollenLegInput(geometry=((59.94, 30.31), (59.94, 30.31)),
                              walking_duration_seconds=600.0)], BASE),
     "zero-length walk"),
    (lambda: ([leg(straight(100.0), seconds=3600.0, visit=3600.0)] * 13, BASE),
     "elapsed over 24 h"),
])
def test_degenerate_inputs_are_refused(build, expected):
    legs, departure = build()
    with pytest.raises(PollenAnalysisInputError):
        _engine().analyze(legs, departure, series(BASE))


def test_no_legs_at_all_is_refused():
    with pytest.raises(PollenAnalysisInputError):
        _engine().analyze([], BASE, series(BASE))


def test_the_diagnostics_count_species_states_and_outside_legs():
    inside = leg(straight(600.0), seconds=1800.0)
    outside = leg(((OTHER_CELL_LAT, OTHER_CELL_LON),
                   (OTHER_CELL_LAT + 0.001, OTHER_CELL_LON + 0.001)), seconds=1800.0)
    # 3 × 1800 s = 1.5 h: the first bin (missing) covers the first two legs and
    # the second bin (known) covers the third, so every species is partial.
    partial = _engine().analyze(
        [inside, outside, leg(straight(200.0, x_from=-400.0), seconds=1800.0)],
        BASE, series(BASE, missing={BASE}), include_legs=True)
    assert partial.status == "partial"
    assert partial.diagnostics["legs"] == 3
    assert partial.diagnostics["legs_outside_source_cell"] == 1
    assert partial.diagnostics["species_partial"] == len(SPECIES_CODES)
    assert partial.diagnostics["species_available"] == 0
    assert partial.diagnostics["species_unknown"] == 0


def test_a_visit_after_the_last_leg_extends_nothing():
    """Nothing follows the final leg, so its visit is not part of the walk.

    The request schema excludes it from the elapsed-time limit for the same
    reason; the assessed period must agree with that definition instead of
    reporting a span that contains a dwell nobody walks in.
    """
    without = _engine().analyze([leg(straight(200.0), seconds=1800.0)], BASE,
                                series(BASE), include_legs=True)
    with_trailing = _engine().analyze(
        [leg(straight(200.0), seconds=1800.0, visit=3600.0)], BASE,
        series(BASE), include_legs=True)
    assert with_trailing.elapsed_seconds == pytest.approx(without.elapsed_seconds)
    assert with_trailing.elapsed_seconds == pytest.approx(1800.0)
    assert with_trailing.walking_seconds == pytest.approx(1800.0)
    assert with_trailing.evaluated_period_end == without.evaluated_period_end
    assert with_trailing.coverage_share == pytest.approx(1.0)


def test_the_mean_and_the_integral_stay_consistent_with_their_units():
    """exposure[grains·h/m³] == mean[grains/m³] × known_hours."""
    weather = series(BASE, missing={BASE + HOUR},
                     concentration=constant(12.5))
    result = _engine().analyze([leg(straight(100.0), seconds=7200.0)], BASE,
                               weather)
    item = _species(result, "alder_pollen")
    known_hours = item.known_seconds / 3600.0
    assert known_hours == pytest.approx(1.0)
    assert item.exposure_grains_hours_m3 \
        == pytest.approx(item.mean_grains_m3 * known_hours)
    # Halving the known time halves the integral but not the mean.
    shorter = _engine().analyze([leg(straight(100.0), seconds=3600.0)], BASE,
                                weather)
    assert _species(shorter, "alder_pollen").exposure_grains_hours_m3 \
        == pytest.approx(12.5)


# --------------------------------------------------------------------------
# C7.3.1 — an unavailable answer withholds EVERY number, per-species included.
#
# The route-level verdict follows the complete six-species panel: an hour where
# any species is missing is not presented as a complete pollen picture. When no
# hour of the walk has the whole panel, the response is ``unavailable`` and the
# contract forbids numbers on it — so the per-species numbers are withheld,
# while each species keeps its real coverage and the reason says whether the
# source had nothing or merely never had all six at once.
# --------------------------------------------------------------------------


def _alternating(first: str, second: str, *, other_value: float = 5.0):
    """``first`` known in hour 1 only, ``second`` in hour 2 only.

    Returns a ``concentration(code, starts_at)`` callable in which ``None`` is
    a per-species gap for that single hour; every other species is known in
    both hours.
    """
    def concentration(code: str, starts_at: dt.datetime):
        in_second_hour = starts_at == BASE + HOUR
        if code == first:
            return None if in_second_hour else 10.0
        if code == second:
            return None if not in_second_hour else 20.0
        return other_value
    return concentration


def test_no_hour_with_the_whole_panel_publishes_no_numbers_at_all():
    """ТЗ C7.3.1: two hours, alder known in one, birch in the other.

    Four species are known in BOTH hours and two are known in one hour each —
    yet not one hour carries the complete panel, so the answer is
    ``unavailable`` and not a single number may be published: not the two
    partial species and not the four fully covered ones either.
    """
    result = _engine().analyze(
        [leg(straight(1000.0), seconds=7200.0)], BASE,
        series(BASE, concentration=_alternating("alder_pollen", "birch_pollen")),
        include_legs=True)
    assert result.status == "unavailable"
    assert result.confidence == "unavailable"
    # The walk itself is untouched by the withholding.
    assert result.total_length_m > 0
    assert result.known_meters == pytest.approx(0.0)
    assert result.coverage_share == pytest.approx(0.0)
    assert result.unknown_meters == pytest.approx(result.total_length_m)
    assert result.walking_seconds == pytest.approx(7200.0)
    # The reason separates "never all six at once" from "no data at all".
    assert "pollen_no_complete_panel" in result.reasons
    assert "pollen_incomplete" in result.reasons
    assert "pollen_no_data" not in result.reasons
    assert "modeled_pollen_concentration" not in result.reasons
    # NOT ONE number survives, including the four species the source covered
    # for the entire walk.
    for item in result.species:
        assert item.status == "unknown"
        assert item.mean_grains_m3 is None
        assert item.peak_grains_m3 is None
        assert item.peak_at is None
        assert item.exposure_grains_hours_m3 is None
    # ... and the withholding is not a claim that the source had nothing: the
    # per-species coverage stays exactly as the source produced it.
    assert _species(result, "alder_pollen").known_share == pytest.approx(0.5)
    assert _species(result, "birch_pollen").known_share == pytest.approx(0.5)
    assert _species(result, "alder_pollen").known_seconds == pytest.approx(3600.0)
    for code in SPECIES_CODES:
        if code not in ("alder_pollen", "birch_pollen"):
            assert _species(result, code).known_share == pytest.approx(1.0)
    # Per-leg numbers are withheld with the route-level ones.
    assert result.legs == ()
    # The engine's own account of what it found is kept in diagnostics.
    assert result.diagnostics["species_withheld"] == len(SPECIES_CODES)
    assert result.diagnostics["species_partial"] == 2
    assert result.diagnostics["species_available"] == 4
    assert result.diagnostics["species_unknown"] == 0


def test_the_withheld_engine_result_never_exposes_a_species_number():
    """The invariant the API and the Flutter model both rely on (C7.3.1)."""
    for weather in (
            series(BASE, concentration=_alternating("alder_pollen",
                                                    "birch_pollen")),
            # One species missing in EVERY hour, the rest complete: no hour
            # has the whole panel either.
            series(BASE, concentration=lambda code, starts_at: (
                None if code == "olive_pollen" else 7.0)),
    ):
        result = _engine().analyze(
            [leg(straight(500.0), seconds=7200.0)], BASE, weather,
            include_legs=True)
        assert result.status == "unavailable"
        assert result.known_meters == pytest.approx(0.0)
        assert result.legs == ()
        assert all(item.status == "unknown"
                   and item.mean_grains_m3 is None
                   and item.peak_grains_m3 is None
                   and item.peak_at is None
                   and item.exposure_grains_hours_m3 is None
                   for item in result.species)


def test_a_partially_complete_panel_still_publishes_the_known_hour():
    """The withholding must not over-reach: half the walk IS fully covered.

    ``grass_pollen`` is missing in the first hour only, so the second hour
    carries the whole panel. That hour's numbers are published — including
    grass's own — and the first hour is reported as unknown coverage instead
    of being filled with anything.
    """
    result = _engine().analyze(
        [leg(straight(500.0), seconds=7200.0)], BASE,
        series(BASE, concentration=lambda code, starts_at: (
            None if code == "grass_pollen" and starts_at == BASE else 7.0)),
        include_legs=True)
    assert result.status == "partial"
    assert result.confidence == "low"
    assert result.coverage_share == pytest.approx(0.5)
    assert result.known_meters == pytest.approx(result.total_length_m * 0.5)
    assert "pollen_incomplete" in result.reasons
    assert "pollen_no_complete_panel" not in result.reasons
    assert result.diagnostics["species_withheld"] == 0
    grass = _species(result, "grass_pollen")
    assert grass.status == "partial"
    assert grass.known_share == pytest.approx(0.5)
    # The mean is over the species' own known hour, never diluted by the gap.
    assert grass.mean_grains_m3 == pytest.approx(7.0)
    assert grass.exposure_grains_hours_m3 == pytest.approx(7.0)
    for code in SPECIES_CODES:
        if code != "grass_pollen":
            assert _species(result, code).status == "available"
            assert _species(result, code).mean_grains_m3 == pytest.approx(7.0)
    assert len(result.legs) == 1


def test_a_hour_with_the_whole_panel_keeps_every_number():
    """The regression guard: the withholding must not spread to good answers."""
    result = _engine().analyze(
        [leg(straight(500.0), seconds=3600.0)], BASE,
        series(BASE, concentration=constant(3.0)))
    assert result.status == "available"
    assert result.coverage_share == pytest.approx(1.0)
    assert result.diagnostics["species_withheld"] == 0
    assert "pollen_no_complete_panel" not in result.reasons
    assert "pollen_no_data" not in result.reasons
    for item in result.species:
        assert item.status == "available"
        assert item.mean_grains_m3 == pytest.approx(3.0)


def test_a_single_fully_missing_species_is_not_called_no_data():
    """Five species known for the whole walk, one missing everywhere.

    The panel is still incomplete, so the numbers are withheld — but calling
    this "the source has no values" would be false, and the reason code says
    the true thing instead.
    """
    result = _engine().analyze(
        [leg(straight(500.0), seconds=3600.0)], BASE,
        series(BASE, concentration=lambda code, starts_at: (
            None if code == "olive_pollen" else 4.0)))
    assert result.status == "unavailable"
    assert "pollen_no_complete_panel" in result.reasons
    assert "pollen_no_data" not in result.reasons
    # No species is PARTIALLY covered here, so the "incomplete" code — which
    # means "values are missing for part of the walk" — is not added.
    assert "pollen_incomplete" not in result.reasons
    assert _species(result, "olive_pollen").known_share == pytest.approx(0.0)
    assert _species(result, "alder_pollen").known_share == pytest.approx(1.0)
    assert all(item.mean_grains_m3 is None for item in result.species)


def test_an_all_null_panel_is_no_data_and_not_a_withheld_panel():
    """The other direction: coverage zero everywhere is genuinely "no data"."""
    weather = series(BASE, missing={BASE + dt.timedelta(hours=offset)
                                    for offset in range(-2, 19)})
    result = _engine().analyze([leg(straight(100.0), seconds=7200.0)], BASE,
                               weather, include_legs=True)
    assert result.status == "unavailable"
    assert "pollen_no_data" in result.reasons
    assert "pollen_no_complete_panel" not in result.reasons
    assert all(item.known_share == pytest.approx(0.0) for item in result.species)
    assert result.diagnostics["species_withheld"] == 0


def test_a_withheld_panel_is_the_only_unavailable_answer_with_coverage():
    """``unavailable`` without a read source must leave every share at zero.

    The client tells the two states apart by exactly this: a non-zero
    ``known_share`` under ``unavailable`` can only mean "the source did have
    values; the numbers were withheld".
    """
    outside_cell = leg(((OTHER_CELL_LAT, OTHER_CELL_LON),
                        (OTHER_CELL_LAT + 0.001, OTHER_CELL_LON + 0.001)),
                       seconds=600.0)
    cases = (
        # No source at all (the production default).
        _engine().analyze([leg(straight(100.0), seconds=600.0)], BASE, None,
                          "pollen_not_configured"),
        # A stale series.
        _engine().analyze(
            [leg(straight(100.0), seconds=600.0)], BASE,
            series(BASE, retrieved_at=BASE - dt.timedelta(hours=3))),
        # A walk entirely past the forecast horizon.
        _engine().analyze(
            [leg(straight(100.0), seconds=600.0)],
            BASE + dt.timedelta(hours=6),
            series(BASE, hours_before=0, hours_after=3)),
        # A walk entirely in another CAMS cell.
        _engine().analyze([outside_cell], BASE,
                          series(BASE, concentration=constant(50.0))),
    )
    for result in cases:
        assert result.status in {"unavailable", "outside_coverage"}
        assert all(item.known_share == pytest.approx(0.0)
                   for item in result.species)
        assert "pollen_no_complete_panel" not in result.reasons
        assert result.diagnostics["species_withheld"] == 0
