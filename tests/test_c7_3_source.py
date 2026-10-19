"""C7.3 source contract: parsing, units, gaps, and the upstream fetch budget.

These tests are offline (``httpx.MockTransport``). They pin the properties the
engine's honesty depends on:

* ``null`` is a gap and is NEVER replaced by zero;
* a confirmed ``0.0`` IS a value and is preserved (the real off-season case);
* wrong units, wrong chronology, a missing species column, an out-of-domain
  error payload and a response from another grid cell are all refused;
* one upstream fetch per validity window, even for concurrent callers, and the
  request carries the fixed centroid only — never a user's route.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.services.pollen_engine.source import (
    MAX_CONCENTRATION,
    MAX_SERIES_HOURS,
    SPECIES_CODES,
    UNIT,
    DisabledPollenProvider,
    OpenMeteoPollenProvider,
    PollenUnavailable,
    parse_open_meteo_air_quality,
)
from tests.pollen_fixtures import ORIGIN_LAT, ORIGIN_LON

NOW = datetime(2026, 7, 17, 9, 0, tzinfo=timezone.utc)
START = int(NOW.timestamp())


def _payload(*, hours: int = 6, value: float | None = 0.0,
             missing: tuple[int, ...] = (), units: dict | None = None,
             utc_offset_seconds: int = 0, latitude: float = 59.9,
             longitude: float = 30.3, drop: str | None = None,
             step: int = 3600, error: dict | None = None) -> dict:
    if error is not None:
        return error
    times = [START]
    for _ in range(hours - 1):
        times.append(times[-1] + step)
    hourly = {"time": times}
    for code in SPECIES_CODES:
        if code == drop:
            continue
        hourly[code] = [None if index in missing else value
                        for index in range(len(times))]
    return {
        "latitude": latitude, "longitude": longitude,
        "utc_offset_seconds": utc_offset_seconds,
        "hourly_units": units or {"time": "unixtime",
                                  **{code: UNIT for code in SPECIES_CODES}},
        "hourly": hourly,
    }


def _parse(payload: dict):
    return parse_open_meteo_air_quality(
        payload, NOW, contacted_latitude=ORIGIN_LAT,
        contacted_longitude=ORIGIN_LON)


def _provider(handler, *, clock, latitude=ORIGIN_LAT, longitude=ORIGIN_LON):
    return OpenMeteoPollenProvider(
        latitude=latitude, longitude=longitude, clock=clock,
        transport=httpx.MockTransport(handler))


def test_valid_payload_parses_with_modelled_provenance():
    parsed = _parse(_payload(hours=5))
    assert len(parsed.hours) == 5
    assert [hour.starts_at for hour in parsed.hours] == [
        NOW + timedelta(hours=index) for index in range(5)]
    provenance = parsed.provenance()
    assert provenance["available"] is True
    assert provenance["model"] == "cams_europe"
    assert provenance["unit"] == UNIT
    assert provenance["license"] == "CC-BY-4.0"
    assert "Open-Meteo" in provenance["attribution"]
    assert "CAMS" in provenance["attribution"]
    assert provenance["observed_at"] is None
    assert provenance["spatial_resolution_meters"] == 11000
    assert provenance["grid_step_degrees"] == 0.1
    assert "instantaneous" in provenance["time_semantics"]
    assert list(provenance["species"]) == list(SPECIES_CODES)
    # The interval is [first bin start, last bin start + 1 h) — the model's own
    # horizon edge, not the last stamp.
    assert provenance["interval_start"] == NOW.isoformat()
    assert provenance["interval_end"] == (NOW + timedelta(hours=5)).isoformat()


def test_the_snapped_cell_and_its_bounds_are_reported():
    parsed = _parse(_payload(latitude=59.90001, longitude=30.300003))
    assert parsed.cell_latitude == pytest.approx(59.9)
    assert parsed.cell_longitude == pytest.approx(30.3)
    assert parsed.cell_bounds() == (59.85, 30.25, 59.95, 30.35)
    assert parsed.contains_point(59.94, 30.31) is True
    assert parsed.contains_point(59.94, 30.36) is False
    assert parsed.contains_point(59.5, 30.31) is False


def test_null_hours_are_gaps_never_zero_filled():
    parsed = _parse(_payload(hours=4, missing=(1, 2)))
    assert parsed.hours[0].concentrations == (0.0,) * len(SPECIES_CODES)
    assert parsed.hours[1].concentrations == (None,) * len(SPECIES_CODES)
    assert parsed.hours[2].concentrations == (None,) * len(SPECIES_CODES)
    assert parsed.hours[3].concentrations == (0.0,) * len(SPECIES_CODES)


def test_a_confirmed_zero_is_preserved_as_a_value():
    parsed = _parse(_payload(hours=3, value=0.0))
    assert all(value == 0.0 for hour in parsed.hours
               for value in hour.concentrations)


def test_non_zero_values_survive_parsing_unrounded():
    parsed = _parse(_payload(hours=2, value=348.6))
    assert parsed.hours[0].concentrations[0] == pytest.approx(348.6)


@pytest.mark.parametrize("mutation, reason", [
    ("utc", "non-UTC offset"),
    ("units", "wrong concentration unit"),
    ("time_units", "wrong time unit"),
    ("chronology", "non-hourly step"),
    ("duplicate", "repeated stamp"),
    ("drop", "missing species column"),
    ("empty", "no hours at all"),
    ("too_long", "more hours than the bound"),
    ("negative", "negative concentration"),
    ("huge", "implausible concentration"),
    ("boolean", "a boolean is not a concentration"),
    ("nan", "not a number"),
    ("far_cell", "a response from another grid cell"),
    ("error", "an out-of-domain error payload"),
    ("not_a_dict", "not even an object"),
])
def test_malformed_payloads_are_refused(mutation, reason):
    if mutation == "utc":
        payload = _payload(utc_offset_seconds=10800)
    elif mutation == "units":
        payload = _payload(units={"time": "unixtime",
                                  **{code: "µg/m³" for code in SPECIES_CODES}})
    elif mutation == "time_units":
        payload = _payload(units={"time": "iso8601",
                                  **{code: UNIT for code in SPECIES_CODES}})
    elif mutation == "chronology":
        payload = _payload(hours=3, step=7200)
    elif mutation == "duplicate":
        payload = _payload(hours=3, step=0)
    elif mutation == "drop":
        payload = _payload(drop="birch_pollen")
    elif mutation == "empty":
        payload = _payload(hours=1)
        payload["hourly"] = {"time": []}
        for code in SPECIES_CODES:
            payload["hourly"][code] = []
    elif mutation == "too_long":
        payload = _payload(hours=MAX_SERIES_HOURS + 1)
    elif mutation == "negative":
        payload = _payload(value=-1.0)
    elif mutation == "huge":
        payload = _payload(value=MAX_CONCENTRATION + 1.0)
    elif mutation == "boolean":
        payload = _payload(value=True)
    elif mutation == "nan":
        payload = _payload(value=float("nan"))
    elif mutation == "far_cell":
        payload = _payload(latitude=60.4, longitude=30.3)
    elif mutation == "error":
        payload = _payload(error={"error": True,
                                  "reason": "No data is available for this location"})
    else:
        payload = ["not", "an", "object"]
    with pytest.raises(PollenUnavailable) as info:
        _parse(payload)
    assert info.value.reason == "pollen_source_invalid", reason


def test_a_transport_failure_is_unavailable():
    def handler(request):
        raise httpx.ConnectError("no route to host", request=request)

    with pytest.raises(PollenUnavailable):
        asyncio.run(_provider(handler, clock=lambda: NOW).fetch())


@pytest.mark.parametrize("status_code, body", [
    (400, {"error": True, "reason": "No data is available for this location"}),
    (503, {"error": True, "reason": "Service unavailable"}),
])
def test_error_statuses_are_unavailable(status_code, body):
    def handler(request):
        return httpx.Response(status_code, json=body)

    with pytest.raises(PollenUnavailable):
        asyncio.run(_provider(handler, clock=lambda: NOW).fetch())


def test_oversize_response_is_refused_before_parsing():
    def handler(request):
        # 300 KB of padding: the transport cap must reject it, not the parser.
        return httpx.Response(200, content=b'{"pad": "' + b"x" * 300_000 + b'"}')

    with pytest.raises(PollenUnavailable):
        asyncio.run(_provider(handler, clock=lambda: NOW).fetch())


def test_provider_caches_one_bounded_fetch_per_validity_window():
    calls = []
    now = {"value": NOW}

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=_payload(hours=6))

    async def run():
        provider = _provider(handler, clock=lambda: now["value"])
        first = await provider.fetch()
        second = await provider.fetch()
        assert first is second
        assert len(calls) == 1
        url = str(calls[0].url)
        # One fixed centroid, the pinned model, and NOTHING about the user.
        assert "59.9405" in url and "30.3165" in url
        assert "models=cams_europe" in url
        assert "timeformat=unixtime" in url and "timezone=UTC" in url
        assert "past_days=1" in url and "forecast_days=4" in url
        # Nothing about the user travels upstream: one fixed centroid only.
        for forbidden in ("route", "legs", "geometry", "start", "end", "track"):
            assert forbidden not in url
        now["value"] = NOW + timedelta(minutes=61)
        third = await provider.fetch()
        assert third is not first
        assert len(calls) == 2

    asyncio.run(run())


def test_concurrent_fetches_hit_the_transport_once():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=_payload(hours=6))

    async def run():
        provider = _provider(handler, clock=lambda: NOW)
        results = await asyncio.gather(*(provider.fetch() for _ in range(8)))
        assert len(calls) == 1
        assert all(result is results[0] for result in results)

    asyncio.run(run())


def test_disabled_provider_reports_its_own_reason():
    with pytest.raises(PollenUnavailable) as info:
        asyncio.run(DisabledPollenProvider().fetch())
    assert info.value.reason == "pollen_not_configured"


def test_the_payload_shape_matches_the_stored_real_response():
    """The fixture builder must describe the same shape the real source sent.

    ``docs/phase_c7_3_acquisition/pollen_public_pilot.json`` is the verbatim
    response recorded by ``scripts.probe_phase_c7_3_pollen``; this keeps the
    synthetic payload from drifting away from it.
    """
    from pathlib import Path
    stored = Path(__file__).resolve().parents[1] / "docs" / "phase_c7_3_acquisition" \
        / "pollen_public_pilot.json"
    real = json.loads(stored.read_text(encoding="utf-8"))
    synthetic = _payload(hours=2)
    assert set(real) - set(synthetic) == {"generationtime_ms", "timezone",
                                          "timezone_abbreviation", "elevation"}
    assert set(real["hourly"]) == set(synthetic["hourly"])
    assert real["hourly_units"] == synthetic["hourly_units"]
    assert real["utc_offset_seconds"] == synthetic["utc_offset_seconds"] == 0
    assert real["timezone"] == "GMT"
