from fastapi import APIRouter
from app.api.v1.endpoints import (assistant, environment, places, pollen, puddle,
                                  quiet, routes, search)

api_router = APIRouter()
api_router.include_router(places.router, prefix="/places", tags=["Места Санкт-Петербурга"])
api_router.include_router(routes.router, prefix="/routes", tags=["Пешеходные маршруты"])
api_router.include_router(search.router, prefix="/search", tags=["Поиск адресов и объектов"])
api_router.include_router(environment.router, prefix="/environment", tags=["Окружающая среда"])
api_router.include_router(puddle.router, prefix="/environment", tags=["Окружающая среда"])
api_router.include_router(pollen.router, prefix="/environment", tags=["Окружающая среда"])
api_router.include_router(quiet.router, prefix="/environment", tags=["Окружающая среда"])
api_router.include_router(assistant.router, prefix="/assistant", tags=["ИИ-помощник"])
