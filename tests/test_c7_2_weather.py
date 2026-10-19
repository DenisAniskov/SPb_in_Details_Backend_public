"""C7.2 weather provider: strict parsing, bounded caching, honest failure.

The provider is the only C7.2 component that talks to an external service.
These tests pin its contract offline with a MockTransport transport:

* strict units / UTC / chronology validation — a malformed payload becomes
  ``WeatherUnavailable``, never a silently repaired series;
* null hours remain gaps (they are dropped, they are not filled with zero);
* archived/modeled data is labelled as such — no ``observed_at`` claim;
* the cache is bounded (one bounded fetch per validity window) and the
  user's route is never part of the request;
* the disabled provider is a first-class state: ``weather_not_configured``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.services.puddle_engine.weather import (
    DisabledWeatherProvider,
    OpenMeteoWeatherProvider,
    WeatherUnavailable,
    parse_open_meteo,
)

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
HOUR = 1_791_936_000  # 2026-10-14T00:00:00Z, an arbitrary hourly anchor


def _payload(times: list, precipitation: list, temperature: list,
             snowfall: list, *, offset: int = 0,
             units: dict | None = None) -> dict:
    return {
        "utc_offset_seconds": offset,
        "hourly_units": units or {"time": "unixtime", "precipitation": "mm",
                                  "snowfall": "cm", "temperature_2m": "°C"},
        "hourly": {"time": times, "precipitation": precipitation,
                   "temperature_2m": temperature, "snowfall": snowfall},
    }


def _valid(**overrides) -> dict:
    times = [HOUR + 3600 * index for index in range(4)]
    values = {"times": times, "precipitation": [0.2, 0.0, 1.5, 0.4],
              "temperature": [11.0, 11.5, 12.0, 12.0],
              "snowfall": [0.0, 0.0, 0.0, 0.0]}
    values.update(overrides)
    return _payload(values["times"], values["precipitation"],
                    values["temperature"], values["snowfall"],
                    offset=values.get("offset", 0), units=values.get("units"))


def test_valid_payload_parses_with_modeled_provenance():
    result = parse_open_meteo(_valid(), NOW)
    assert [hour.precipitation_mm for hour in result.hours] == [0.2, 0.0, 1.5, 0.4]
    provenance = result.provenance()
    assert provenance["available"] is True
    assert provenance["observed_at"] is None
    assert provenance["model_issued_at"] is None
    assert provenance["license"] == "CC-BY-4.0"
    assert provenance["interval_start"] == "2026-10-13T23:00:00+00:00"
    assert provenance["interval_end"] == "2026-10-14T03:00:00+00:00"
    assert "modeled" in provenance["kind"]
    assert provenance["valid_until"] == (NOW + timedelta(minutes=15)).isoformat()


def test_null_hours_are_dropped_as_gaps_not_filled_with_zero():
    payload = _valid(precipitation=[0.2, None, 1.5, 0.4])
    result = parse_open_meteo(payload, NOW)
    assert [hour.precipitation_mm for hour in result.hours] == [0.2, 1.5, 0.4]
    gaps = [(b.ends_at - a.ends_at).total_seconds()
            for a, b in zip(result.hours, result.hours[1:])]
    assert gaps == [7200.0, 3600.0]  # the 2-hour jump is the recorded gap


@pytest.mark.parametrize("mutation, message", [
    ({"offset": 10800}, "non-UTC offset"),
    ({"units": {"time": "unixtime", "precipitation": "inch",
                "snowfall": "cm", "temperature_2m": "°C"}}, "wrong unit"),
    ({"precipitation": [0.2, 0.0, 1.5, 0.4, 9.9]}, "length mismatch"),
    ({"temperature": [11.0, 11.5, 12.0, 999.0]}, "out-of-range value"),
])
def test_malformed_payloads_are_refused(mutation, message):
    with pytest.raises(WeatherUnavailable):
        parse_open_meteo(_valid(**mutation), NOW)


def test_non_hourly_chronology_is_refused():
    payload = _valid()
    payload["hourly"]["time"][2] += 120  # breaks the strict 3600 s step
    with pytest.raises(WeatherUnavailable):
        parse_open_meteo(payload, NOW)


def test_empty_or_unbounded_series_is_refused():
    with pytest.raises(WeatherUnavailable):
        parse_open_meteo(_payload([], [], [], []), NOW)
    times = [HOUR + 3600 * index for index in range(201)]
    with pytest.raises(WeatherUnavailable):
        parse_open_meteo(_payload(times, [0.0] * 201, [10.0] * 201,
                                  [0.0] * 201), NOW)


def _provider(handler, *, clock):
    return OpenMeteoWeatherProvider(
        latitude=59.9405, longitude=30.3165, clock=clock,
        transport=httpx.MockTransport(handler))


def test_provider_caches_one_bounded_fetch_per_validity_window():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_valid())

    moved = {"now": NOW}
    provider = _provider(handler, clock=lambda: moved["now"])

    async def run() -> None:
        first = await provider.fetch()
        moved["now"] = NOW + timedelta(minutes=5)
        second = await provider.fetch()
        assert first is second  # same cached series
        moved["now"] = NOW + timedelta(minutes=16)
        third = await provider.fetch()
        assert third is not first

    asyncio.run(run())
    assert len(calls) == 2
    # The user's route is NEVER sent upstream: only the fixed pilot centroid.
    first_url = str(calls[0].url)
    assert "latitude=59.9405" in first_url and "longitude=30.3165" in first_url
    assert "route" not in first_url and "legs" not in first_url


def test_concurrent_fetches_hit_the_transport_once():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_valid())

    provider = _provider(handler, clock=lambda: NOW)

    async def run() -> None:
        first, second = await asyncio.gather(provider.fetch(), provider.fetch())
        assert first is second

    asyncio.run(run())
    assert len(calls) == 1


def test_transport_failures_and_error_statuses_are_unavailable():
    def connect_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    provider = _provider(connect_error, clock=lambda: NOW)
    with pytest.raises(WeatherUnavailable):
        asyncio.run(provider.fetch())

    def server_error(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": True})

    provider = _provider(server_error, clock=lambda: NOW)
    with pytest.raises(WeatherUnavailable):
        asyncio.run(provider.fetch())


def test_oversize_response_is_refused_before_parsing():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{" + b" " * 200_000)

    provider = _provider(handler, clock=lambda: NOW)
    with pytest.raises(WeatherUnavailable):
        asyncio.run(provider.fetch())


def test_disabled_provider_reports_its_own_reason():
    with pytest.raises(WeatherUnavailable) as info:
        asyncio.run(DisabledWeatherProvider().fetch())
    assert info.value.reason == "weather_not_configured"
