"""OSM tag → surface behaviour classes for the C7.2 v1 puddle model.

Only tags that are actually present on a confirmed way are used. A missing
``surface`` is NEVER treated as asphalt (ТЗ C7.2 §2): the slice stays
``unknown`` and the result says so. ``covered``/``tunnel`` mark a confirmed
shelter, which lowers — but never zeroes — the modelled precipitation input:
a covered passage is not a guarantee of dry ground (runoff, wind-blown rain,
a wet floor can still be there).

The classes below group surface values by how they behave under rain, not by
material taxonomy. The grouping itself is an experimental modelling choice,
documented in PHASE_C7_2_MODEL.md, not a measured property of these materials.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Asphalt-like: sheds water quickly, but keeps a wet film for hours.
SURFACE_SMOOTH = "impervious_smooth"
#: Paving stones, setts, cobbles: joints and dips pond water and dry slowly.
SURFACE_UNEVEN = "impervious_uneven"
#: Compacted/gravel/wood decking: drains, but softens and stays damp.
SURFACE_SEMI = "semi_permeable"
#: Ground, grass, sand: infiltrates fast, turns muddy and slippery.
SURFACE_PERMEABLE = "permeable"

_SURFACE_CLASS = {
    "asphalt": SURFACE_SMOOTH,
    "chipseal": SURFACE_SMOOTH,
    "concrete": SURFACE_SMOOTH,
    "concrete:plates": SURFACE_SMOOTH,
    "metal": SURFACE_SMOOTH,
    "paved": SURFACE_SMOOTH,
    "paving_stones": SURFACE_UNEVEN,
    "stone": SURFACE_UNEVEN,
    "sett": SURFACE_UNEVEN,
    "cobblestone": SURFACE_UNEVEN,
    "unhewn_cobblestone": SURFACE_UNEVEN,
    "brick": SURFACE_UNEVEN,
    "bricks": SURFACE_UNEVEN,
    "compacted": SURFACE_SEMI,
    "fine_gravel": SURFACE_SEMI,
    "gravel": SURFACE_SEMI,
    "pebblestone": SURFACE_SEMI,
    "wood": SURFACE_SEMI,
    "woodchips": SURFACE_SEMI,
    "grass_paver": SURFACE_SEMI,
    "ground": SURFACE_PERMEABLE,
    "dirt": SURFACE_PERMEABLE,
    "earth": SURFACE_PERMEABLE,
    "soil": SURFACE_PERMEABLE,
    "mud": SURFACE_PERMEABLE,
    "sand": SURFACE_PERMEABLE,
    "grass": SURFACE_PERMEABLE,
}

#: ``tunnel`` values that mean "the way runs through a structure". ``culvert``
#: is included because a footpath through a culvert is also sheltered from rain.
_COVER_VALUES = {"yes", "building_passage", "culvert", "avalanche_protector"}


@dataclass(frozen=True)
class SurfaceFacts:
    """Everything the v1 model uses about one confirmed way's surface."""

    surface_class: str | None
    covered: bool
    surface_tag: str | None
    covered_by: str | None  # the tag that confirmed the shelter, for the report


def surface_facts(tags: dict[str, str]) -> SurfaceFacts:
    surface = tags.get("surface")
    surface_class = _SURFACE_CLASS.get(surface) if surface else None
    covered_by = None
    if tags.get("covered") in {"yes", "true", "1"}:
        covered_by = "covered"
    elif tags.get("tunnel") in _COVER_VALUES:
        covered_by = "tunnel"
    return SurfaceFacts(
        surface_class=surface_class,
        covered=covered_by is not None,
        surface_tag=surface,
        covered_by=covered_by,
    )
