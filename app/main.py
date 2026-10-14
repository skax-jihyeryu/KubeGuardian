"""FastAPI 앱 진입점. 실행: cd app && uv run python main.py"""

import uvicorn
from fastapi import FastAPI

from api.router import api_router
from core.config import get_setting
from core.log.logging import get_logging

settings = get_setting()
logger = get_logging()


def create_app() -> FastAPI:
    app = FastAPI(title=settings.APP_NAME)
    app.include_router(api_router)
    return app


app = create_app()

if __name__ == "__main__":
    logger.info(f"{settings.APP_NAME}[{settings.APP_PORT}] starting")
    uvicorn.run(app, host="0.0.0.0", port=settings.APP_PORT)
