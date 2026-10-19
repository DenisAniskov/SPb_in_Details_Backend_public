from typing import AsyncGenerator
import logging
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import select, event, text, inspect, update, delete
from app.core.config import settings
from app.db.base import Base
from app.models.place import Place
from app.models.enums import PlaceCategory
from app.db.seed_data import SPB_SEED_PLACES, same_coordinates

# Startup repairs of a local development database are rare but silent data
# decisions; every removal/normalization is logged with enough detail to
# audit it (row id, catalog_key, reason). Titles are deliberately not logged:
# hostile rows hold untrusted free text, and id + key identify the row.
repair_logger = logging.getLogger('app.db.startup_repair')

engine = create_async_engine(settings.DATABASE_URL, echo=False)


def configure_sqlite_unicode(db_engine):
    """SQLite hardening: lower()/LIKE Cyrillic case-folding, foreign keys, busy timeout, WAL mode."""
    if db_engine.dialect.name == 'sqlite':
        @event.listens_for(db_engine.sync_engine, 'connect')
        def register_casefold_and_pragmas(connection, _record):
            connection.create_function(
                'unicode_casefold', 1,
                lambda value: value.casefold() if isinstance(value, str) else None,
                deterministic=True,
            )
            cursor = connection.cursor()
            try:
                cursor.execute('PRAGMA foreign_keys = ON;')
                cursor.execute('PRAGMA busy_timeout = 5000;')
                cursor.execute('PRAGMA journal_mode = WAL;')
                cursor.execute('PRAGMA synchronous = NORMAL;')
            finally:
                cursor.close()


configure_sqlite_unicode(engine)
async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """Sync known starter records while preserving IDs and unrelated places."""
    fields = ('catalog_key', 'title', 'short_description', 'full_description', 'category',
              'is_unusual', 'latitude', 'longitude', 'address', 'photo_url',
              'working_hours', 'tags', 'rating', 'architectural_style', 'year_built',
              'recommended_visit_minutes')
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # create_all never alters an existing table. Migrate before ORM SELECT.
        columns = await conn.run_sync(lambda sync: {
            c['name'] for c in inspect(sync).get_columns('places')
        })
        if 'catalog_key' not in columns:
            await conn.execute(text('ALTER TABLE places ADD COLUMN catalog_key VARCHAR(80)'))
        catalog_index = next(index for index in Place.__table__.indexes
                             if index.name == 'ix_places_catalog_key')
        await conn.run_sync(lambda sync: catalog_index.create(sync, checkfirst=True))
        if conn.dialect.name == 'sqlite':
            if 'recommended_visit_minutes' not in columns:
                await conn.execute(text(
                    'ALTER TABLE places ADD COLUMN recommended_visit_minutes INTEGER '
                    'NOT NULL DEFAULT 30 CHECK(recommended_visit_minutes BETWEEN 5 AND 180)'
                ))
            await conn.execute(text(
                'UPDATE places SET recommended_visit_minutes=30 '
                'WHERE recommended_visit_minutes IS NULL '
                'OR typeof(recommended_visit_minutes) != \'integer\' '
                'OR recommended_visit_minutes NOT BETWEEN 5 AND 180'
            ))
        # Repair keyed canonical rows BEFORE reading typed ORM values or deleting
        # hostile coordinates. Stable identity lets us restore trusted content in
        # place, preserving IDs even when category/coordinates have been corrupted.
        for item in SPB_SEED_PLACES:
            await conn.execute(update(Place).where(
                Place.catalog_key == item['catalog_key']).values(
                    {field: item.get(field) for field in fields}))
        # A keyed row claiming an identity the current catalogue does not contain
        # is incoherent: keeping it would leave a stale duplicate next to the
        # canonical row the sync below re-creates (e.g. after an accidental seed
        # key rename). Remove it and say so; user rows remain unkeyed.
        known_keys = [item['catalog_key'] for item in SPB_SEED_PLACES]
        orphan_rows = (await conn.execute(
            select(Place.id, Place.catalog_key).where(
                Place.catalog_key.is_not(None), Place.catalog_key.not_in(known_keys))
        )).all()
        if orphan_rows:
            await conn.execute(delete(Place).where(
                Place.catalog_key.is_not(None), Place.catalog_key.not_in(known_keys)))
            for row in orphan_rows:
                repair_logger.warning(
                    'Startup repair removed row id=%s catalog_key=%s (reason=orphan_catalog_identity)',
                    row.id, row.catalog_key)
        if conn.dialect.name == 'sqlite':
            # The documented API category form is the enum value; SQLAlchemy
            # persists the enum name. A row written externally with the value
            # form is unambiguous and safely normalized instead of deleted.
            normalized = 0
            for member in PlaceCategory:
                result = await conn.execute(text(
                    'UPDATE places SET category = :name WHERE category = :value'),
                    {'name': member.name, 'value': member.value})
                normalized += result.rowcount
            if normalized:
                repair_logger.warning(
                    'Startup repair normalized %s row(s) from documented enum value form '
                    'to storage name form (reason=enum_value_form)', normalized)
            # Hostile boolean storage: repair to the documented default instead of
            # letting SQLAlchemy truthiness read arbitrary text/blob as unusual=true.
            repaired_flags = (await conn.execute(text(
                "UPDATE places SET is_unusual = 0 "
                "WHERE typeof(is_unusual) != 'integer' OR is_unusual NOT IN (0, 1)")
            )).rowcount
            if repaired_flags:
                repair_logger.warning(
                    'Startup repair normalized %s row(s) with hostile is_unusual storage '
                    'to false (reason=hostile_boolean_storage)', repaired_flags)
            # Invalid unkeyed spatial rows cannot be identified as canonical from
            # title alone; remove them rather than invent coordinates or identity.
            bad_spatial = (await conn.execute(text(
                'SELECT id, catalog_key FROM places '
                "WHERE typeof(latitude) NOT IN ('integer', 'real') "
                "OR typeof(longitude) NOT IN ('integer', 'real') "
                'OR latitude NOT BETWEEN -90 AND 90 '
                'OR longitude NOT BETWEEN -180 AND 180'
            ))).all()
            if bad_spatial:
                await conn.execute(text(
                    'DELETE FROM places '
                    "WHERE typeof(latitude) NOT IN ('integer', 'real') "
                    "OR typeof(longitude) NOT IN ('integer', 'real') "
                    'OR latitude NOT BETWEEN -90 AND 90 '
                    'OR longitude NOT BETWEEN -180 AND 180'
                ))
                for row in bad_spatial:
                    repair_logger.warning(
                        'Startup repair removed row id=%s catalog_key=%s (reason=invalid_coordinates)',
                        row.id, row.catalog_key)
        # An unsupported category must never be relabeled as a real category, or
        # reach SQLAlchemy's strict Enum result loader and abort the whole startup.
        # Runs after value-form normalization so only genuinely unknown storage
        # is removed.
        bad_category = (await conn.execute(
            select(Place.id, Place.catalog_key).where(Place.category.not_in(list(PlaceCategory)))
        )).all()
        if bad_category:
            await conn.execute(delete(Place).where(Place.category.not_in(list(PlaceCategory))))
            for row in bad_category:
                repair_logger.warning(
                    'Startup repair removed row id=%s catalog_key=%s (reason=unsupported_category)',
                    row.id, row.catalog_key)
    async with async_session_factory() as session:
        rows = (await session.scalars(select(Place).order_by(Place.id))).all()
        existing = {place.catalog_key: place for place in rows if place.catalog_key}
        for item in SPB_SEED_PLACES:
            place = existing.get(item['catalog_key'])
            if place is None:
                # One-time adoption of the old catalog, before stable keys existed.
                # Match a known title AND catalog location; never claim arbitrary
                # same-title rows or nearby objects as reviewed tourist content.
                titles = {item['title'], *item.get('legacy_titles', [])}
                coordinates = [(item['latitude'], item['longitude']),
                               *item.get('legacy_coordinates', [])]
                matches = [row for row in rows if not row.catalog_key
                           and row.title in titles
                           and any(same_coordinates(row.latitude, row.longitude, *coord)
                                   for coord in coordinates)]
                if len(matches) == 1:
                    place = matches[0]
            if place is None:
                place = Place()
                session.add(place)
            for field in fields:
                setattr(place, field, item.get(field))
        await session.commit()
    from app.core.upstream_controls import initialize_quota_schema
    from app.services.assistant.quota import initialize_assistant_schema
    await initialize_quota_schema(engine)
    await initialize_assistant_schema(engine)


async def check_database_ready() -> None:
    """Verify mapped schema, catalog initialization and quota schema, locally."""
    from sqlalchemy import func
    async with engine.connect() as conn:
        await conn.execute(select(Place.__table__).limit(1))
        count = await conn.scalar(select(func.count()).select_from(Place).where(
            Place.catalog_key.in_([item["catalog_key"] for item in SPB_SEED_PLACES])))
        if count != len(SPB_SEED_PLACES):
            raise RuntimeError("Catalog is not initialized")
        await conn.execute(text(
            "SELECT id, product, reserved_at FROM upstream_quota_ledger LIMIT 0"))
