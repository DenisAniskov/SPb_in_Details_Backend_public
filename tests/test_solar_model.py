"""Tests for the pure solar position model (Phase 2).

Test expectations are derived from FIRST PRINCIPLES (spherical trigonometry
at solstices/equinoxes) and textbook facts, not from the implementation:

    * solar declination at a solstice = ± Earth's axial tilt ≈ ±23.436°;
    * at an equinox the declination is 0°;
    * maximum noon elevation on a solstice/equinox: h = 90° − |φ − δ|;
    * on the equinox the Sun rises due east (az ≈ 90°) and sets due west
      (az ≈ 270°) everywhere on Earth (geometric, refraction ignored);
    * St. Petersburg white nights: at summer-solstice local solar midnight
      the Sun is only ≈ −6.6° below the horizon at φ = 59.94° (mirror of the
      noon formula: h = φ + δ − 90° ... = −(90° − φ − δ));
    * St. Petersburg winter: around the December solstice the noon Sun is
      barely ≈ 6.6° above the horizon (standard geography-textbook fact).

The NOAA/Meeus low-precision theory itself is documented to ≈0.01–0.03°, so
tolerances here are ≥ 0.1° unless comparing against first-principles values.
"""

import math
from datetime import datetime, timedelta, timezone

import pytest

from app.services.shade_engine.solar import SolarPosition, solar_position

UTC = timezone.utc
SPB_LAT = 59.9398
SPB_LON = 30.3146
OBLIQUITY_2024 = 23.436  # ±0.01 published axial tilt for 2024


# --------------------------------------------------------------------------
# First-principles anchors: declination
# --------------------------------------------------------------------------

def test_june_solstice_declination_2024():
    p = solar_position(SPB_LAT, SPB_LON, datetime(2024, 6, 20, 20, 51, tzinfo=UTC))
    assert abs(p.declination_deg - OBLIQUITY_2024) < 0.05


def test_december_solstice_declination_2024():
    p = solar_position(SPB_LAT, SPB_LON, datetime(2024, 12, 21, 9, 21, tzinfo=UTC))
    assert abs(p.declination_deg + OBLIQUITY_2024) < 0.05


def test_march_equinox_declination_2024():
    p = solar_position(SPB_LAT, SPB_LON, datetime(2024, 3, 20, 3, 6, tzinfo=UTC))
    assert abs(p.declination_deg) < 0.06


# --------------------------------------------------------------------------
# First-principles anchors: noon elevation & azimuth at SPb
# --------------------------------------------------------------------------

def _scan_max_elevation(day: datetime) -> SolarPosition:
    best = None
    t = day.replace(hour=8, minute=0, second=0, tzinfo=UTC)
    while t < day.replace(hour=12, minute=0, tzinfo=UTC):
        p = solar_position(SPB_LAT, SPB_LON, t)
        if best is None or p.elevation_deg > best.elevation_deg:
            best = p
        t += timedelta(minutes=1)
    return best


def test_spb_winter_solstice_noon():
    best = _scan_max_elevation(datetime(2024, 12, 21, tzinfo=UTC))
    expected = 90.0 - (SPB_LAT + OBLIQUITY_2024)  # φ + δ > 90 → sun barely up
    assert abs(best.elevation_deg - expected) < 0.1
    assert abs(best.azimuth_deg - 180.0) < 1.0


def test_spb_summer_solstice_noon():
    best = _scan_max_elevation(datetime(2024, 6, 20, tzinfo=UTC))
    expected = 90.0 - (SPB_LAT - OBLIQUITY_2024)
    assert abs(best.elevation_deg - expected) < 0.1
    assert abs(best.azimuth_deg - 180.0) < 1.0


def test_spb_equinox_noon():
    best = _scan_max_elevation(datetime(2024, 3, 20, tzinfo=UTC))
    expected = 90.0 - SPB_LAT  # δ = 0 at the equinox INSTANT (03:06 UTC)
    # SPb solar noon (~09:57 UTC) is ~7 h AFTER the equinox instant; the
    # declination has already moved by ~ +0.11° (rate ≈ 0.4°/day near the
    # equinox), so the tolerance must cover that physical drift, not just
    # the model's ~0.03° accuracy.
    assert abs(best.elevation_deg - expected) < 0.3
    assert abs(best.azimuth_deg - 180.0) < 1.0


# --------------------------------------------------------------------------
# First-principles anchors: horizon crossings (equinox: due E/W)
# --------------------------------------------------------------------------

def test_equinox_sunrise_azimuth_due_east():
    # March equinox at SPb: scan the morning for the geometric horizon crossing.
    t = datetime(2024, 3, 20, 3, 0, tzinfo=UTC)
    prev = solar_position(SPB_LAT, SPB_LON, t)
    while t < datetime(2024, 3, 20, 5, 0, tzinfo=UTC):
        t += timedelta(seconds=30)
        cur = solar_position(SPB_LAT, SPB_LON, t)
        if prev.elevation_deg < 0.0 <= cur.elevation_deg:
            assert abs(cur.azimuth_deg - 90.0) < 1.0
            return
        prev = cur
    pytest.fail("sunrise crossing not found on the equinox morning")


def test_equinox_sunset_azimuth_due_west():
    t = datetime(2024, 3, 20, 15, 0, tzinfo=UTC)
    prev = solar_position(SPB_LAT, SPB_LON, t)
    while t < datetime(2024, 3, 20, 18, 0, tzinfo=UTC):
        t += timedelta(seconds=30)
        cur = solar_position(SPB_LAT, SPB_LON, t)
        if prev.elevation_deg >= 0.0 > cur.elevation_deg:
            assert abs(cur.azimuth_deg - 270.0) < 1.0
            return
        prev = cur
    pytest.fail("sunset crossing not found on the equinox evening")


def test_white_nights_midnight_sun_only_slightly_below_horizon():
    # Famous SPb fact: around the June solstice the midnight Sun is ≈6.6° down.
    # Local solar midnight is the DEEPEST point of the night; scan for it
    # instead of assuming a clock time (the equation of time shifts it).
    best = None
    t = datetime(2024, 6, 20, 21, 0, tzinfo=UTC)
    for _ in range(90):
        p = solar_position(SPB_LAT, SPB_LON, t)
        if best is None or p.elevation_deg < best.elevation_deg:
            best = p
        t += timedelta(minutes=1)
    expected = -(90.0 - (SPB_LAT + OBLIQUITY_2024))
    assert abs(best.elevation_deg - expected) < 0.15
    assert best.above_horizon is False
    # At solar midnight the midnight sun stands due north of the observer.
    assert abs(best.azimuth_deg - 0.0) < 2.0 or abs(best.azimuth_deg - 360.0) < 2.0


def test_december_midnight_deep_below_horizon():
    p = solar_position(SPB_LAT, SPB_LON, datetime(2024, 12, 20, 21, 57, tzinfo=UTC))
    expected = -(90.0 - (SPB_LAT - OBLIQUITY_2024))  # ≈ −53.5°
    assert abs(p.elevation_deg - expected) < 0.2
    assert p.above_horizon is False


# --------------------------------------------------------------------------
# Invariants, determinism, robustness
# --------------------------------------------------------------------------

def test_deterministic_output():
    args = (SPB_LAT, SPB_LON, datetime(2026, 7, 1, 11, 0, tzinfo=UTC))
    a, b = solar_position(*args), solar_position(*args)
    assert a == b


def test_timezones_equivalent_instant_same_result():
    msk = timezone(timedelta(hours=3))
    utc_dt = datetime(2026, 7, 1, 8, 0, tzinfo=UTC)
    msk_dt = datetime(2026, 7, 1, 11, 0, tzinfo=msk)
    assert solar_position(SPB_LAT, SPB_LON, utc_dt) == solar_position(SPB_LAT, SPB_LON, msk_dt)


def test_zoneinfo_aware_datetime_accepted():
    from zoneinfo import ZoneInfo

    p = solar_position(SPB_LAT, SPB_LON, datetime(2026, 7, 1, 14, 0, tzinfo=ZoneInfo("Europe/Moscow")))
    ref = solar_position(SPB_LAT, SPB_LON, datetime(2026, 7, 1, 11, 0, tzinfo=UTC))
    assert abs(p.elevation_deg - ref.elevation_deg) < 1e-9
    assert abs(p.azimuth_deg - ref.azimuth_deg) < 1e-9


def test_naive_datetime_rejected():
    with pytest.raises(ValueError):
        solar_position(SPB_LAT, SPB_LON, datetime(2026, 7, 1, 14, 0))


def test_non_finite_coordinates_rejected():
    for lat, lon in [(float("nan"), 30.0), (59.9, float("inf")), (float("nan"), float("nan"))]:
        with pytest.raises(ValueError):
            solar_position(lat, lon, datetime(2026, 7, 1, 12, 0, tzinfo=UTC))


def test_latitude_out_of_range_rejected():
    with pytest.raises(ValueError):
        solar_position(90.5, 30.0, datetime(2026, 7, 1, 12, 0, tzinfo=UTC))


def test_pre_gregorian_date_rejected():
    with pytest.raises(ValueError):
        solar_position(SPB_LAT, SPB_LON, datetime(1500, 6, 1, 12, 0, tzinfo=UTC))


def test_zenith_relation_and_azimuth_range():
    p = solar_position(SPB_LAT, SPB_LON, datetime(2026, 10, 5, 9, 30, tzinfo=UTC))
    assert abs(p.zenith_deg - (90.0 - p.elevation_deg)) < 1e-9
    assert 0.0 <= p.azimuth_deg < 360.0


def test_randomized_sweep_invariants():
    # Deterministic pseudo-random sweep over a decade of dates and the globe.
    import random

    rng = random.Random(20261005)
    for _ in range(400):
        lat = rng.uniform(-85.0, 85.0)
        lon = rng.uniform(-180.0, 180.0)
        when = datetime(2015, 1, 1, tzinfo=UTC) + timedelta(
            days=rng.randrange(0, 3650), seconds=rng.randrange(0, 86400))
        p = solar_position(lat, lon, when)
        assert math.isfinite(p.elevation_deg) and math.isfinite(p.azimuth_deg)
        assert -90.5 <= p.elevation_deg <= 90.5
        assert 0.0 <= p.azimuth_deg < 360.0
        assert (p.elevation_deg > 0.0) == p.above_horizon


def test_continuity_across_sunrise_second_by_second():
    # Robustness at the horizon: no jumps, no NaN through the crossing.
    t = datetime(2024, 3, 20, 3, 50, tzinfo=UTC)
    prev = solar_position(SPB_LAT, SPB_LON, t)
    crossed = False
    for _ in range(600):
        t += timedelta(seconds=1)
        cur = solar_position(SPB_LAT, SPB_LON, t)
        assert math.isfinite(cur.elevation_deg) and math.isfinite(cur.azimuth_deg)
        assert abs(cur.elevation_deg - prev.elevation_deg) < 0.02
        assert abs(cur.azimuth_deg - prev.azimuth_deg) < 0.1
        if prev.elevation_deg < 0.0 <= cur.elevation_deg:
            crossed = True
        prev = cur
    assert crossed, "sweep must actually cross the horizon"


def test_refraction_raises_low_sun_and_is_guarded_below_horizon():
    geo = solar_position(SPB_LAT, SPB_LON, datetime(2024, 3, 20, 4, 6, tzinfo=UTC))
    if geo.elevation_deg <= -1.0 or geo.elevation_deg >= 1.0:
        pytest.skip("anchor instant not near horizon on this configuration")
    refr = solar_position(SPB_LAT, SPB_LON, datetime(2024, 3, 20, 4, 6, tzinfo=UTC), refraction=True)
    assert refr.elevation_deg > geo.elevation_deg
    assert refr.refraction_applied is True
    # Deeply below the horizon: correction defined as 0 (guarded), no NaN.
    deep = solar_position(SPB_LAT, SPB_LON, datetime(2024, 12, 20, 21, 57, tzinfo=UTC), refraction=True)
    assert math.isfinite(deep.elevation_deg)
    assert abs(deep.elevation_deg - (-53.5)) < 0.2
    # High noon: the Bennett correction is tiny (~0.013° at 53° elevation)
    # but still applied and strictly positive; document, don't deny it.
    noon_geo = solar_position(SPB_LAT, SPB_LON, datetime(2024, 6, 20, 9, 57, tzinfo=UTC))
    noon_refr = solar_position(SPB_LAT, SPB_LON, datetime(2024, 6, 20, 9, 57, tzinfo=UTC), refraction=True)
    assert noon_refr.refraction_applied is True
    assert 0.0 < noon_refr.elevation_deg - noon_geo.elevation_deg < 0.05


def test_azimuth_south_at_noon_for_northern_latitudes():
    for lat in (45.0, 55.75, 60.0, 68.97):
        best_t, best_e = None, -999.0
        t = datetime(2024, 10, 15, 8, 0, tzinfo=UTC)
        for _ in range(300):
            p = solar_position(lat, 30.0, t)
            if p.elevation_deg > best_e:
                best_e, best_t = p.elevation_deg, t
            t += timedelta(minutes=1)
        at_noon = solar_position(lat, 30.0, best_t)
        assert abs(at_noon.azimuth_deg - 180.0) < 1.0, lat


def test_southern_hemisphere_noon_azimuth_north():
    # φ = −45, mid-October: the noon sun stands due NORTH (az ≈ 0).
    # Scan the true solar-noon window for longitude 30°E (~09:57 UTC).
    best_t, best_e = None, -999.0
    t = datetime(2024, 10, 15, 8, 0, tzinfo=UTC)
    for _ in range(300):
        p = solar_position(-45.0, 30.0, t)
        if p.elevation_deg > best_e:
            best_e, best_t = p.elevation_deg, t
        t += timedelta(minutes=1)
    at_noon = solar_position(-45.0, 30.0, best_t)
    assert abs(at_noon.azimuth_deg - 0.0) < 1.0 or abs(at_noon.azimuth_deg - 360.0) < 1.0


def test_return_type():
    p = solar_position(SPB_LAT, SPB_LON, datetime(2026, 10, 5, 9, 0, tzinfo=UTC))
    assert isinstance(p, SolarPosition)
