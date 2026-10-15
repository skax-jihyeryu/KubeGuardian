"""FastAPI 에러 핸들러. 모든 오류를 같은 응답 형식으로 돌려준다."""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from core.error.service_exception import ErrorCode, ServiceException, error_body
from core.log.logging import get_logging

logger = get_logging()


def set_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ServiceException)
    async def service_exception_handler(request: Request, exc: ServiceException):
        log = logger.error if exc.error_code.http_status >= 500 else logger.warning
        log(f"{exc.error_code.name} path={request.url.path} detail={exc.detail}")
        return JSONResponse(status_code=exc.error_code.http_status, content=exc.to_dict())

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        # 입력값 원문(input)은 비밀값일 수 있어 위치와 종류만 남긴다
        fields = [(".".join(map(str, e.get("loc", ()))), e.get("type")) for e in exc.errors()]
        logger.warning(f"VALIDATION_ERROR path={request.url.path} fields={fields}")
        code = ErrorCode.VALIDATION_ERROR
        return JSONResponse(status_code=code.http_status, content=error_body(code))

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        if exc.status_code >= 500:
            code = ErrorCode.INTERNAL_ERROR
        else:
            by_status = {404: ErrorCode.NOT_FOUND, 405: ErrorCode.METHOD_NOT_ALLOWED}
            code = by_status.get(exc.status_code, ErrorCode.BAD_REQUEST)
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(code),
            headers=getattr(exc, "headers", None),  # 405의 Allow 등
        )

    @app.exception_handler(Exception)
    async def unexpected_exception_handler(request: Request, exc: Exception):
        logger.exception(f"INTERNAL_ERROR path={request.url.path}")
        code = ErrorCode.INTERNAL_ERROR
        return JSONResponse(status_code=code.http_status, content=error_body(code))
