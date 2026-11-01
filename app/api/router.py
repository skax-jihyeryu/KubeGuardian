"""/api/v1 라우터 묶음."""

from fastapi import APIRouter

from api.routes import chat, health

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(chat.router)
