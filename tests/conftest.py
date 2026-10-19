import pytest
import pytest_asyncio
from typing import AsyncGenerator
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.pool import StaticPool
from app.main import app
from app.db.base import Base
from app.db.session import get_db, configure_sqlite_unicode
from app.db.seed_data import SPB_SEED_PLACES
from app.models.place import Place

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

test_engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=StaticPool)
configure_sqlite_unicode(test_engine)
test_session_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture(autouse=True)
async def upstream_ledger_engine(tmp_path, monkeypatch):
    """Exercise enabled quota safeguards without touching the developer's ledger."""
    from app.core import upstream_controls as controls_module

    ledger_engine = create_async_engine(
        f'sqlite+aiosqlite:///{(tmp_path / "quota.sqlite").as_posix()}')
    configure_sqlite_unicode(ledger_engine)
    await controls_module.initialize_quota_schema(ledger_engine)
    monkeypatch.setattr(controls_module, '_get_engine', lambda: ledger_engine)
    try:
        yield ledger_engine
    finally:
        await ledger_engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with test_session_factory() as session:
        for item in SPB_SEED_PLACES:
            place = Place(
                catalog_key=item["catalog_key"],
                title=item["title"],
                short_description=item["short_description"],
                full_description=item["full_description"],
                category=item["category"],
                is_unusual=item["is_unusual"],
                latitude=item["latitude"],
                longitude=item["longitude"],
                address=item["address"],
                photo_url=item.get("photo_url"),
                working_hours=item.get("working_hours"),
                tags=item.get("tags"),
                rating=item.get("rating"),
                architectural_style=item.get("architectural_style"),
                year_built=item.get("year_built"),
                recommended_visit_minutes=item.get("recommended_visit_minutes", 30),
            )
            session.add(place)
        await session.commit()

    async with test_session_factory() as session:
        yield session

    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await test_engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    # Each test gets fresh middleware state, with the real limits enabled.
    app.middleware_stack = None
    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()
