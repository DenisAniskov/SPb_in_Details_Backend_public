"""Single starter catalogue. OSM provenance: docs/PLACES_AUDIT.md."""
import json
import math
import re
from pathlib import Path
from app.models.enums import PlaceCategory

SPB_SEED_PLACES = json.loads(Path(__file__).with_name("seed_places.json").read_text(encoding="utf-8"))
for place in SPB_SEED_PLACES:
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,79}", place.get("catalog_key", "")):
        raise ValueError('Invalid stable identity in seed catalogue')
    place["category"] = PlaceCategory(place["category"])
    minutes = place.get('recommended_visit_minutes')
    if type(minutes) is not int or not 5 <= minutes <= 180:
        raise ValueError('Invalid editorial visit estimate in seed catalogue')

_CATALOG_INDEX = {p["catalog_key"]: p for p in SPB_SEED_PLACES}
if len(_CATALOG_INDEX) != len(SPB_SEED_PLACES):
    raise ValueError('Duplicate stable identity in seed catalogue')


def seed_metadata_for(catalog_key: str | None) -> dict:
    """Editorial content belongs to a stable identity, never a display title."""
    return _CATALOG_INDEX.get(catalog_key, {})


def same_coordinates(lat: float, lon: float, expected_lat: float, expected_lon: float) -> bool:
    return (math.isclose(lat, expected_lat, rel_tol=0, abs_tol=1e-7)
            and math.isclose(lon, expected_lon, rel_tol=0, abs_tol=1e-7))
