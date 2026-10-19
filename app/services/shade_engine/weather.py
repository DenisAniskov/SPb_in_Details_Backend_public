"""Weather influence interface — Phase 7 of the Shade Engine foundation.

DESIGN RULE (enforced by a dedicated test):

    Cloud cover NEVER changes the computed shadow geometry. Shadows are a
    function of solar position and building geometry only. Cloud cover
    changes how RELEVANT/USEFUL a shade estimate is to the user:

        clear sky      → a shaded path is pleasantly cool AND the exposed
                         path is hot; the shade distinction matters most.
        overcast       → direct sun barely exists; the shade fraction is
                         technically computable but of little practical
                         value.

    Solar geometry and weather context therefore live in separate modules.
    v1 defines ONLY the interface + relevance classification; no weather
    fetching service is implemented (no invented data).
"""

from dataclasses import dataclass
from enum import Enum


class ShadeRelevance(str, Enum):
    HIGH = "high"
    MODERATE = "moderate"
    LOW = "low"
    UNKNOWN = "unknown"          # cloud cover not provided
    NOT_APPLICABLE = "not_applicable"  # sun below horizon: no shade distinction


@dataclass(frozen=True)
class WeatherContext:
    """Cloud-cover context attached by the caller (future weather service).

    cloud_cover_fraction — 0..1 (None = unknown). This is a MEANING/CONTEXT
        input only; the shadow model must not (and does not) read it.
    source — provenance string, e.g. "open-meteo forecast" (future).
    """

    cloud_cover_fraction: float | None = None
    source: str | None = None

    def __post_init__(self) -> None:
        c = self.cloud_cover_fraction
        if c is not None and (not isinstance(c, (int, float)) or not 0.0 <= float(c) <= 1.0):
            raise ValueError("cloud_cover_fraction must be within [0, 1] or None")


def shade_estimate_relevance(
    weather: WeatherContext | None,
    sun_above_horizon: bool,
) -> tuple[ShadeRelevance, str]:
    """How practically relevant is a shade estimate under this weather?

    Returns (relevance, human-readable explanation). Thresholds are
    documented product decisions, not measurements: ≤0.2 clear, ≤0.6 partly
    cloudy, >0.6 overcast.
    """
    if not sun_above_horizon:
        return (ShadeRelevance.NOT_APPLICABLE,
                "Солнце ниже горизонта — различие тень/солнце не применимо.")
    if weather is None or weather.cloud_cover_fraction is None:
        return (ShadeRelevance.UNKNOWN,
                "Облачность неизвестна; оценка тени не уточнена по погоде.")
    c = float(weather.cloud_cover_fraction)
    if c <= 0.2:
        return (ShadeRelevance.HIGH,
                f"Малооблачно ({c:.0%}): оценка тени максимально полезна.")
    if c <= 0.6:
        return (ShadeRelevance.MODERATE,
                f"Переменная облачность ({c:.0%}): оценка тени умеренно полезна.")
    return (ShadeRelevance.LOW,
            f"Пасмурно ({c:.0%}): различие тень/солнце малозначимо, оценка носит справочный характер.")
