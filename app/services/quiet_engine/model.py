"""Transparent engineering constants for the C11 quiet-road proxy.

These are PROJECT ASSUMPTIONS, not measured acoustics and not coefficients
validated for Saint Petersburg. They convert the distance from a pedestrian
geometry to mapped motor roads into a bounded, dimensionless exposure proxy.
The model never returns dB, dB(A), a health risk or a guarantee of silence.
"""
from __future__ import annotations

MODEL_VERSION = "c11_1"

#: Beyond this distance a mapped road contributes no exposure. 120 m is a
#: planning assumption for a city-scale proxy, not an acoustic attenuation law.
MAX_INFLUENCE_METERS = 120.0

#: Route geometry is split by LENGTH (not by vertex count) before nearest-road
#: queries. This makes the result stable when MapKit densifies the same line.
SAMPLE_STEP_METERS = 10.0

#: Relative thresholds used only for interpretable shares, never as safety
#: categories: below 0.20 is described as "away from major roads", at or above
#: 0.50 as "adjacent to a major road".
LOW_EXPOSURE_THRESHOLD = 0.20
MAJOR_ADJACENT_THRESHOLD = 0.50

#: Functional-class weights. Higher class = more motor traffic assumed.
#: Links inherit their parent class. ``living_street`` is deliberately low:
#: it is a shared pedestrian/vehicle space, not a quiet guarantee.
ROAD_CLASS_WEIGHTS: dict[str, float] = {
    "motorway": 1.00,
    "trunk": 1.00,
    "primary": 0.85,
    "secondary": 0.70,
    "tertiary": 0.55,
    "residential": 0.30,
    "unclassified": 0.25,
    "living_street": 0.15,
    "motorway_link": 1.00,
    "trunk_link": 1.00,
    "primary_link": 0.85,
    "secondary_link": 0.70,
    "tertiary_link": 0.55,
}

#: Ways whose own access rules exclude ordinary motor traffic do not act as
#: noise sources. ``destination`` stays eligible: local traffic still exists.
EXCLUDED_ACCESS = frozenset({"no", "private", "customers", "permit"})

#: A tunnel's portal/ventilation noise is still real, but the surface line
#: above a tunnel is not evidence of traffic noise at street level.
TUNNEL_FACTOR = 0.15
BUILDING_PASSAGE_FACTOR = 0.10

#: Fixed Russian-language limitations copied into every estimate. The API
#: returns these verbatim so a client cannot silently drop them.
LIMITATIONS = (
    "Относительный индекс основан на расстоянии до автомобильных дорог OSM, "
    "а не на акустических измерениях.",
    "Значения не являются уровнем шума в дБ или дБ(А) и не подтверждают "
    "санитарные нормативы.",
    "Модель статична: интенсивность движения по часам суток не моделируется.",
    "Полнота и актуальность OSM различаются по улицам; отсутствие дороги "
    "в данных не доказывает отсутствие транспорта.",
    "Классы дорог и веса — проектные допущения, а не измеренные коэффициенты.",
)

ASSUMPTIONS = (
    "Пешеходный маршрут сравнивается с ближайшими автомобильными дорогами "
    "в пределах 120 м.",
    "Вес класса дороги умножается на квадратичное затухание по расстоянию.",
    "Покрытие означает долю длины маршрута внутри проверенного пилота OSM; "
    "непокрытая часть остаётся неизвестной, а не нулевой.",
    "Ноль индекса означает отсутствие значимой дороги в пределах 120 м "
    "внутри пилота, а не гарантию тишины.",
)
