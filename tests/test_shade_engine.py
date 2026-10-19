"""End-to-end engine and weather-interface tests (Phases 6 pipeline + 7)."""

from datetime import datetime, timezone

import pytest

from app.services.shade_engine.buildings import HeightProvenance
from app.services.shade_engine.engine import ShadeEngine, route_bbox
from app.services.shade_engine.intersection import InvalidRouteError
from app.services.shade_engine.weather import ShadeRelevance, WeatherContext, shade_estimate_relevance
from shade_fixtures import FRAME, rect_footprint, synthetic_provider

UTC = timezone.utc
SPB_NOON_WINTER = datetime(2024, 12, 21, 9, 57, tzinfo=UTC)   # elev ≈ 6.6°, az ≈ 180°
SPB_SUMMER = datetime(2024, 6, 20, 9, 57, tzinfo=UTC)         # elev ≈ 53.5°, az ≈ 180°
SPB_NIGHT = datetime(2024, 12, 20, 21, 57, tzinfo=UTC)        # elev ≈ −53.5°


def make_engine(*footprints) -> ShadeEngine:
    return ShadeEngine(building_provider=synthetic_provider(*footprints))


# --------------------------------------------------------------------------
# Pipeline end-to-end with synthetic (explicitly labeled) data
# --------------------------------------------------------------------------

def test_end_to_end_partial_shade_invariants():
    # Building east of a N-S route; summer noon: shadow due north ≈ 37 m from
    # the 50 m building (L = 50 / tan(53.5°)), so the route is PARTIALLY shaded.
    fp = rect_footprint(FRAME, 40.0, 0.0, 30.0, 30.0, height_m=50.0,
                        provenance=HeightProvenance.KNOWN, source_id="b1")
    engine = make_engine(fp)
    # Route placed NORTH of the building so the noon shadow lies over it.
    route = [FRAME.to_latlon(40.0, 20.0), FRAME.to_latlon(40.0, 90.0)]
    result = engine.analyze_route(route, SPB_SUMMER)
    assert result.status == "ok"
    assert 0.0 < result.shade_fraction < 1.0
    assert result.total_length_m == pytest.approx(70.0, abs=0.05)
    assert result.shaded_length_m + result.exposed_length_m == pytest.approx(result.total_length_m, abs=0.05)
    assert result.buildings_used == 1
    assert result.provenance_known == 1
    assert result.limitations  # honesty marker always present
    assert any("ОЦЕНКА" in lim for lim in result.limitations)
    assert result.method.startswith("shade_engine_v1")


def SPB_WINTER_LOW():
    # 2024-02-21 ~09:57 UTC: sun elev ≈ 15°, az ≈ 165–175° (low, southern sky)
    return datetime(2024, 2, 21, 9, 57, tzinfo=UTC)


def test_below_horizon_status():
    engine = make_engine(rect_footprint(FRAME, 0, 0, 20, 20, 30.0))
    result = engine.analyze_route([FRAME.to_latlon(0, -50), FRAME.to_latlon(0, 50)], SPB_NIGHT)
    assert result.status == "sun_below_horizon"
    assert result.shade_fraction is None and result.total_length_m is None
    assert result.solar.above_horizon is False
    assert result.relevance == ShadeRelevance.NOT_APPLICABLE


def test_unavailable_without_provider():
    engine = ShadeEngine()  # production default: EmptyBuildingProvider
    result = engine.analyze_route([FRAME.to_latlon(0, -50), FRAME.to_latlon(0, 50)], SPB_SUMMER)
    assert result.status == "unavailable"
    assert result.shade_fraction is None
    assert result.provider_name == "none_configured"
    assert result.reason and "OpenStreetMap" in result.reason


def test_unavailable_when_no_buildings_for_route_area():
    fp = rect_footprint(FRAME, 5000.0, 5000.0, 20.0, 20.0, 30.0)  # ~5 km away
    # A provider whose only building is ~5 km away must yield "unavailable":
    class FarOnlyProvider:
        name = "far_only"

        def available(self):
            return True

        def provenance_description(self):
            return "test fixture provider"

        def footprints_in_bbox(self, bbox):
            min_lat, min_lon, max_lat, max_lon = bbox
            fp_ll = fp.polygon_latlon
            lats = [p[0] for p in fp_ll]
            lons = [p[1] for p in fp_ll]
            if max(lats) >= min_lat and min(lats) <= max_lat and max(lons) >= min_lon and min(lons) <= max_lon:
                return [fp]
            return []

    route = [FRAME.to_latlon(0.0, -50.0), FRAME.to_latlon(0.0, 50.0)]
    result2 = ShadeEngine(building_provider=FarOnlyProvider()).analyze_route(route, SPB_SUMMER)
    assert result2.status == "unavailable"
    assert "ни одного здания" in result2.reason


def test_route_bbox_padding_catches_nearby_buildings():
    route = [FRAME.to_latlon(0, 0), FRAME.to_latlon(0, 100.0)]
    bbox = route_bbox(route, pad_m=1000.0)  # (min_lat, min_lon, max_lat, max_lon)
    # 1000 m pad ≈ 0.009° of latitude
    assert bbox[0] < FRAME.lat0 - 0.008
    assert bbox[2] > max(p[0] for p in route) + 0.008
    # Pad must stay sane for zero-extent routes
    bbox2 = route_bbox([route[0], route[0]], pad_m=500.0)
    assert bbox2[2] > bbox2[0] and bbox2[3] > bbox2[1]


def test_degenerate_route_raises_through_engine():
    engine = make_engine(rect_footprint(FRAME, 0, 0, 20, 20, 30.0))
    p = FRAME.to_latlon(0, 0)
    with pytest.raises(InvalidRouteError):
        engine.analyze_route([p, p], SPB_SUMMER)


def test_unknown_height_counts_in_limitations():
    fps = [
        rect_footprint(FRAME, 40.0, 0.0, 30.0, 30.0, height_m=50.0,
                       provenance=HeightProvenance.KNOWN, source_id="b1"),
        rect_footprint(FRAME, -40.0, 0.0, 30.0, 30.0, height_m=None,
                       provenance=HeightProvenance.UNAVAILABLE, source_id="b2"),
    ]
    engine = make_engine(*fps)
    route = [FRAME.to_latlon(40.0, 20.0), FRAME.to_latlon(40.0, 90.0)]
    result = engine.analyze_route(route, SPB_WINTER_LOW())
    assert result.status == "ok"
    assert result.buildings_used == 1
    assert result.buildings_excluded_unknown_height == 1
    assert any("без известной высоты" in lim for lim in result.limitations)


# --------------------------------------------------------------------------
# Weather interface (Phase 7): relevance only, geometry untouched
# --------------------------------------------------------------------------

@pytest.mark.parametrize("cloud,expected", [
    (0.0, ShadeRelevance.HIGH),
    (0.2, ShadeRelevance.HIGH),
    (0.5, ShadeRelevance.MODERATE),
    (0.6, ShadeRelevance.MODERATE),
    (0.9, ShadeRelevance.LOW),
    (None, ShadeRelevance.UNKNOWN),
])
def test_relevance_mapping(cloud, expected):
    rel, note = shade_estimate_relevance(WeatherContext(cloud_cover_fraction=cloud), True)
    assert rel == expected
    assert note


def test_relevance_not_applicable_at_night():
    rel, _ = shade_estimate_relevance(WeatherContext(0.0), False)
    assert rel == ShadeRelevance.NOT_APPLICABLE


def test_weather_context_validation():
    with pytest.raises(ValueError):
        WeatherContext(cloud_cover_fraction=1.5)
    with pytest.raises(ValueError):
        WeatherContext(cloud_cover_fraction=-0.1)


def test_cloud_cover_never_changes_shadow_geometry():
    fp = rect_footprint(FRAME, 40.0, 0.0, 30.0, 30.0, height_m=50.0,
                        provenance=HeightProvenance.KNOWN, source_id="b1")
    engine = make_engine(fp)
    route = [FRAME.to_latlon(40.0, 20.0), FRAME.to_latlon(40.0, 90.0)]
    baseline = engine.analyze_route(route, SPB_WINTER_LOW(), weather=WeatherContext(0.0))
    variants = [
        engine.analyze_route(route, SPB_WINTER_LOW(), weather=WeatherContext(0.9)),
        engine.analyze_route(route, SPB_WINTER_LOW(), weather=WeatherContext(0.45)),
        engine.analyze_route(route, SPB_WINTER_LOW(), weather=None),
    ]
    for v in variants:
        assert v.shade_fraction == baseline.shade_fraction  # bit-identical
        assert v.shaded_length_m == baseline.shaded_length_m
    # while relevance DOES change
    assert baseline.relevance == ShadeRelevance.HIGH
    assert variants[0].relevance == ShadeRelevance.LOW
    assert variants[2].relevance == ShadeRelevance.UNKNOWN
