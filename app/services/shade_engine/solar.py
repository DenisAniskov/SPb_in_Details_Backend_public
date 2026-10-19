"""Pure solar position model — Phase 2 of the Shade Engine foundation.

ALGORITHM LINEAGE (documented, no invented values):

    The implementation follows the low-precision solar theory published by
    NOAA (Global Monitoring Laboratory, "NOAA Solar Calculator", spreadsheet
    and accompanying JavaScript), which is itself a codification of
    Jean Meeus, "Astronomical Algorithms", 2nd edition, 1998, chapters 25
    (solar coordinates) and 28 (equation of time). The refraction correction
    is Bennett's formula (G. Bennett, 1982, "The calculation of astronomical
    refraction in marine navigation") as used by NOAA.

    Documented accuracy of this algorithm class: ≈ 0.01°–0.03° of solar
    position for the Gregorian calendar era, which is orders of magnitude
    below every other error source in shade estimation (building heights,
    footprint accuracy, roof shape). It is fully deterministic: identical
    inputs always produce bit-identical outputs; no external data, network
    or randomness is involved.

CONVENTIONS:

    * Azimuth: degrees clockwise from true North. 0 = N, 90 = E, 180 = S,
      270 = W. (This is the convention used by the shadow model, where the
      shadow is cast opposite the solar azimuth.)
    * Elevation (altitude): degrees above the geometric horizon; negative
      below the horizon. Geometric (center of the solar disk, no atmosphere)
      unless ``refraction=True`` is requested.
    * Input time: a timezone-aware ``datetime`` in ANY time zone; it is
      converted to UTC internally. Naive datetimes are REJECTED — silently
      assuming a zone would be an invented value.
    * Input position: WGS84 latitude/longitude in degrees.

ROBUSTNESS:

    * Below the horizon the same equations continue smoothly (negative
      elevation); no special-casing, no discontinuities.
    * Near sunrise/sunset the formulas are continuous; only the OPTIONAL
      refraction correction has a documented validity range (see below).
    * At local solar noon the azimuth is exactly due south (northern
      hemisphere, latitude > declination); the atan2 formulation is stable
      there.
"""

import math
from dataclasses import dataclass
from datetime import date, datetime, timezone

_GREGORIAN_START = date(1582, 10, 15)


@dataclass(frozen=True)
class SolarPosition:
    """Solar position for one instant and location.

    elevation_deg  — geometric elevation above horizon (negative below it),
                     optionally refraction-corrected when requested.
    azimuth_deg    — clockwise from true north, in [0, 360).
    zenith_deg     — 90 - elevation.
    declination_deg— solar declination (diagnostic, useful in tests).
    refraction_applied — True when the Bennett/NOAA correction was applied.
    """

    elevation_deg: float
    azimuth_deg: float
    zenith_deg: float
    declination_deg: float
    refraction_applied: bool = False

    @property
    def above_horizon(self) -> bool:
        return self.elevation_deg > 0.0


def _julian_day(dt_utc: datetime) -> float:
    """Julian Day (Meeus, ch. 7) for a Gregorian-calendar UTC datetime.

    Valid from 1582-10-15 (Gregorian reform) onward; earlier dates are
    rejected because Julian/Gregorian calendar switching would otherwise be
    an implicit assumption rather than a documented one.
    """
    y, m = dt_utc.year, dt_utc.month
    if date(y, m, 1) < _GREGORIAN_START:
        raise ValueError("Solar model is defined for the Gregorian calendar only (dates from 1582-10-15).")
    if m <= 2:
        y -= 1
        m += 12
    a = math.floor(y / 100.0)
    b = 2 - a + math.floor(a / 4.0)
    day_fraction = (dt_utc.hour + dt_utc.minute / 60.0
                    + dt_utc.second / 3600.0 + dt_utc.microsecond / 3_600_000_000.0) / 24.0
    return (math.floor(365.25 * (y + 4716.0))
            + math.floor(30.6001 * (m + 1.0))
            + dt_utc.day + b - 1524.5
            + day_fraction)


def _refraction_deg(elevation_deg: float, pressure_hpa: float, temperature_c: float) -> float:
    """Atmospheric refraction correction (Bennett 1982, as used by NOAA).

    Defined for geometric elevations above -1.0°; outside that range it is
    conventionally returned as 0 and the caller gets the geometric position.
    (Refraction only matters when the Sun is at/above the horizon; below the
    horizon there is nothing to refract for shadow purposes.) The correction
    depends on pressure and temperature; DEFAULTS document standard
    atmosphere conditions. Weather/cloud cover NEVER enters here — clouds
    change irradiance, not geometry.
    """
    if not (-1.0 < elevation_deg < 90.0):
        return 0.0
    arg = math.radians(elevation_deg + 10.3 / (elevation_deg + 5.11))
    tan_arg = math.tan(arg)
    if tan_arg == 0.0 or not math.isfinite(tan_arg):
        return 0.0
    arcmin = (pressure_hpa / 1010.0) * (283.0 / (273.0 + temperature_c)) * (1.02 / (60.0 * tan_arg))
    return arcmin / 60.0


def solar_position(
    latitude: float,
    longitude: float,
    when: datetime,
    refraction: bool = False,
    pressure_hpa: float = 1013.25,
    temperature_c: float = 15.0,
) -> SolarPosition:
    """Solar elevation and azimuth for a location and timezone-aware instant.

    Deterministic and side-effect free. Raises ``ValueError`` on naive
    datetimes, non-finite or out-of-range inputs, and pre-Gregorian dates.
    """
    if not isinstance(when, datetime):
        raise ValueError("when must be a datetime")
    if when.tzinfo is None or when.tzinfo.utcoffset(when) is None:
        raise ValueError("when must be timezone-aware (naive datetimes are rejected)")
    if not math.isfinite(latitude) or not math.isfinite(longitude):
        raise ValueError("latitude/longitude must be finite")
    if not -90.0 <= latitude <= 90.0:
        raise ValueError("latitude must be within [-90, 90]")

    dt_utc = when.astimezone(timezone.utc)
    jd = _julian_day(dt_utc)
    t = (jd - 2_451_545.0) / 36_525.0  # Julian centuries since J2000.0

    # --- Meeus/NOAA low-precision solar theory (degrees unless noted) ---
    l0 = (280.46646 + t * (36_000.76983 + t * 0.0003032)) % 360.0          # mean longitude
    m = 357.52911 + t * (35_999.05029 - 0.0001537 * t)                     # mean anomaly
    e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)                 # orbital eccentricity
    center = (math.sin(math.radians(m)) * (1.914602 - t * (0.004817 + 0.000014 * t))
              + math.sin(math.radians(2 * m)) * (0.019993 - 0.000101 * t)
              + math.sin(math.radians(3 * m)) * 0.000289)                  # equation of center
    true_longitude = l0 + center
    omega = 125.04 - 1934.136 * t                                          # Moon's ascending node
    apparent_longitude = true_longitude - 0.00569 - 0.00478 * math.sin(math.radians(omega))

    eps0 = 23.0 + (26.0 + (21.448 - t * (46.8150 + t * (0.00059 - t * 0.001813))) / 60.0) / 60.0
    eps = eps0 + 0.00256 * math.cos(math.radians(omega))                   # obliquity

    # Declination and right ascension from the apparent longitude.
    sin_dec = math.sin(math.radians(eps)) * math.sin(math.radians(apparent_longitude))
    declination = math.degrees(math.asin(max(-1.0, min(1.0, sin_dec))))

    # Equation of time in minutes (NOAA formulation; 1 degree = 4 minutes).
    y = math.tan(math.radians(eps / 2.0)) ** 2
    eot_deg = (y * math.sin(math.radians(2 * l0))
               - 2.0 * e * math.sin(math.radians(m))
               + 4.0 * e * y * math.sin(math.radians(m)) * math.cos(math.radians(2 * l0))
               - 0.5 * y * y * math.sin(math.radians(4 * l0))
               - 1.25 * e * e * math.sin(math.radians(2 * m)))
    eot_minutes = 4.0 * eot_deg

    # True solar time & hour angle (UTC minutes since midnight; NOAA uses
    # local standard time + 4*longitude - 60*TZoffset; UTC is TZoffset = 0).
    minutes_since_midnight = (dt_utc.hour * 60.0 + dt_utc.minute
                              + dt_utc.second / 60.0 + dt_utc.microsecond / 60_000_000.0)
    true_solar_time = minutes_since_midnight + eot_minutes + 4.0 * longitude
    hour_angle = true_solar_time / 4.0 - 180.0  # degrees; negative before solar noon
    hour_angle = (hour_angle + 180.0) % 360.0 - 180.0

    lat_rad = math.radians(latitude)
    dec_rad = math.radians(declination)
    ha_rad = math.radians(hour_angle)

    sin_elevation = (math.sin(lat_rad) * math.sin(dec_rad)
                     + math.cos(lat_rad) * math.cos(dec_rad) * math.cos(ha_rad))
    sin_elevation = max(-1.0, min(1.0, sin_elevation))
    elevation = math.degrees(math.asin(sin_elevation))

    # Azimuth from north, clockwise: atan2(east, north) in the ENU frame.
    east = -math.cos(dec_rad) * math.sin(ha_rad)
    north = math.cos(lat_rad) * math.sin(dec_rad) - math.sin(lat_rad) * math.cos(dec_rad) * math.cos(ha_rad)
    azimuth = math.degrees(math.atan2(east, north))
    azimuth = azimuth % 360.0

    refraction_applied = False
    if refraction:
        correction = _refraction_deg(elevation, pressure_hpa, temperature_c)
        if correction != 0.0:
            elevation = elevation + correction
            refraction_applied = True
            if elevation > 90.0:
                elevation = 90.0

    return SolarPosition(
        elevation_deg=elevation,
        azimuth_deg=azimuth,
        zenith_deg=90.0 - elevation,
        declination_deg=declination,
        refraction_applied=refraction_applied,
    )
