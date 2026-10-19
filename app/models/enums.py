from enum import Enum


class PlaceCategory(str, Enum):
    HISTORY_ARCHITECTURE = "history_architecture"  # История и архитектура
    ART = "art"                                     # Искусство
    SCIENCE_TECHNOLOGY = "science_technology"       # Наука и техника
    NATURE = "nature"                               # Природа
    MODERN_SPACES = "modern_spaces"                 # Современные пространства
    FAMILY_LEISURE = "family_leisure"               # Семейный отдых

    @classmethod
    def get_display_name(cls, category: "PlaceCategory") -> str:
        names = {
            cls.HISTORY_ARCHITECTURE: "История и архитектура",
            cls.ART: "Искусство",
            cls.SCIENCE_TECHNOLOGY: "Наука и техника",
            cls.NATURE: "Природа",
            cls.MODERN_SPACES: "Современные пространства",
            cls.FAMILY_LEISURE: "Семейный отдых",
        }
        return names.get(category, category.value)
