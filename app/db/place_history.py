"""Phase C6: cached loader for the editorial historical dataset.

Mirrors seed_data.py discipline: the versioned editorial file is read ONCE per
process into a validated, immutable index keyed by the stable ``catalog_key``.
API requests never touch the filesystem and never trigger network access.

Failure policy: a structurally invalid dataset raises
``PlaceHistoryValidationError`` at load time — editorial data ships with the
application, so it must fail fast instead of serving half-parsed history.
A MISSING card is not an error: the catalogue keeps working without history.
"""
import json
import re
from functools import lru_cache
from pathlib import Path

from pydantic import ValidationError

from app.db.seed_data import SPB_SEED_PLACES
from app.schemas.place_history import HistoricalDataset, HistoricalPlaceContent

DATASET_PATH = Path(__file__).with_name("place_history.json")

_CATALOG_KEYS = {place["catalog_key"] for place in SPB_SEED_PLACES}


class PlaceHistoryValidationError(ValueError):
    """The editorial dataset violates one of the documented invariants."""


def _dataset_errors(dataset: HistoricalDataset) -> list[str]:
    """Cross-entry invariants that a single card cannot check alone."""
    errors: list[str] = []
    seen: set[str] = set()
    for entry in dataset.entries:
        key = entry.catalogKey
        if key in seen:
            errors.append(f"duplicate catalogKey {key!r}")
            continue
        seen.add(key)
        if key not in _CATALOG_KEYS:
            errors.append(f"catalogKey {key!r} does not exist in the place catalogue")
        if entry.timeline:
            ids = [event.id for event in entry.timeline]
            if len(set(ids)) != len(ids):
                errors.append(f"{key}: duplicate timeline event ids")
        if entry.facts:
            ids = [fact.id for fact in entry.facts]
            if len(set(ids)) != len(ids):
                errors.append(f"{key}: duplicate fact ids")
    return errors


@lru_cache(maxsize=8)
def _load_index(path: str) -> dict[str, HistoricalPlaceContent]:
    """Validate and index the dataset file. Cached per resolved path."""
    source = Path(path)
    if not source.exists():
        # The historical layer is optional: an absent file is honest emptiness,
        # not a broken catalogue.
        return {}
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlaceHistoryValidationError(
            f"place history dataset {source} is unreadable: {exc}"
        ) from exc
    try:
        dataset = HistoricalDataset.model_validate(raw)
    except ValidationError as exc:
        # Uniform failure policy: every invalid-dataset problem (schema or
        # cross-entry) surfaces as PlaceHistoryValidationError.
        raise PlaceHistoryValidationError(
            f"place history dataset {source} is invalid: {exc}"
        ) from exc
    errors = _dataset_errors(dataset)
    if errors:
        raise PlaceHistoryValidationError(
            "place history dataset is invalid: " + "; ".join(errors)
        )
    return {entry.catalogKey: entry for entry in dataset.entries}


def load_place_history(path: Path | None = None) -> dict[str, HistoricalPlaceContent]:
    """Validated history cards indexed by ``catalog_key`` (empty when absent)."""
    resolved = str(path if path is not None else DATASET_PATH)
    return _load_index(resolved)


def clear_cache() -> None:
    """Drop the cached dataset index (used by tests and the validation script)."""
    _load_index.cache_clear()


def history_for(catalog_key: str | None) -> HistoricalPlaceContent | None:
    """Editorial card for a stable catalog identity, or None. Never invents."""
    if not isinstance(catalog_key, str) or not catalog_key:
        return None
    return load_place_history().get(catalog_key)
