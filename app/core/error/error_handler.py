"""FastAPI 에러 핸들러. 모든 오류를 같은 응답 형식으로 돌려준다."""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from core.error.service_exception import ErrorCode, ServiceException, error_body
from core.log.logging import get_logging

logger = get_logging()


def set_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ServiceException)
    async def service_exception_handler(request: Request, exc: ServiceException):
        logger.error(f"{exc.error_code.name} path={request.url.path} detail={exc.detail}")
        return JSONResponse(status_code=exc.error_code.http_status, content=exc.to_dict())

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        logger.warning(f"VALIDATION_ERROR path={request.url.path} errors={exc.errors()}")
        code = ErrorCode.VALIDATION_ERROR
        return JSONResponse(status_code=code.http_status, content=error_body(code))

    @app.exception_handler(Exception)
    async def unexpected_exception_handler(request: Request, exc: Exception):
        logger.exception(f"INTERNAL_ERROR path={request.url.path}")
        code = ErrorCode.INTERNAL_ERROR
        return JSONResponse(status_code=code.http_status, content=error_body(code))
