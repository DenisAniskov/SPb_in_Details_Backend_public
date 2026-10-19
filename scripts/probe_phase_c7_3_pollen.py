"""One explicit public-pilot Open-Meteo Air Quality probe, never a runtime cache.

From backend: python -m scripts.probe_phase_c7_3_pollen [--verify]

The pollen variables come from the CAMS European Air Quality forecast (0.1°
grid, ~11 km) and are available in Europe; the free endpoint is for
non-commercial use and requires attribution (see
docs/PHASE_C7_3_SOURCE_FEASIBILITY.md).

This script records three kinds of evidence and NOTHING that production may
read as pollen data:

* ``pollen_public_pilot.json`` — one real response in exactly the shape the
  production provider requests (6 species, one fixed pilot centroid);
* ``pollen_grid_probe.json`` — the requested→returned coordinate pairs that
  prove how a requested point is snapped to a 0.1° model cell, and therefore
  how far a single cell's value can honestly be applied;
* ``pollen_season_probe.json`` — 92 days of past hours, the evidence that the
  source carries a real, botanically coherent seasonal signal (and that
  off-season hours are returned as 0.0, not null).

The bytes are stored verbatim with their SHA-256 so any later claim can be
re-checked offline. No keys, no personal data, one fixed centroid only.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "docs/phase_c7_3_acquisition"
ENDPOINT = "https://air-quality-api.open-meteo.com/v1/air-quality"
SOURCE_URL = "https://open-meteo.com/en/docs/air-quality-api"
CLIENT = ("SmartNavigator-SPb-C7.3/1.0 bounded research pilot "
          "https://github.com/DenisAniskov/SmartNavigator_Spb")
MAX_RAW_BYTES = 2_000_000

#: The production request shape: one fixed pilot centroid, the whole pollen
#: panel, CAMS Europe explicitly pinned, UTC + unixtime, one past day (so an
#: assessment starting "now" has the hour it falls into) and the documented
#: four pollen forecast days.
SPECIES = ("alder_pollen", "birch_pollen", "grass_pollen",
           "mugwort_pollen", "olive_pollen", "ragweed_pollen")
CENTROID = (59.9405, 30.3165)
PRODUCTION_PARAMETERS = {
    "latitude": f"{CENTROID[0]}",
    "longitude": f"{CENTROID[1]}",
    "hourly": ",".join(SPECIES),
    "models": "cams_europe",
    "timezone": "UTC",
    "timeformat": "unixtime",
    "past_days": "1",
    "forecast_days": "4",
}
#: Requested points whose returned cell centre reveals the snapping rule.
GRID_POINTS = ((59.849, 30.31), (59.851, 30.31), (59.949, 30.31),
               (59.951, 30.31), (59.94, 30.249), (59.94, 30.251),
               (59.94, 30.349), (59.94, 30.351),
               (59.50, 30.31), (60.40, 30.31))


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _get(parameters: dict) -> tuple[bytes, str | None]:
    url = ENDPOINT + "?" + urllib.parse.urlencode(parameters)
    request = urllib.request.Request(url, headers={"User-Agent": CLIENT})
    with urllib.request.urlopen(request, timeout=40) as response:
        payload = response.read(MAX_RAW_BYTES + 1)
        return payload, response.headers.get("Date")


def _loads(payload: bytes) -> dict:
    return json.loads(payload.decode("utf-8"))


def validate_pollen_series(payload: bytes, requested: tuple[float, float]) -> dict:
    """The same invariants the production parser enforces (offline check)."""
    data = _loads(payload)
    if data.get("error") or data.get("utc_offset_seconds") != 0:
        raise ValueError("Air-quality error or non-UTC response")
    units = data["hourly_units"]
    if units["time"] != "unixtime":
        raise ValueError("Unexpected time unit")
    for species in SPECIES:
        if units.get(species) != "grains/m³":
            raise ValueError(f"Unexpected unit for {species}")
    times = data["hourly"]["time"]
    if not times or any(b - a != 3600 for a, b in zip(times, times[1:])):
        raise ValueError("Missing/non-hourly pollen intervals")
    for species in SPECIES:
        values = data["hourly"][species]
        if len(values) != len(times):
            raise ValueError(f"Length mismatch for {species}")
        for value in values:
            if value is None:
                continue
            if not isinstance(value, (int, float)) or not 0 <= value <= 100_000:
                raise ValueError(f"Implausible concentration for {species}")
    if abs(data["latitude"] - requested[0]) > 0.25 \
            or abs(data["longitude"] - requested[1]) > 0.25:
        raise ValueError("Response cell is not near the requested point")
    return data


def _write(name: str, payload: bytes) -> str:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / name
    if name.endswith(".gz"):
        path.write_bytes(gzip.compress(payload, mtime=0))
    else:
        path.write_bytes(payload)
    return sha256(payload)


def _read(name: str) -> bytes:
    raw = (CACHE / name).read_bytes()
    return gzip.decompress(raw) if name.endswith(".gz") else raw


def _season_summary(data: dict) -> dict:
    times = data["hourly"]["time"]
    summary = {"hours": len(times),
               "interval_start": datetime.fromtimestamp(times[0], timezone.utc)
               .isoformat().replace("+00:00", "Z"),
               "interval_end": datetime.fromtimestamp(times[-1], timezone.utc)
               .isoformat().replace("+00:00", "Z"),
               "species": {}}
    for species in SPECIES:
        values = data["hourly"][species]
        known = [(t, v) for t, v in zip(times, values) if v is not None]
        nonzero = [(t, v) for t, v in known if v > 0]
        peak = max(nonzero, key=lambda item: item[1]) if nonzero else None
        summary["species"][species] = {
            "known_hours": len(known), "nonzero_hours": len(nonzero),
            "max_grains_per_m3": peak[1] if peak else None,
            "max_at": (datetime.fromtimestamp(peak[0], timezone.utc)
                       .isoformat().replace("+00:00", "Z")) if peak else None,
            "first_nonzero_at": (datetime.fromtimestamp(nonzero[0][0], timezone.utc)
                                 .isoformat().replace("+00:00", "Z")) if nonzero else None,
            "last_nonzero_at": (datetime.fromtimestamp(nonzero[-1][0], timezone.utc)
                                .isoformat().replace("+00:00", "Z")) if nonzero else None,
        }
    return summary


def _verify_offline() -> None:
    metadata = json.loads((CACHE / "pollen_probe.json").read_text(encoding="utf-8"))
    if metadata["status"] != "verified_response":
        raise ValueError("No successful real response is recorded")

    for key, name, request in (
            ("production_response", "pollen_public_pilot.json", CENTROID),
            ("season_response", "pollen_season_probe.json.gz", CENTROID)):
        payload = _read(name)
        if sha256(payload) != metadata[key]["raw_sha256"]:
            raise ValueError(f"{key} checksum mismatch")
        validate_pollen_series(payload, request)

    grid = json.loads((CACHE / "pollen_grid_probe.json").read_text(encoding="utf-8"))
    if sha256(json.dumps(grid["points"], sort_keys=True).encode()) \
            != metadata["grid_response"]["points_sha256"]:
        raise ValueError("Grid probe checksum mismatch")
    if grid["cell_bounds_lat"] != [59.85, 59.95] \
            or grid["cell_bounds_lon"] != [30.25, 30.35]:
        raise ValueError("The recorded snapping rule changed")
    print("Pollen source evidence verified offline; NOT current pollen concentrations")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if args.verify:
        _verify_offline()
        return

    metadata: dict = {
        "status": "verified_response",
        "endpoint": ENDPOINT,
        "source_url": SOURCE_URL,
        "provider": "Open-Meteo",
        "model": "cams_europe",
        "model_name": "CAMS European Air Quality Forecast (CAMS ENSEMBLE)",
        "data_kind": "modeled_forecast_and_recent_archived_forecast",
        "observed_at_utc": None,
        "model_issued_at_utc": None,
        "pollens": list(SPECIES),
        "concentration_unit": "grains/m³",
        "interval_seconds": 3600,
        "time_semantics": ("instantaneous value valid for the indicated hour "
                           "[t, t+1h) — NOT an accumulated preceding-hour sum"),
        "model_grid_degrees": 0.1,
        "nominal_model_resolution_meters": 11000,
        "forecast_horizon_days": 4,
        "license": "CC-BY-4.0",
        "attribution": ("Pollen data by Open-Meteo.com; CAMS ENSEMBLE "
                        "(Copernicus Atmosphere Monitoring Service) model"),
        "service_use": "noncommercial_research_probe",
        "not_a_runtime_pollen_cache": True,
        "no_sensitive_data_recorded": True,
    }
    try:
        payload, date_header = _get(PRODUCTION_PARAMETERS)
        data = validate_pollen_series(payload, CENTROID)
        metadata["retrieved_at_utc"] = utc_now()
        metadata["response_date_header"] = date_header
        metadata["production_request"] = PRODUCTION_PARAMETERS
        metadata["production_response"] = {
            "file": "pollen_public_pilot.json",
            "raw_sha256": _write("pollen_public_pilot.json", payload),
            "bytes": len(payload),
            "returned_cell": [data["latitude"], data["longitude"]],
            "hours": len(data["hourly"]["time"]),
        }
        metadata["production_known_hours_per_species"] = {
            species: sum(1 for value in data["hourly"][species] if value is not None)
            for species in SPECIES}

        points = []
        for latitude, longitude in GRID_POINTS:
            parameters = {**PRODUCTION_PARAMETERS,
                          "latitude": f"{latitude}", "longitude": f"{longitude}",
                          "past_days": "0", "forecast_days": "1"}
            raw, _ = _get(parameters)
            returned = _loads(raw)
            if returned.get("error"):
                points.append({"requested": [latitude, longitude],
                               "returned_cell": None,
                               "error": returned.get("reason")})
                continue
            points.append({"requested": [latitude, longitude],
                           "returned_cell": [returned["latitude"],
                                             returned["longitude"]]})
        grid = {"endpoint": ENDPOINT, "model": "cams_europe",
                "grid_step_degrees": 0.1,
                "cell_bounds_lat": [59.85, 59.95],
                "cell_bounds_lon": [30.25, 30.35],
                "cell_centre": [59.9, 30.3],
                "interpretation": (
                    "A requested point is snapped to the nearest 0.1° CAMS "
                    "Europe cell centre, so a fetch at the pilot centroid "
                    "describes exactly the cell [59.85,59.95)x[30.25,30.35) "
                    "and nothing outside it."),
                "points": points}
        (CACHE / "pollen_grid_probe.json").write_text(
            json.dumps(grid, indent=2, ensure_ascii=False), encoding="utf-8")
        metadata["grid_response"] = {
            "file": "pollen_grid_probe.json",
            "points_sha256": sha256(json.dumps(points, sort_keys=True).encode())}

        season_parameters = {**PRODUCTION_PARAMETERS, "past_days": "92",
                             "forecast_days": "1"}
        season_payload, _ = _get(season_parameters)
        season = validate_pollen_series(season_payload, CENTROID)
        metadata["season_request"] = season_parameters
        metadata["season_response"] = {
            "file": "pollen_season_probe.json.gz",
            "raw_sha256": _write("pollen_season_probe.json.gz", season_payload),
            "bytes": len(season_payload),
            "summary": _season_summary(season),
        }

        (CACHE / "pollen_probe.json").write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps({"production": metadata["production_response"],
                          "season": metadata["season_response"]["summary"]},
                         indent=2, ensure_ascii=False))
    except (urllib.error.URLError, KeyError, TypeError, ValueError) as exc:
        metadata["status"] = "failed"
        metadata["failure"] = f"{type(exc).__name__}: {exc}"
        metadata["retrieved_at_utc"] = utc_now()
        (CACHE / "pollen_probe.json").write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
        raise


if __name__ == "__main__":
    main()
