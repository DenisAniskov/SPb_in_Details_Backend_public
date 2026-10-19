"""Shade Engine orchestrator — ties the pure layers into one analysis.

The engine analyzes an ALREADY-EXISTING route (e.g. a real Yandex MapKit
pedestrian polyline) at a given instant. It does NOT generate, choose or
optimize routes, and every result it returns is explicitly an estimate with
provenance and limitations attached.
"""

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Sequence

from app.services.shade_engine.buildings import (
    BuildingFootprint,
    BuildingGeometryProvider,
    EmptyBuildingProvider,
    HeightProvenance,
)
from app.services.shade_engine.geo import LocalFrame
from app.services.shade_engine.intersection import InvalidRouteError, RouteSubSegment, analyze_route
from app.services.shade_engine.solar import SolarPosition, solar_position
from app.services.shade_engine.shadows import ShadowModelConfig, build_shadows
from app.services.shade_engine.weather import ShadeRelevance, WeatherContext, shade_estimate_relevance

ENGINE_METHOD = "shade_engine_v1_2.5d_parallel_rays_noaa_solar"

Bbox = tuple[float, float, float, float]


def route_bbox(route_latlon: Sequence[tuple[float, float]], pad_m: float) -> Bbox:
    """Route bounding box padded by ``pad_m`` meters on every side.

    The pad is approximate (degrees converted with fixed scale factors for
    SPb latitudes) — it only selects the candidate-building query window,
    so precision here is irrelevant to correctness: the intersection layer
    makes the final decision geometrically.
    """
    lats = [p[0] for p in route_latlon]
    lons = [p[1] for p in route_latlon]
    lat_center = (min(lats) + max(lats)) / 2.0
    d_lat = pad_m / 111_320.0
    d_lon = pad_m / (111_320.0 * max(0.2, math.cos(math.radians(lat_center))))
    return (min(lats) - d_lat, min(lons) - d_lon, max(lats) + d_lat, max(lons) + d_lon)


@dataclass(frozen=True)
class ShadeAnalysisResult:
    """Full result of one shade analysis (engine-level, not yet API-shaped)."""

    status: str                                  # ok | sun_below_horizon | unavailable
    method: str
    evaluated_at: str                            # ISO-8601 of the input instant
    solar: SolarPosition
    provider_name: str
    provider_provenance: str
    route_points: int
    # Metrics — None exactly when the analysis could not honestly produce them.
    total_length_m: float | None = None
    shaded_length_m: float | None = None
    exposed_length_m: float | None = None
    shade_fraction: float | None = None
    buildings_used: int = 0
    buildings_excluded_unknown_height: int = 0
    buildings_excluded_low_sun: int = 0
    buildings_skipped_invalid_geometry: int = 0
    shadows_capped: int = 0
    provenance_externally_verified: int = 0
    provenance_osm_declared: int = 0
    provenance_levels_estimate: int = 0
    relevance: ShadeRelevance | None = None
    relevance_explanation: str | None = None
    subsegments: tuple[RouteSubSegment, ...] = field(default=())
    limitations: tuple[str, ...] = field(default=())
    reason: str | None = None                    # why not "ok", when so

    @property
    def provenance_known(self) -> int:
        """Legacy v1 alias: the v1 ``KNOWN`` level is now [OSM_DECLARED]."""
        return self.provenance_osm_declared


class ShadeEngine:
    """Analyzes an existing route polyline for sun exposure at one instant."""

    def __init__(
        self,
        building_provider: BuildingGeometryProvider | None = None,
        config: ShadowModelConfig = ShadowModelConfig(),
    ) -> None:
        self._provider = building_provider or EmptyBuildingProvider()
        self._config = config

    @property
    def config(self) -> ShadowModelConfig:
        return self._config

    def provider_available(self) -> bool:
        """Whether the configured building provider has usable data."""
        return self._provider.available()

    def analyze_route(
        self,
        route_latlon: Sequence[tuple[float, float]],
        when: datetime,
        include_subsegments: bool = False,
        weather: WeatherContext | None = None,
    ) -> ShadeAnalysisResult:
        solar = solar_position(
            latitude=self._route_centroid(route_latlon)[0],
            longitude=self._route_centroid(route_latlon)[1],
            when=when,
        )
        relevance, relevance_note = shade_estimate_relevance(weather, solar.above_horizon)

        if not solar.above_horizon:
            return self._result(
                status="sun_below_horizon", solar=solar, when=when, route_latlon=route_latlon,
                relevance=relevance, relevance_note=relevance_note,
                reason="Солнце ниже горизонта в указанный момент: прямые тени от зданий отсутствуют.",
            )

        if not self._provider.available():
            return self._result(
                status="unavailable", solar=solar, when=when, route_latlon=route_latlon,
                relevance=relevance, relevance_note=relevance_note,
                reason=("Расчёт тени недоступен: " + self._provider.provenance_description()
                        + " Требуется проверенный источник геометрии зданий (планируется OpenStreetMap)."),
            )

        bbox = route_bbox(route_latlon, pad_m=self._config.max_shadow_length_m)
        footprints: Sequence[BuildingFootprint] = self._provider.footprints_in_bbox(bbox)
        if not footprints:
            return self._result(
                status="unavailable", solar=solar, when=when, route_latlon=route_latlon,
                relevance=relevance, relevance_note=relevance_note,
                reason=("Для области маршрута источник геометрии не вернул ни одного здания; "
                        "оценка доли тени была бы фиктивной."),
            )

        frame = LocalFrame.covering([(p[0], p[1]) for p in route_latlon])
        report = build_shadows(footprints, solar, frame, self._config)
        result = analyze_route(route_latlon, report.shadows, frame, include_subsegments)
        provenance_verified = sum(
            1 for s in report.shadows if s.provenance == HeightProvenance.EXTERNALLY_VERIFIED)
        provenance_osm = sum(
            1 for s in report.shadows if s.provenance == HeightProvenance.OSM_DECLARED)
        provenance_levels = sum(1 for s in report.shadows if s.provenance == HeightProvenance.LEVELS_ESTIMATE)

        limitations = list(BASE_LIMITATIONS)
        if report.excluded_unknown_height:
            limitations.append(
                f"Зданий без известной высоты исключено из расчёта: {report.excluded_unknown_height} "
                "(высота не выдумывается — их тень не учтена, тень маршрута занижена).")
        if report.excluded_low_sun:
            limitations.append(
                f"Зданий исключено из-за слишком низкого солнца (<{self._config.min_solar_elevation_deg}°): "
                f"{report.excluded_low_sun}.")
        if report.capped_count:
            limitations.append(
                f"Теней ограничено максимальной длиной {self._config.max_shadow_length_m:.0f} м: "
                f"{report.capped_count} (занижает оценку тени при низком солнце).")
        if report.skipped_invalid_geometry:
            limitations.append(
                f"Зданий пропущено из-за некорректной геометрии: {report.skipped_invalid_geometry}.")
        limitations.append(
            f"Источник геометрии зданий: {self._provider.provenance_description()}")
        limitations.append(
            "Здания вне области маршрута не учитывались; отсутствие здания в данных — не отсутствие тени.")

        return self._result(
            status="ok", solar=solar, when=when, route_latlon=route_latlon,
            relevance=relevance, relevance_note=relevance_note,
            total_length_m=result.total_length_m,
            shaded_length_m=result.shaded_length_m,
            exposed_length_m=result.exposed_length_m,
            shade_fraction=result.shade_fraction,
            buildings_used=len(report.shadows),
            buildings_excluded_unknown_height=report.excluded_unknown_height,
            buildings_excluded_low_sun=report.excluded_low_sun,
            buildings_skipped_invalid_geometry=report.skipped_invalid_geometry,
            shadows_capped=report.capped_count,
            provenance_externally_verified=provenance_verified,
            provenance_osm_declared=provenance_osm,
            provenance_levels_estimate=provenance_levels,
            subsegments=result.subsegments if include_subsegments else (),
            limitations=tuple(limitations),
        )

    @staticmethod
    def _route_centroid(route_latlon: Sequence[tuple[float, float]]) -> tuple[float, float]:
        pts = list(route_latlon)
        if not pts:
            raise InvalidRouteError("route is empty")
        lat = sum(p[0] for p in pts) / len(pts)
        lon = sum(p[1] for p in pts) / len(pts)
        if not (math.isfinite(lat) and math.isfinite(lon)):
            raise InvalidRouteError("route contains non-finite coordinates")
        return lat, lon

    def _result(self, *, status: str, solar: SolarPosition, when: datetime,
                route_latlon: Sequence[tuple[float, float]], relevance, relevance_note,
                reason: str | None = None, **metrics) -> ShadeAnalysisResult:
        metrics.setdefault("limitations", tuple(BASE_LIMITATIONS) if status != "ok" else ())
        return ShadeAnalysisResult(
            status=status,
            method=ENGINE_METHOD,
            evaluated_at=when.isoformat(),
            solar=solar,
            provider_name=self._provider.name,
            provider_provenance=self._provider.provenance_description(),
            route_points=len(list(route_latlon)),
            relevance=relevance,
            relevance_explanation=relevance_note,
            reason=reason,
            **metrics,
        )


BASE_LIMITATIONS: tuple[str, ...] = (
    "Результат — ОЦЕНКА по доступной геометрии зданий, а не точное измерение тени.",
    "Модель 2.5D: здания — призмы с плоской крышей на плоской земле; форма крыш, рельеф, "
    "деревья и навесы не учитываются.",
    "Учтено прямое солнечное освещение в один момент времени; отражённый свет и полутень не моделируются.",
)
