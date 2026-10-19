from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import settings
from app.api.v1.api import api_router
from app.db.session import init_db, engine
from app.services.yandex_search_service import yandex_search_service
from app.services.assistant.backup import (
    AssistantBackupError,
    maybe_backup_before_migration,
)
from app.api.v1.endpoints.assistant import close_assistant_service
from app.core.request_limits import RequestLimitsMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.initialized = False
    try:
        # C13: create the verified online backup BEFORE init_db can add the
        # assistant tables. A failure aborts startup, leaving the previous
        # Railway deployment serving the existing database.
        try:
            maybe_backup_before_migration()
        except AssistantBackupError:
            logger.error("C13 pre-migration backup failed; startup aborted")
            raise
        await init_db()
        app.state.initialized = True
        yield
    finally:
        app.state.initialized = False
        try:
            await close_assistant_service()
        finally:
            try:
                await yandex_search_service.close()
            finally:
                await engine.dispose()


app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    description="REST API для мобильного адаптивного путеводителя по Санкт-Петербургу «SmartNavigator SPb»",
    lifespan=lifespan,
    docs_url="/docs" if settings.BACKEND_ENV == "development" else None,
    redoc_url="/redoc" if settings.BACKEND_ENV == "development" else None,
    openapi_url="/openapi.json" if settings.BACKEND_ENV == "development" else None,
)
app.state.initialized = False
app.add_middleware(RequestLimitsMiddleware)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

import asyncio
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
import logging

logger = logging.getLogger("smart_navigator.api")
from app.core.logging_safety import SafeServerErrors
logging.getLogger("uvicorn.error").addFilter(SafeServerErrors())

app.include_router(api_router, prefix=settings.API_V1_STR)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_request, exc: RequestValidationError):
    # Pydantic input/context may contain GPS, credentials or attacker text.
    errors = [{"type": e["type"], "loc": e["loc"],
               "msg": "Некорректное значение параметра."} for e in exc.errors()]
    return JSONResponse(
        status_code=422,
        content={"detail": errors},
    )


@app.exception_handler(SQLAlchemyError)
async def sqlalchemy_exception_handler(_request, exc: SQLAlchemyError):
    logger.error("Database operational error (%s)", type(exc).__name__)
    return JSONResponse(
        status_code=503,
        content={"detail": "База данных временно недоступна. Попробуйте позже."},
    )


@app.exception_handler(Exception)
async def unexpected_exception_handler(_request, exc):
    logger.error("Unexpected application error (%s)", type(exc).__name__)
    return JSONResponse(status_code=500, content={"detail": "Внутренняя ошибка сервиса."})


@app.get("/health", tags=["Система"], summary="Проверка работоспособности")
async def health_check():
    return {
        "status": "ok",
        "service": settings.PROJECT_NAME,
        "version": settings.VERSION,
    }


@app.get("/ready", tags=["Система"], summary="Готовность локальных зависимостей")
async def readiness_check():
    from app.db.session import check_database_ready
    if app.state.initialized:
        try:
            await asyncio.wait_for(check_database_ready(), settings.READINESS_TIMEOUT_SECONDS)
            return {"status": "ready"}
        except (SQLAlchemyError, asyncio.TimeoutError, RuntimeError, OSError) as exc:
            logger.warning("Readiness check failed (%s)", type(exc).__name__)
    return JSONResponse(status_code=503, content={"status": "not_ready"})


@app.get("/", tags=["Система"], summary="Корень API")
async def root():
    return {
        "message": "Добро пожаловать в SmartNavigator SPb API",
        "docs": app.docs_url,
        "health": "/health",
        "ready": "/ready",
        "api_v1": settings.API_V1_STR,
    }
