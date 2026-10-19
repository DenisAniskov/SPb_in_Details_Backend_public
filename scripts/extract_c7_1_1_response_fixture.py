"""Regenerates the C7.1.1 cross-language contract fixture (deterministic).

Emits ``tests/fixtures/c7_1_1_shade_response.json``: the REAL response
of ``POST /api/v1/environment/shade/analyze`` produced through the production
wiring (the committed pilot dataset, no dependency overrides) for the real
``gostiny_dvor_arcade`` route at a fixed instant. The Flutter test
``§18 C7.1.1 backend payload contract`` parses this file, so the client's
parser is locked to the server's actual output instead of a hand-written
example.

Run from the repository root:

    python -m scripts.extract_c7_1_1_response_fixture

The file is a MODEL OUTPUT (a regression fixture), not a field measurement;
``include_segments`` is true so the classified segments with their measured
spans travel with it.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from app.main import app

BACKEND_ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = BACKEND_ROOT / "tests/fixtures/c7_1_scenarios.json"
OUT = BACKEND_ROOT / "tests/fixtures/c7_1_1_shade_response.json"
MSK = timezone(timedelta(hours=3))
DEPARTURE = datetime(2026, 7, 1, 14, 0, tzinfo=MSK)


async def _fetch() -> dict:
    scenario = json.loads(SCENARIOS.read_text(encoding="utf-8"))["scenarios"][
        "gostiny_dvor_arcade"]
    geometry = [{"latitude": lat, "longitude": lon}
                for lat, lon in scenario["geometry_lat_lon"]]
    payload = {
        "departure_at": DEPARTURE.isoformat(),
        "legs": [{"geometry": geometry,
                  "walking_duration_seconds": 3600.0,
                  "visit_duration_seconds_after_leg": 0.0}],
        "include_segments": True,
    }
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://fixture") as client:
        response = await client.post("/api/v1/environment/shade/analyze", json=payload)
    response.raise_for_status()
    return response.json()


def main() -> None:
    body = asyncio.run(_fetch())
    document = {
        "_provenance": (
            "C7.1.1 contract fixture: real response of the production wiring "
            "(committed pilot dataset c7_1_pilot_v1, shade_engine_c7_1_time_aware_2.5d_noaa "
            f"method_version {body['method_version']}) for the real gostiny_dvor_arcade "
            f"route at {DEPARTURE.isoformat()}. Regenerate with "
            "scripts/extract_c7_1_1_response_fixture.py. Model output, not a "
            "field measurement."
        ),
        "response": body,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes), status={body['status']}, "
          f"segments={len(body.get('classified_segments') or [])}")


if __name__ == "__main__":
    main()
