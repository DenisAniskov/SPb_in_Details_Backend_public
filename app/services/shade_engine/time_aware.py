"""C7.1 time-aware shade analysis of a walk (§10).

The walk is a list of legs with REAL native pedestrian geometry and walking
durations, separated by POI visits. The Sun moves while the tourist walks, so
one solar position for a four-hour walk is not an analysis — it is a lie.

MODEL OF TIME (one documented, deterministic approximation):

* Walking time inside a leg is distributed along the leg's geometry in
  proportion to geodesic length. Real walking speed varies; that is a stated
  limitation, not a hidden assumption. Vertex count is never used as a proxy.
* The walk is cut into spatial slices (~``slice_length_m``) and each slice is
  classified at the solar position of its own mid-time, bucketed into
  ``time_step_seconds`` steps (5–10 min, §10.2) so that the number of shadow
  computations stays bounded.
* The Sun is evaluated per slice mid-time; visit durations advance the clock,
  so a route returning through the same street after a museum visit is
  classified with the Sun of the later moment.

A slice is the REAL polyline between two cut points — every interior vertex of
the source geometry is preserved, so a walkway that turns inside a 40 m slice
is still classified on the path the tourist walks. A straight chord between the
cut points would silently analyse a different street, and the downstream map
overlay would draw that chord.

WHERE the shade is (not only how much): every slice reports the measured
intervals (``shaded_spans_m`` / ``unknown_spans_m``, metres along the slice)
where its shadow bands start and end. A length alone cannot say whether "20 m
of shade" is the first or the last 20 m of a slice, and any client-side guess
would be an invented boundary (§14.4).

THREE STATES (§9), never silently collapsed:

* ``estimated_shaded`` — the WHOLE slice is inside the modelled shadow of
  buildings with usable heights;
* ``unknown`` — the WHOLE slice lies in the shadow envelope of buildings
  WITHOUT a usable height (a documented 40 m plausibility envelope), or
  outside the mapped pilot coverage. Unknown ground is never reported as sun;
* ``estimated_exposed`` — the whole slice is inside coverage and outside both
  bands;
* ``mixed`` — the slice carries more than one of the above; the measured spans
  say exactly which metres are which.

The unknown band is measured as "envelope MINUS confirmed shadow": confirmed
shade wins, so overlapping metres are attributed once, and genuinely sunlit
ground is never demoted into ``unknown`` by an overlap (§9).

Invariant (tested): shaded + unknown + exposed ≈ total walking length, and the
spans of each slice are disjoint, inside ``[0, length]`` and sum exactly to the
reported ``shaded_m`` / ``unknown_m``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import math
from typing import Sequence

from shapely.geometry import GeometryCollection, LineString, MultiLineString, Point
from shapely.ops import unary_union

from app.services.shade_engine.buildings import (
    BuildingFootprint,
    HeightProvenance,
    USABLE_HEIGHT_PROVENANCE,
)
from app.services.shade_engine.geo import LocalFrame, haversine_distance_m
from app.services.shade_engine.shadows import (
    ShadowModelConfig,
    build_shadows,
    footprint_to_local,
)
from app.services.shade_engine.solar import SolarPosition, solar_position

METHOD = "shade_engine_c7_1_time_aware_2.5d_noaa"
METHOD_VERSION = "c7_1.2"

# --- documented, bounded parameters (§10.2, §20) ---------------------------
SLICE_LENGTH_M = 40.0            # spatial resolution along the walk
TIME_STEP_SECONDS = 600.0        # 10 min solar buckets (spec range: 5–10)
MAX_SLICES = 900                 # ≈ 36 km of walking at 40 m slices
MAX_TIME_BUCKETS = 60            # 10 h of walking
MAX_BUILDINGS_PER_ANALYSIS = 6000
MIN_SOLAR_ELEVATION_DEG = 2.0    # below this the 2.5D model is not applicable
#: Upper bound on the building-query pad (documented; keeps a low-sun analysis
#: bounded instead of degenerating into a city-wide query).
MAX_QUERY_PAD_M = 500.0
#: Buildings without a usable height are still assumed to cast SOMETHING; the
#: envelope bounds how far. 40 m is a deliberately generous envelope for the
#: historic centre (median mapped building is 15–25 m), which makes the
#: ``unknown`` band conservative — it can only under-report sun, never invent
#: shade. Documented in PHASE_C7_1_SHADOW_MODEL.md.
UNKNOWN_HEIGHT_ENVELOPE_M = 40.0
#: Tolerance of the whole-slice state labels. A slice is called
#: ``estimated_shaded`` when at least ``length - 0.25 m`` of it is measured
#: shadow; the spans still carry the exact geometry, so the label never hides
#: more than a quarter of a metre of possibly-lit ground.
STATE_EPSILON_M = 0.25


class ShadeAnalysisInputError(ValueError):
    """Raised for inputs the analysis cannot process honestly."""


@dataclass(frozen=True)
class WalkLegInput:
    geometry: tuple[tuple[float, float], ...]      # (lat, lon) WGS84
    walking_duration_seconds: float
    visit_duration_seconds_after_leg: float = 0.0


@dataclass(frozen=True)
class ClassifiedSlice:
    leg_index: int
    #: The REAL slice geometry (lat, lon), including every interior vertex of
    #: the source polyline — never a straight chord between two cut points.
    geometry: tuple[tuple[float, float], ...]
    length_m: float
    shaded_m: float
    unknown_m: float
    #: Measured positions of the shadow bands, metres along the slice from its
    #: start. Disjoint, inside [0, length_m], summing to the lengths above.
    shaded_spans_m: tuple[tuple[float, float], ...]
    unknown_spans_m: tuple[tuple[float, float], ...]
    state: str                 # estimated_shaded | estimated_exposed | unknown | mixed
    starts_at_seconds: float   # offset from the walk start
    ends_at_seconds: float

    def to_debug_map(self) -> dict:
        return {
            "leg": self.leg_index, "len_m": round(self.length_m, 1),
            "shaded_m": round(self.shaded_m, 1), "unknown_m": round(self.unknown_m, 1),
            "state": self.state, "t0": round(self.starts_at_seconds, 1),
            "shaded_spans": [[round(a, 2), round(b, 2)] for a, b in self.shaded_spans_m],
            "unknown_spans": [[round(a, 2), round(b, 2)] for a, b in self.unknown_spans_m],
        }


@dataclass(frozen=True)
class SolarSummary:
    min_elevation_deg: float
    max_elevation_deg: float
    min_azimuth_deg: float
    max_azimuth_deg: float
    bucket_count: int

    def to_map(self) -> dict:
        return {
            "min_elevation_deg": round(self.min_elevation_deg, 2),
            "max_elevation_deg": round(self.max_elevation_deg, 2),
            "min_azimuth_deg": round(self.min_azimuth_deg, 2),
            "max_azimuth_deg": round(self.max_azimuth_deg, 2),
            "bucket_count": self.bucket_count,
        }


@dataclass(frozen=True)
class TimeAwareShadeResult:
    status: str                      # available | partial | unavailable | outside_coverage | sun_below_horizon
    method: str
    method_version: str
    evaluated_period_start: str
    evaluated_period_end: str
    total_length_m: float
    #: The three lengths are None exactly when the analysis could not honestly
    #: produce them (unavailable / sun below horizon): a number is never
    #: invented to fill the field.
    shaded_length_m: float | None
    exposed_length_m: float | None
    unknown_length_m: float | None
    shade_fraction: float | None     # estimated share of the walk in shade
    confidence: str                  # high | medium | low | unavailable
    solar: SolarSummary | None
    coverage: dict
    height_provenance: dict
    slices: tuple[ClassifiedSlice, ...] = field(default=())
    limitations: tuple[str, ...] = field(default=())
    reason: str | None = None
    diagnostics: dict = field(default_factory=dict)


@dataclass(frozen=True)
class _SliceGeom:
    leg_index: int
    #: Real slice polyline (lat, lon) — interior vertices preserved.
    geometry: tuple[tuple[float, float], ...]
    #: The point at half the slice's along-path length (used for the zone test
    #: and for sound reach pruning).
    mid_lat: float
    mid_lon: float
    length_m: float
    t0: float
    t1: float
    bucket: int
    inside_coverage: bool

    @property
    def start(self) -> tuple[float, float]:
        return self.geometry[0]

    @property
    def end(self) -> tuple[float, float]:
        return self.geometry[-1]


def _bucket_of(seconds_from_start: float) -> int:
    return int(seconds_from_start // TIME_STEP_SECONDS)


def _build_slices(legs: Sequence[WalkLegInput], zones,
                  touched_zones: set[str] | None = None) -> list[_SliceGeom]:
    """Cut the legs into ~``SLICE_LENGTH_M`` pieces ALONG the real polyline.

    Every piece keeps the interior vertices of the source geometry, so a slice
    is a polyline and never a straight chord between two cut points: a walkway
    that turns inside a slice is classified on the path actually walked.
    """
    slices: list[_SliceGeom] = []
    clock = 0.0
    for leg_index, leg in enumerate(legs):
        points = leg.geometry
        lengths = [haversine_distance_m(a[0], a[1], b[0], b[1])
                   for a, b in zip(points, points[1:])]
        leg_length = sum(lengths)
        if leg_length <= 0:
            clock += leg.walking_duration_seconds + leg.visit_duration_seconds_after_leg
            continue
        piece_count = max(1, int(round(leg_length / SLICE_LENGTH_M)))
        target = leg_length / piece_count

        # Locate a point at a distance along the leg.
        def _point_at(distance: float) -> tuple[float, float]:
            acc = 0.0
            for (la, lo), (lb, lob), seg_len in zip(points, points[1:], lengths):
                if seg_len <= 0:
                    continue
                if acc + seg_len >= distance - 1e-9:
                    frac = (distance - acc) / seg_len
                    return (la + (lb - la) * frac, lo + (lob - lo) * frac)
                acc += seg_len
            return points[-1]

        def _piece_geometry(d0: float, d1: float) -> list[tuple[float, float]]:
            # Vertices strictly between the cut points are kept: the slice
            # follows the real walkway through every turn inside it.
            distances = [d0]
            acc = 0.0
            for seg_len in lengths:
                acc += seg_len
                if d0 + 1e-9 < acc < d1 - 1e-9:
                    distances.append(acc)
            distances.append(d1)
            return [_point_at(distance) for distance in distances]

        piece_start = 0.0
        for index in range(piece_count):
            piece_end = leg_length if index == piece_count - 1 else min(
                leg_length, piece_start + target)
            geometry = _piece_geometry(piece_start, piece_end)
            piece_len = piece_end - piece_start
            mid_lat, mid_lon = _point_at(piece_start + piece_len / 2.0)
            frac0 = piece_start / leg_length
            frac1 = piece_end / leg_length
            t0 = clock + frac0 * leg.walking_duration_seconds
            t1 = clock + frac1 * leg.walking_duration_seconds
            mid = (t0 + t1) / 2.0
            inside = not zones  # a provider without declared zones is assumed to cover the query
            for zone_id, min_lat, min_lon, max_lat, max_lon in zones:
                if min_lat <= mid_lat <= max_lat and min_lon <= mid_lon <= max_lon:
                    inside = True
                    touched_zones.add(zone_id)
                    break
            slices.append(_SliceGeom(leg_index, tuple(geometry), mid_lat, mid_lon,
                                     piece_len, t0, t1, _bucket_of(mid), inside))
            piece_start = piece_end
        clock += leg.walking_duration_seconds + leg.visit_duration_seconds_after_leg
    return slices


# ------------------------------------------------------------- span geometry
def _line_parts(geometry):
    """Iterate the 1-D parts of an intersection (stray points are skipped)."""
    if geometry is None or geometry.is_empty:
        return
    if isinstance(geometry, LineString):
        yield geometry
    elif isinstance(geometry, (MultiLineString, GeometryCollection)):
        for part in geometry.geoms:
            yield from _line_parts(part)


def _distinct_points(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Drop consecutive duplicates (a slice polyline needs >= 2 real points)."""
    out: list[tuple[float, float]] = []
    for point in points:
        if not out or point != out[-1]:
            out.append(point)
    return out


def _merge_spans(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Sort, drop empty and merge touching/overlapping intervals."""
    merged: list[tuple[float, float]] = []
    for start, end in sorted(spans):
        if end - start <= 0:
            continue
        if merged and start <= merged[-1][1] + 1e-6:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _spans_along(line: LineString, geometry) -> list[tuple[float, float]]:
    """Measured intervals (metres along ``line``) where it meets ``geometry``.

    The intersection pieces are projected back onto the line, so the result is
    a POSITION, not a total: "the first 20 m are shaded" and "the last 20 m are
    shaded" become distinguishable downstream instead of one forced guess.
    """
    pieces: list[tuple[float, float]] = []
    for part in _line_parts(geometry):
        coords = list(part.coords)
        if len(coords) < 2:
            continue
        start = line.project(Point(coords[0]))
        end = line.project(Point(coords[-1]))
        if end < start:
            start, end = end, start
        pieces.append((start, end))
    return _merge_spans(pieces)


def _subtract_spans(spans: list[tuple[float, float]],
                    holes: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Interval subtraction (``spans`` minus ``holes``), exact on 1-D sets."""
    remaining = list(spans)
    for hole_start, hole_end in holes:
        next_remaining: list[tuple[float, float]] = []
        for start, end in remaining:
            if hole_end <= start or hole_start >= end:
                next_remaining.append((start, end))
                continue
            if hole_start > start:
                next_remaining.append((start, hole_start))
            if hole_end < end:
                next_remaining.append((hole_end, end))
        remaining = next_remaining
    return _merge_spans(remaining)


def _scale_spans(spans: list[tuple[float, float]], scale: float,
                 limit: float) -> list[tuple[float, float]]:
    """Rescale planar intervals into the geodesic metric and clamp to the slice."""
    scaled: list[tuple[float, float]] = []
    for start, end in spans:
        low = min(limit, max(0.0, start * scale))
        high = min(limit, max(0.0, end * scale))
        if high - low > 0:
            scaled.append((low, high))
    return _merge_spans(scaled)


class _ShadowCache:
    """Per-analysis cache: one building's shadow per solar bucket."""

    def __init__(self, frame: LocalFrame, config: ShadowModelConfig) -> None:
        self._frame = frame
        self._config = config
        self._cache: dict[tuple[int, int], object] = {}
        self.computed = 0

    def shadow_for(self, index: int, footprint: BuildingFootprint, solar: SolarPosition,
                   bucket: int, *, height_override: float | None = None):
        key = (index, bucket, "envelope") if height_override is not None else (index, bucket)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        fp = footprint
        if height_override is not None:
            fp = BuildingFootprint(
                polygon_latlon=footprint.polygon_latlon,
                holes_latlon=footprint.holes_latlon,
                height_m=height_override,
                provenance=HeightProvenance.OSM_DECLARED,
                source=footprint.source,
                source_id=footprint.source_id)
        report = build_shadows([fp], solar, self._frame, self._config)
        geometry = report.shadows[0].geometry if report.shadows else None
        self._cache[key] = geometry
        self.computed += 1
        return geometry


def _local_bbox_distance_m(frame: LocalFrame, footprint: BuildingFootprint,
                           x: float, y: float) -> float:
    """Approximate planar distance from a point to the footprint's bbox."""
    xs = [frame.to_xy(lat, lon) for lat, lon in footprint.polygon_latlon]
    bx0 = min(p[0] for p in xs); bx1 = max(p[0] for p in xs)
    by0 = min(p[1] for p in xs); by1 = max(p[1] for p in xs)
    dx = max(bx0 - x, 0.0, x - bx1)
    dy = max(by0 - y, 0.0, y - by1)
    return math.hypot(dx, dy)


class TimeAwareShadeEngine:
    """Analyses a walk (legs + visits) against the pilot building dataset."""

    def __init__(self, building_provider, config: ShadowModelConfig = ShadowModelConfig()) -> None:
        self._provider = building_provider
        self._config = config

    def provider_available(self) -> bool:
        return self._provider.available()

    def provider_provenance(self) -> str:
        return self._provider.provenance_description()

    def dataset_version(self) -> str | None:
        try:
            return str(self._provider.dataset_meta().get("dataset_version"))
        except (AttributeError, RuntimeError):
            return None

    # ------------------------------------------------------------------ main
    def analyze(
        self,
        legs: Sequence[WalkLegInput],
        departure: datetime,
        *,
        include_slices: bool = False,
    ) -> TimeAwareShadeResult:
        if not legs:
            raise ShadeAnalysisInputError("at least one leg is required")
        if departure.tzinfo is None or departure.tzinfo.utcoffset(departure) is None:
            raise ShadeAnalysisInputError(
                "departure must be timezone-aware (naive datetimes are rejected)")
        for leg in legs:
            if len(leg.geometry) < 2:
                raise ShadeAnalysisInputError("every leg needs at least 2 route points")
            if not math.isfinite(leg.walking_duration_seconds) or leg.walking_duration_seconds <= 0:
                raise ShadeAnalysisInputError("walking_duration_seconds must be positive")
            if not math.isfinite(leg.visit_duration_seconds_after_leg) \
                    or leg.visit_duration_seconds_after_leg < 0:
                raise ShadeAnalysisInputError("visit_duration_seconds_after_leg must be >= 0")

        total_points = sum(len(leg.geometry) for leg in legs)
        if total_points > 5000:
            raise ShadeAnalysisInputError("too many route points for one analysis")

        if not self._provider.available():
            return self._unavailable(
                "Источник геометрии зданий недоступен: "
                + self._provider.provenance_description(),
                departure)

        try:
            zones = self._provider.zone_boxes()
        except AttributeError:
            # A provider that does not declare coverage zones (test fixtures,
            # synthetic providers) is treated as covering the queried area;
            # the pilot dataset always declares its zones.
            zones = ()
        touched_zones: set[str] = set()
        slices = _build_slices(legs, zones, touched_zones)
        if not slices:
            raise ShadeAnalysisInputError("the walk has no walkable length")
        if len(slices) > MAX_SLICES:
            raise ShadeAnalysisInputError(
                f"walk is too long for one analysis ({len(slices)} slices > {MAX_SLICES}); "
                "split it or raise the documented limit deliberately")
        buckets = sorted({s.bucket for s in slices})
        if len(buckets) > MAX_TIME_BUCKETS:
            raise ShadeAnalysisInputError(
                f"walk spans {len(buckets)} time buckets > {MAX_TIME_BUCKETS}")

        # One solar position per bucket (documented 10-minute discretisation).
        bucket_solar: dict[int, SolarPosition] = {}
        for bucket in buckets:
            mid = departure + timedelta(seconds=(bucket + 0.5) * TIME_STEP_SECONDS)
            bucket_solar[bucket] = solar_position(
                latitude=legs[0].geometry[0][0], longitude=legs[0].geometry[0][1], when=mid)

        if all(s.elevation_deg < MIN_SOLAR_ELEVATION_DEG for s in bucket_solar.values()):
            return self._sun_below(bucket_solar, departure, slices)

        total_slice_length = sum(s.length_m for s in slices)
        if zones and not any(s.inside_coverage for s in slices):
            # The whole walk is outside the mapped pilot zones: report exactly
            # that instead of a vaguer "no data" — and claim nothing about sun.
            return TimeAwareShadeResult(
                status="outside_coverage", method=METHOD, method_version=METHOD_VERSION,
                evaluated_period_start=departure.isoformat(),
                evaluated_period_end=(departure + timedelta(
                    seconds=max((s.t1 for s in slices), default=0.0))).isoformat(),
                total_length_m=total_slice_length, shaded_length_m=0.0,
                exposed_length_m=0.0, unknown_length_m=total_slice_length,
                shade_fraction=None, confidence="unavailable",
                solar=SolarSummary(
                    min_elevation_deg=min(s.elevation_deg for s in bucket_solar.values()),
                    max_elevation_deg=max(s.elevation_deg for s in bucket_solar.values()),
                    min_azimuth_deg=min(s.azimuth_deg for s in bucket_solar.values()),
                    max_azimuth_deg=max(s.azimuth_deg for s in bucket_solar.values()),
                    bucket_count=len(bucket_solar)),
                coverage={"dataset_zones_touched": [],
                          "outside_coverage_meters": round(total_slice_length, 1),
                          "coverage_share": 0.0},
                height_provenance={}, limitations=tuple(BASE_LIMITATIONS),
                reason=("Маршрут полностью вне зон покрытия пилотного датасета: "
                        "оценка тени для него недоступна, неизвестные участки не "
                        "считаются солнечными."))

        # Candidate buildings: one spatial query around the whole walk. The pad
        # is the reach of the TALLEST usable building at the lowest sun of this
        # walk (bounded by the documented shadow cap), so a tall building far
        # from the route is still fetched when its shadow can reach the walk.
        route_points = [point for leg in legs for point in leg.geometry]
        lats = [p[0] for p in route_points]; lons = [p[1] for p in route_points]
        try:
            dataset_max_height = self._provider.max_usable_height_m()
        except AttributeError:
            dataset_max_height = 0.0
        plausible_height = max(dataset_max_height, UNKNOWN_HEIGHT_ENVELOPE_M)
        lowest_elevation = max(
            MIN_SOLAR_ELEVATION_DEG,
            min(s.elevation_deg for s in bucket_solar.values()))
        pad_m = min(self._config.max_shadow_length_m, MAX_QUERY_PAD_M,
                    plausible_height / math.tan(math.radians(lowest_elevation)))
        pad_deg_lat = pad_m / 111_320.0
        pad_deg_lon = pad_m / (111_320.0 * math.cos(math.radians(sum(lats) / len(lats))))
        bbox = (min(lats) - pad_deg_lat, min(lons) - pad_deg_lon,
                max(lats) + pad_deg_lat, max(lons) + pad_deg_lon)
        footprints = self._provider.footprints_in_bbox(bbox)
        if len(footprints) > MAX_BUILDINGS_PER_ANALYSIS:
            raise ShadeAnalysisInputError(
                f"{len(footprints)} buildings in range exceed the per-analysis cap")
        if not footprints:
            return self._unavailable(
                "Для области маршрута датасет не вернул ни одного здания; оценка доли "
                "тени была бы фиктивной.", departure)

        frame = LocalFrame.covering(route_points)
        cache = _ShadowCache(frame, self._config)

        usable: list[tuple[int, BuildingFootprint]] = [
            (i, fp) for i, fp in enumerate(footprints)
            if fp.provenance in USABLE_HEIGHT_PROVENANCE and fp.height_m]
        unknown_height: list[tuple[int, BuildingFootprint]] = [
            (i, fp) for i, fp in enumerate(footprints)
            if fp.provenance not in USABLE_HEIGHT_PROVENANCE or not fp.height_m]
        # Conservatively, the shadow of the tallest usable building bounds which
        # buildings can matter for any slice in a bucket (plus its own extent).
        tallest_usable_m = max((fp.height_m or 0.0) for _i, fp in usable) if usable else 0.0

        shaded_total = unknown_total = exposed_total = total = 0.0
        classified: list[ClassifiedSlice] = []
        outside_coverage_length = 0.0

        for slice_ in slices:
            solar = bucket_solar[slice_.bucket]
            local = _distinct_points([frame.to_xy(lat, lon)
                                      for lat, lon in slice_.geometry])
            segment = LineString(local) if len(local) >= 2 else None
            planar_length = segment.length if segment is not None else 0.0
            mid_x, mid_y = frame.to_xy(slice_.mid_lat, slice_.mid_lon)
            reach = min(self._config.max_shadow_length_m,
                        tallest_usable_m / math.tan(math.radians(
                            max(solar.elevation_deg, MIN_SOLAR_ELEVATION_DEG))))
            # Sound reach pruning: every point of the slice lies within half the
            # slice length (straight line) of its midpoint, so a building more
            # than reach + L/2 away from the midpoint cannot shade any of it.
            prune_margin = reach + slice_.length_m / 2.0 + 1.0

            shaded_spans_planar: list[tuple[float, float]] = []
            if segment is not None and slice_.inside_coverage and usable:
                local_shadows = []
                for index, fp in usable:
                    if _local_bbox_distance_m(frame, fp, mid_x, mid_y) > prune_margin:
                        continue
                    geometry = cache.shadow_for(index, fp, solar, slice_.bucket)
                    if geometry is not None and geometry.intersects(segment):
                        local_shadows.append(geometry)
                if local_shadows:
                    union = unary_union(local_shadows) if len(local_shadows) > 1 else local_shadows[0]
                    shaded_spans_planar = _spans_along(
                        segment, segment.intersection(union))

            unknown_spans_planar: list[tuple[float, float]] = []
            if segment is not None and slice_.inside_coverage and unknown_height:
                envelope_reach = min(
                    self._config.max_shadow_length_m,
                    UNKNOWN_HEIGHT_ENVELOPE_M / math.tan(math.radians(max(solar.elevation_deg, MIN_SOLAR_ELEVATION_DEG))))
                envelope_prune = envelope_reach + slice_.length_m / 2.0 + 1.0
                envelope_parts = []
                for index, fp in unknown_height:
                    if _local_bbox_distance_m(frame, fp, mid_x, mid_y) > envelope_prune:
                        continue
                    geometry = cache.shadow_for(index, fp, solar, slice_.bucket,
                                                height_override=UNKNOWN_HEIGHT_ENVELOPE_M)
                    if geometry is not None and geometry.intersects(segment):
                        envelope_parts.append(geometry)
                if envelope_parts:
                    envelope = unary_union(envelope_parts) if len(envelope_parts) > 1 \
                        else envelope_parts[0]
                    # Confirmed shadow WINS: the envelope contributes only the
                    # ground whose state is genuinely unknown, so metres already
                    # attributed to a known-height shadow are not counted twice
                    # and sunlit ground is not demoted into ``unknown``.
                    unknown_spans_planar = _subtract_spans(
                        _spans_along(segment, segment.intersection(envelope)),
                        shaded_spans_planar)

            # The planar frame and the geodesic slice length differ by a nearly
            # constant local scale; renormalising keeps spans, lengths and the
            # invariant in ONE metric.
            scale = (slice_.length_m / planar_length) if planar_length > 0 else 0.0
            shaded_spans = _scale_spans(shaded_spans_planar, scale, slice_.length_m)
            unknown_spans = _scale_spans(unknown_spans_planar, scale, slice_.length_m)
            shaded_m = sum(end - start for start, end in shaded_spans)
            unknown_band_m = sum(end - start for start, end in unknown_spans)
            exposed_m = max(0.0, slice_.length_m - shaded_m - unknown_band_m)

            if not slice_.inside_coverage:
                # Outside the mapped pilot zones nothing is claimed: all unknown.
                state = "unknown"
                shaded_m = 0.0
                shaded_spans = []
                unknown_band_m = slice_.length_m
                unknown_spans = [(0.0, slice_.length_m)]
                exposed_m = 0.0
                outside_coverage_length += slice_.length_m
            elif shaded_m <= STATE_EPSILON_M and unknown_band_m <= STATE_EPSILON_M:
                state = "estimated_exposed"
            elif shaded_m >= slice_.length_m - STATE_EPSILON_M \
                    and unknown_band_m <= STATE_EPSILON_M:
                state = "estimated_shaded"
            elif unknown_band_m >= slice_.length_m - STATE_EPSILON_M \
                    and shaded_m <= STATE_EPSILON_M:
                state = "unknown"
            else:
                # More than one state inside one slice: the measured spans say
                # exactly which metres are shaded, unknown and lit.
                state = "mixed"

            total += slice_.length_m
            shaded_total += shaded_m
            unknown_total += unknown_band_m
            exposed_total += max(0.0, exposed_m)
            if include_slices:
                classified.append(ClassifiedSlice(
                    leg_index=slice_.leg_index,
                    geometry=slice_.geometry,
                    length_m=slice_.length_m, shaded_m=shaded_m,
                    unknown_m=unknown_band_m,
                    shaded_spans_m=tuple(shaded_spans),
                    unknown_spans_m=tuple(unknown_spans),
                    state=state,
                    starts_at_seconds=slice_.t0, ends_at_seconds=slice_.t1))

        shade_fraction = (shaded_total / total) if total > 0 else None
        period_end = max((s.t1 for s in slices), default=0.0)
        provenance = {
            "externally_verified": sum(
                1 for fp in footprints if fp.provenance == HeightProvenance.EXTERNALLY_VERIFIED),
            "osm_declared": sum(
                1 for fp in footprints if fp.provenance == HeightProvenance.OSM_DECLARED),
            "levels_estimate": sum(
                1 for fp in footprints if fp.provenance == HeightProvenance.LEVELS_ESTIMATE),
            "unavailable": len(unknown_height),
            "buildings_considered": len(footprints),
        }
        coverage_metrics = {
            "dataset_zones_touched": sorted(touched_zones),
            "outside_coverage_meters": round(outside_coverage_length, 1),
            "coverage_share": round(1.0 - outside_coverage_length / total, 4) if total else 0.0,
        }
        confidence = self._confidence(coverage_metrics, provenance, unknown_total, total)
        limitations = list(BASE_LIMITATIONS)
        if provenance["unavailable"]:
            limitations.append(
                f"Зданий без известной высоты в области расчёта: {provenance['unavailable']} — "
                f"их возможная тень отнесена к «неизвестно» (конверт {UNKNOWN_HEIGHT_ENVELOPE_M:.0f} м).")
        if unknown_total > 0:
            limitations.append(
                f"«Неизвестно» — {unknown_total:.0f} м из {total:.0f} м: вне покрытия датасета "
                "или в пределах возможной тени зданий без высоты; на солнце это не засчитывается.")
        if provenance["externally_verified"] == 0:
            limitations.append(
                "Ни одна высота не подтверждена внешним источником: уровень достоверности "
                "ограничен «medium» (§9).")

        status = "available"
        if outside_coverage_length and outside_coverage_length < total:
            status = "partial"
        elif outside_coverage_length >= total:
            status = "outside_coverage"
        elif unknown_total > 0:
            status = "partial"

        return TimeAwareShadeResult(
            status=status,
            method=METHOD,
            method_version=METHOD_VERSION,
            evaluated_period_start=departure.isoformat(),
            evaluated_period_end=(departure + timedelta(seconds=period_end)).isoformat(),
            total_length_m=total,
            shaded_length_m=shaded_total,
            exposed_length_m=exposed_total,
            unknown_length_m=unknown_total,
            shade_fraction=(round(shade_fraction, 4) if shade_fraction is not None else None),
            confidence=confidence,
            solar=SolarSummary(
                min_elevation_deg=min(s.elevation_deg for s in bucket_solar.values()),
                max_elevation_deg=max(s.elevation_deg for s in bucket_solar.values()),
                min_azimuth_deg=min(s.azimuth_deg for s in bucket_solar.values()),
                max_azimuth_deg=max(s.azimuth_deg for s in bucket_solar.values()),
                bucket_count=len(bucket_solar)),
            coverage=coverage_metrics,
            height_provenance=provenance,
            slices=tuple(classified),
            limitations=tuple(limitations),
            reason=None,
            diagnostics={
                "slices": len(slices),
                "shadow_computations": cache.computed,
                "buildings_usable": len(usable),
                "buildings_unknown_height": len(unknown_height),
            })

    # ------------------------------------------------------------ subresults
    def _confidence(self, coverage: dict, provenance: dict,
                    unknown_m: float, total_m: float) -> str:
        """Measurable criteria (§9). 'high' needs externally verified heights."""
        if total_m <= 0:
            return "unavailable"
        unknown_share = unknown_m / total_m
        if coverage.get("coverage_share", 0.0) < 1.0 or unknown_share >= 0.25:
            return "low"
        if provenance.get("externally_verified", 0) > 0 and unknown_share < 0.05:
            return "high"
        return "medium"

    def _unavailable(self, reason: str, departure: datetime) -> TimeAwareShadeResult:
        return TimeAwareShadeResult(
            status="unavailable", method=METHOD, method_version=METHOD_VERSION,
            evaluated_period_start=departure.isoformat(),
            evaluated_period_end=departure.isoformat(),
            total_length_m=0.0, shaded_length_m=None, exposed_length_m=None,
            unknown_length_m=None, shade_fraction=None, confidence="unavailable",
            solar=None, coverage={}, height_provenance={},
            limitations=tuple(BASE_LIMITATIONS), reason=reason)

    def _sun_below(self, bucket_solar: dict[int, SolarPosition], departure: datetime,
                   slices: list[_SliceGeom]) -> TimeAwareShadeResult:
        total = sum(s.length_m for s in slices)
        return TimeAwareShadeResult(
            status="sun_below_horizon", method=METHOD, method_version=METHOD_VERSION,
            evaluated_period_start=departure.isoformat(),
            evaluated_period_end=(departure + timedelta(
                seconds=max((s.t1 for s in slices), default=0.0))).isoformat(),
            total_length_m=total, shaded_length_m=None, exposed_length_m=None,
            unknown_length_m=None, shade_fraction=None, confidence="unavailable",
            solar=SolarSummary(
                min_elevation_deg=min(s.elevation_deg for s in bucket_solar.values()),
                max_elevation_deg=max(s.elevation_deg for s in bucket_solar.values()),
                min_azimuth_deg=min(s.azimuth_deg for s in bucket_solar.values()),
                max_azimuth_deg=max(s.azimuth_deg for s in bucket_solar.values()),
                bucket_count=len(bucket_solar)),
            coverage={}, height_provenance={}, limitations=tuple(BASE_LIMITATIONS),
            reason=("Солнце ниже горизонта в течение всей прогулки: прямые тени зданий "
                    "отсутствуют, доля тени не вычисляется (фиктивные проценты не выдаются)."))


BASE_LIMITATIONS: tuple[str, ...] = (
    "Результат — ОЦЕНКА по доступной геометрии зданий, а не измерение тени на месте.",
    "Модель 2.5D: здания — вертикальные призмы с плоской крышей на плоской земле; "
    "форма крыш, рельеф, деревья, навесы и прозрачные конструкции не учитываются.",
    "Время ходьбы распределяется по геометрии этапа пропорционально длине: реальная "
    "скорость пешехода может отличаться.",
    "Каждый участок прогулки (≈40 м РЕАЛЬНОЙ геометрии, включая её повороты) "
    "классифицируется в момент своей середины; положение Солнца берётся с шагом "
    "10 минут, а границы теневых полос — измеренные, а не усреднённые по участку.",
)
