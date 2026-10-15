from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.error.error_handler import set_error_handlers
from core.error.service_exception import ErrorCode, ServiceException


def _client() -> TestClient:
    app = FastAPI()
    set_error_handlers(app)

    @app.get("/busy")
    def busy():
        raise ServiceException(ErrorCode.CHECK_ALREADY_RUNNING)

    @app.get("/boom")
    def boom():
        raise RuntimeError("secret detail must not leak")

    @app.get("/items/{n}")
    def item(n: int):
        return {"n": n}

    return TestClient(app, raise_server_exceptions=False)


def test_service_exception_uses_template_error_format():
    response = _client().get("/busy")

    assert response.status_code == 409
    assert response.json() == {
        "success": False,
        "error": {
            "code": ErrorCode.CHECK_ALREADY_RUNNING.code,
            "name": "CHECK_ALREADY_RUNNING",
            "message": ErrorCode.CHECK_ALREADY_RUNNING.message,
        },
    }


def test_unexpected_error_hides_internal_detail():
    response = _client().get("/boom")

    assert response.status_code == 500
    assert response.json()["error"]["name"] == "INTERNAL_ERROR"
    assert "secret detail" not in response.text


def test_request_validation_error_uses_validation_code():
    response = _client().get("/items/abc")

    assert response.status_code == 400
    assert response.json()["error"]["name"] == "VALIDATION_ERROR"


def test_cli_exit_code_separates_client_and_server_errors():
    assert ServiceException(ErrorCode.VALIDATION_ERROR).exit_code == 2
    assert ServiceException(ErrorCode.COLLECT_FAILED).exit_code == 1


def test_codes_follow_area_scheme():
    for code in ErrorCode:
        first = code.code // 10000
        assert (first == 1) == (code.http_status < 500), code.name
        assert code.code // 1000 % 10 in {0, 1, 2, 3, 4, 5, 6, 9}, code.name


def test_unknown_route_uses_not_found_code():
    response = _client().get("/no-such-path")

    assert response.status_code == 404
    assert response.json()["error"]["name"] == "NOT_FOUND"
