from typing import Optional, List, Sequence
from sqlalchemy import select, func, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.place import Place
from app.models.enums import PlaceCategory
from app.schemas.place import CategoryInfo


CATEGORY_DESCRIPTIONS = {
    PlaceCategory.HISTORY_ARCHITECTURE: "Дворцы, храмы, крепости и памятники зодчества имперской столицы",
    PlaceCategory.ART: "Музеи изобразительного искусства, галереи, театры и мозаичные дворики",
    PlaceCategory.SCIENCE_TECHNOLOGY: "Научные музеи, ледоколы, планетарии и колыбель открытий",
    PlaceCategory.NATURE: "Исторические сады, парки, набережные и оранжереи",
    PlaceCategory.MODERN_SPACES: "Креативные кластеры, арт-заводы, набережные Финского залива и лофты",
    PlaceCategory.FAMILY_LEISURE: "Интерактивные макеты, зоопарк, океанариум и развлечения для всей семьи",
}


def _is_sqlite(db: AsyncSession) -> bool:
    try:
        bind = getattr(db, "bind", None)
        if bind is None:
            bind = db.get_bind()
        return bind.dialect.name == "sqlite" if bind is not None else True
    except Exception:
        return True


def _readable_place_conditions(db: AsyncSession):
    """Reject unusable stored records before strict ORM/response deserialization."""
    conditions = [Place.category.in_(list(PlaceCategory)),
                  Place.latitude.between(-90, 90), Place.longitude.between(-180, 180),
                  Place.recommended_visit_minutes.between(5, 180)]
    if _is_sqlite(db):
        conditions.extend([
            func.typeof(Place.latitude).in_(['integer', 'real']),
            func.typeof(Place.longitude).in_(['integer', 'real']),
            func.typeof(Place.recommended_visit_minutes) == 'integer',
            # SQLite stores whatever was written: a TEXT/BLOB flag must not be
            # read through SQLAlchemy truthiness as unusual=true, and an integer
            # other than 0/1 belongs to neither filter result set.
            func.typeof(Place.is_unusual) == 'integer',
            Place.is_unusual.in_([0, 1]),
        ])
    return conditions


class PlaceService:
    @staticmethod
    async def has_stored_places(db: AsyncSession) -> bool:
        # Read only the primary key so an unsupported enum cannot poison this check.
        return await db.scalar(select(Place.id).limit(1)) is not None

    @staticmethod
    async def get_places(
        db: AsyncSession,
        categories: Optional[List[PlaceCategory]] = None,
        is_unusual: Optional[bool] = None,
        search_query: Optional[str] = None,
        limit: Optional[int] = 100,
        offset: int = 0,
    ) -> Sequence[Place]:
        query = select(Place)
        conditions = _readable_place_conditions(db)

        if categories:
            conditions.append(Place.category.in_(categories))
        
        if is_unusual is not None:
            conditions.append(Place.is_unusual == is_unusual)
            
        if search_query and search_query.strip():
            term = search_query.strip().casefold()
            normalize = func.unicode_casefold if _is_sqlite(db) else func.lower
            conditions.append(
                or_(
                    normalize(Place.title).contains(term, autoescape=True),
                    normalize(Place.short_description).contains(term, autoescape=True),
                    normalize(Place.address).contains(term, autoescape=True),
                    normalize(Place.tags).contains(term, autoescape=True),
                )
            )

        if conditions:
            query = query.where(and_(*conditions))

        query = query.order_by(Place.rating.desc(), Place.id.asc()).offset(offset)
        if limit is not None:
            query = query.limit(limit)
        result = await db.execute(query)
        return result.scalars().all()

    @staticmethod
    async def get_total_count(
        db: AsyncSession,
        categories: Optional[List[PlaceCategory]] = None,
        is_unusual: Optional[bool] = None,
        search_query: Optional[str] = None,
    ) -> int:
        query = select(func.count(Place.id))
        conditions = _readable_place_conditions(db)

        if categories:
            conditions.append(Place.category.in_(categories))
        
        if is_unusual is not None:
            conditions.append(Place.is_unusual == is_unusual)
            
        if search_query and search_query.strip():
            term = search_query.strip().casefold()
            normalize = func.unicode_casefold if _is_sqlite(db) else func.lower
            conditions.append(
                or_(
                    normalize(Place.title).contains(term, autoescape=True),
                    normalize(Place.short_description).contains(term, autoescape=True),
                    normalize(Place.address).contains(term, autoescape=True),
                    normalize(Place.tags).contains(term, autoescape=True),
                )
            )

        if conditions:
            query = query.where(and_(*conditions))

        result = await db.execute(query)
        return result.scalar_one()

    @staticmethod
    async def get_place_by_id(db: AsyncSession, place_id: int) -> Optional[Place]:
        query = select(Place).where(Place.id == place_id, *_readable_place_conditions(db))
        result = await db.execute(query)
        return result.scalar_one_or_none()

    @staticmethod
    async def get_categories_info(db: AsyncSession) -> List[CategoryInfo]:
        categories_info: List[CategoryInfo] = []
        
        for category in PlaceCategory:
            query = select(func.count(Place.id)).where(
                Place.category == category, *_readable_place_conditions(db))
            result = await db.execute(query)
            count = result.scalar_one()
            
            categories_info.append(
                CategoryInfo(
                    id=category,
                    name=PlaceCategory.get_display_name(category),
                    description=CATEGORY_DESCRIPTIONS.get(category, ""),
                    count=count,
                )
            )
            
        return categories_info
