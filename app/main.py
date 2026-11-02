"""FastAPI 앱 진입점. 실행: cd app && uv run python main.py"""

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from api.router import api_router
from api.routes import checks, pages
from api.web import STATIC_DIR
from core.config import get_setting
from core.error.error_handler import set_error_handlers
from core.log.logging import get_logging

settings = get_setting()
logger = get_logging()


def create_app() -> FastAPI:
    app = FastAPI(title=settings.APP_NAME)
    app.include_router(api_router)
    app.include_router(pages.router)  # 보고서 화면 (HTML, /api/v1 아래가 아님)
    app.include_router(checks.router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    set_error_handlers(app)
    return app


app = create_app()

if __name__ == "__main__":
    logger.info(f"{settings.APP_NAME}[{settings.APP_PORT}] starting")
    uvicorn.run(app, host="0.0.0.0", port=settings.APP_PORT)
