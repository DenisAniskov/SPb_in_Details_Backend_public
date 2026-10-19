"""Real file SQLite startup/restart and local-only readiness checks."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.db.seed_data import SPB_SEED_PLACES
from app.models.place import Place


@pytest_asyncio.fixture
async def deployment(tmp_path, monkeypatch):
    import app.db.session as db
    import app.main as main
    engine = create_async_engine(f'sqlite+aiosqlite:///{(tmp_path / "persistent.db").as_posix()}')
    db.configure_sqlite_unicode(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "async_session_factory", factory)
    monkeypatch.setattr(main, "engine", engine)
    close = AsyncMock()
    monkeypatch.setattr(main, "yandex_search_service", SimpleNamespace(close=close))
    monkeypatch.setattr(main.settings, "SUGGEST_API_KEY", None)
    monkeypatch.setattr(main.settings, "GEOCODER_API_KEY", None)
    main.app.state.initialized = False
    main.app.middleware_stack = None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                base_url="http://test") as client:
        yield SimpleNamespace(db=db, main=main, engine=engine, factory=factory, client=client, close=close)
    await engine.dispose()
    main.app.state.initialized = False


async def test_health_running_and_readiness_before_during_after_startup(deployment):
    env = deployment
    assert (await env.client.get("/health")).status_code == 200
    assert (await env.client.get("/ready")).status_code == 503
    async with env.main.lifespan(env.main.app):
        response = await env.client.get("/ready")
        assert response.status_code == 200 and response.json() == {"status": "ready"}
        assert (await env.client.get("/api/v1/places")).json()["total"] == len(SPB_SEED_PLACES)
    assert (await env.client.get("/ready")).json() == {"status": "not_ready"}
    env.close.assert_awaited_once()


async def test_file_database_restart_keeps_ids_keys_and_quota_history(deployment):
    env = deployment
    async with env.main.lifespan(env.main.app):
        async with env.engine.begin() as connection:
            before = dict((await connection.execute(text("SELECT catalog_key,id FROM places"))).all())
            await connection.execute(text(
                "INSERT INTO upstream_quota_ledger (product,reserved_at) VALUES ('suggest',100000)"))
        async with env.factory() as session:
            session.add(Place(title="User editorial place", short_description="Text",
                              full_description="Text", category=SPB_SEED_PLACES[0]["category"],
                              latitude=59.9, longitude=30.3, address="SPb",
                              recommended_visit_minutes=20, is_unusual=False))
            await session.commit()
    # Reconnect after disposal as a restarted process would.
    async with env.main.lifespan(env.main.app):
        async with env.engine.connect() as connection:
            after = dict((await connection.execute(text(
                "SELECT catalog_key,id FROM places WHERE catalog_key IS NOT NULL"))).all())
            assert before == after and len(after) == len(SPB_SEED_PLACES)
            assert await connection.scalar(text("SELECT COUNT(*) FROM places")) == len(SPB_SEED_PLACES) + 1
            assert await connection.scalar(text("SELECT COUNT(*) FROM upstream_quota_ledger")) == 1
        assert (await env.client.get("/ready")).status_code == 200


@pytest.mark.parametrize("failure", ["catalog", "places_schema", "quota_schema"])
async def test_readiness_rejects_missing_local_dependency_safely(deployment, failure, caplog):
    env = deployment
    async with env.main.lifespan(env.main.app):
        async with env.engine.begin() as connection:
            if failure == "catalog":
                await connection.execute(text("DELETE FROM places WHERE id=(SELECT MIN(id) FROM places)"))
            elif failure == "places_schema":
                await connection.execute(text("DROP TABLE places"))
            else:
                await connection.execute(text("DROP TABLE upstream_quota_ledger"))
        response = await env.client.get("/ready")
        assert response.status_code == 503 and response.json() == {"status": "not_ready"}
        assert "persistent.db" not in response.text and "persistent.db" not in caplog.text


async def test_readiness_timeout_without_upstream_request(deployment, monkeypatch):
    import asyncio
    env = deployment
    async def delayed():
        await asyncio.Event().wait()
    async with env.main.lifespan(env.main.app):
        monkeypatch.setattr(env.db, "check_database_ready", delayed)
        monkeypatch.setattr(env.main.settings, "READINESS_TIMEOUT_SECONDS", 0.01)
        assert (await env.client.get("/ready")).status_code == 503


async def test_startup_failure_cleans_both_dependencies(deployment, monkeypatch):
    env = deployment
    dispose = AsyncMock()
    monkeypatch.setattr(env.main, "engine", SimpleNamespace(dispose=dispose))
    monkeypatch.setattr(env.main, "init_db", AsyncMock(side_effect=RuntimeError("startup test")))
    with pytest.raises(RuntimeError, match="startup test"):
        async with env.main.lifespan(env.main.app):
            pytest.fail("startup failure must not yield a serving application")
    env.close.assert_awaited_once()
    dispose.assert_awaited_once()
    assert env.main.app.state.initialized is False


async def test_shutdown_disposes_engine_when_search_cleanup_fails(deployment, monkeypatch):
    env = deployment
    dispose = AsyncMock()
    monkeypatch.setattr(env.main, "engine", SimpleNamespace(dispose=dispose))
    monkeypatch.setattr(env.close, "side_effect", RuntimeError("close failure"))
    with pytest.raises(RuntimeError, match="close failure"):
        async with env.main.lifespan(env.main.app):
            assert env.main.app.state.initialized is True
    dispose.assert_awaited_once()
    assert env.main.app.state.initialized is False
