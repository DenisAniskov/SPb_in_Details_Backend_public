"""Experimental precipitation → wet-surface index for C7.2 v1.

WHAT THIS IS: a deterministic, meter-weighted *relative* wetness proxy over
the hours before a slice of the walk is crossed. WHAT THIS IS NOT: a
probability that a puddle exists, a water depth, a drying-time prediction or
a guarantee of dry feet.

The index is deliberately tiny and explainable:

    effective(t) = current_rain(t)
                 + Σ past_bins  precipitation_mm · exp(−(t − bin_end)/τ(class))
    index(t)     = min(1, effective(t) · covered_factor / saturation(class))

with τ (drying memory) and saturation (the amount that saturates the surface)
per surface class. The per-class numbers and the low/moderate/high thresholds
below are EXPERIMENTAL defaults: they are not calibrated on field
observations (ТЗ C7.2 §3 explicitly forbids presenting such values as
scientifically proven). Every analysis therefore reports a *sensitivity*
block showing how the answer moves when the parameters move, and the report
labels the model uncalibrated until field evidence exists.

A covered slice multiplies the precipitation input by ``covered_factor``
(0 < factor < 1): a roofed passage removes most rain but not runoff, wind and
a wet floor, so it can lower — never zero — the index.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

from app.services.puddle_engine.surfaces import (
    SURFACE_PERMEABLE,
    SURFACE_SEMI,
    SURFACE_SMOOTH,
    SURFACE_UNEVEN,
)

#: Hours of rain memory per surface class (experimental, uncalibrated).
DRYING_HOURS: dict[str, float] = {
    SURFACE_SMOOTH: 3.0,
    SURFACE_UNEVEN: 6.0,   # joints and dips hold water for longer
    SURFACE_SEMI: 2.0,
    SURFACE_PERMEABLE: 1.0,
}

#: Millimetres that saturate a surface class (experimental, uncalibrated).
SATURATION_MM: dict[str, float] = {
    SURFACE_SMOOTH: 3.0,
    SURFACE_UNEVEN: 2.0,   # ponding-prone: saturates sooner
    SURFACE_SEMI: 4.0,
    SURFACE_PERMEABLE: 6.0,
}

#: How much of the falling rain reaches the ground of a covered slice.
COVERED_FACTOR = 0.35

#: Classification thresholds on the [0, 1] index (experimental, uncalibrated).
LOW_MAX = 0.2
HIGH_MIN = 0.5


@dataclass(frozen=True)
class PuddleModelConfig:
    """Parameters that the sensitivity block perturbs (nothing else varies)."""

    drying_scale: float = 1.0
    saturation_scale: float = 1.0
    covered_factor: float = COVERED_FACTOR
    low_max: float = LOW_MAX
    high_min: float = HIGH_MIN

    def variant(self, **changes: float) -> "PuddleModelConfig":
        return replace(self, **changes)


def wetness_index(
    surface_class: str,
    *,
    history: list[tuple[float, float]],
    current_mm: float,
    covered: bool,
    config: PuddleModelConfig,
) -> float:
    """Relative wetness index in [0, 1].

    ``history`` is a list of ``(precipitation_mm, age_hours)`` for hourly bins
    that fully ended before the evaluation instant; ``current_mm`` is rain of
    the bin that is falling right now (0 when none). Both are model inputs,
    never field measurements.
    """
    drying = DRYING_HOURS[surface_class] * config.drying_scale
    saturation = SATURATION_MM[surface_class] * config.saturation_scale
    effective = current_mm
    for precipitation_mm, age_hours in history:
        if age_hours <= 0:
            continue
        effective += precipitation_mm * math.exp(-age_hours / drying)
    if covered:
        effective *= config.covered_factor
    return min(1.0, effective / saturation)


def classify(index: float, config: PuddleModelConfig) -> str:
    if index < config.low_max:
        return "low"
    if index >= config.high_min:
        return "high"
    return "moderate"


#: Sensitivity axes reported with every analysis. Scaling drying by 0.5/2.0
#: and saturation by 0.75 shows how much of the answer is parameter choice
#: rather than data — the honest way to present an uncalibrated model.
SENSITIVITY_VARIANTS: tuple[tuple[str, dict[str, float]], ...] = (
    ("drying_faster_0_5x", {"drying_scale": 0.5}),
    ("drying_slower_2x", {"drying_scale": 2.0}),
    ("ponding_sooner_saturation_0_75x", {"saturation_scale": 0.75}),
)
