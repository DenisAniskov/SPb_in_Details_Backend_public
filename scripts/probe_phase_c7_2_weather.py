"""One explicit public-pilot Open-Meteo probe, never production weather fallback.

From backend: python -m scripts.probe_phase_c7_2_weather [--verify]
The free endpoint is for non-commercial use; see PHASE_C7_2_DATA_PROVENANCE.md.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request

from scripts.acquire_phase_c7_2_osm import CACHE, CLIENT, utc_now

PARAMETERS = {"latitude": "59.9405", "longitude": "30.3165",
              "hourly": "precipitation,rain,showers,snowfall,temperature_2m",
              "past_days": "2", "forecast_days": "2", "models": "icon_global",
              "timezone": "UTC", "timeformat": "unixtime", "precipitation_unit": "mm"}
URL = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(PARAMETERS)
RAW = CACHE / "weather_public_pilot.json"
META = CACHE / "weather_probe.json"


def validate(payload: bytes) -> dict:
    data = json.loads(payload)
    if data.get("error") or data.get("utc_offset_seconds") != 0:
        raise ValueError("Weather error or non-UTC response")
    times = data["hourly"]["time"]
    if not times or any(b - a != 3600 for a, b in zip(times, times[1:])):
        raise ValueError("Missing/non-hourly weather intervals")
    if data["hourly_units"]["precipitation"] != "mm":
        raise ValueError("Unexpected precipitation unit")
    for field in PARAMETERS["hourly"].split(","):
        values = data["hourly"][field]
        if len(values) != len(times) or any(value is None for value in values):
            raise ValueError("Incomplete weather timeseries")
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if args.verify:
        metadata = json.loads(META.read_text(encoding="utf-8"))
        if metadata["status"] != "verified_response":
            raise ValueError("No successful real response is recorded")
        payload = RAW.read_bytes()
        if hashlib.sha256(payload).hexdigest() != metadata["raw_sha256"]:
            raise ValueError("Weather probe checksum mismatch")
        validate(payload)
        print("Weather public-pilot evidence verified offline; NOT current runtime weather")
        return
    CACHE.mkdir(parents=True, exist_ok=True)
    metadata = {"endpoint": URL, "requested_model": "icon_global",
                "retrieved_at_utc": utc_now(), "data_kind": "model_forecast_and_recent_archived_forecast",
                "observed_at_utc": None, "model_issued_at_utc": None,
                "model_grid_degrees": 0.1, "nominal_model_resolution_meters": 11000,
                "interval_seconds": 3600, "precipitation_interval": "(timestamp-3600,timestamp]",
                "license": "CC-BY-4.0", "service_use": "noncommercial_research_probe",
                "not_a_runtime_weather_cache": True}
    try:
        with urllib.request.urlopen(urllib.request.Request(URL, headers={"User-Agent": CLIENT}),
                                    timeout=25) as response:
            payload = response.read(500_001)
            metadata["response_date_header"] = response.headers.get("Date")
        if len(payload) > 500_000:
            raise ValueError("Weather response exceeds probe bound")
        data = validate(payload)
        RAW.write_bytes(payload)
        times = data["hourly"]["time"]
        metadata.update({"status": "verified_response", "raw_bytes": len(payload),
                         "raw_sha256": hashlib.sha256(payload).hexdigest(),
                         "response_grid_center": [data["longitude"], data["latitude"]],
                         "hour_count": len(times),
                         "first_interval_end_utc": datetime.fromtimestamp(times[0], timezone.utc).isoformat(),
                         "last_interval_end_utc": datetime.fromtimestamp(times[-1], timezone.utc).isoformat()})
    except (OSError, ValueError, KeyError, urllib.error.HTTPError) as error:
        metadata.update({"status": "unavailable", "error_type": type(error).__name__,
                         "http_status": getattr(error, "code", None)})
    META.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8", newline="\n")
    print(json.dumps({key: metadata[key] for key in ("status", "retrieved_at_utc")}))


if __name__ == "__main__":
    main()
