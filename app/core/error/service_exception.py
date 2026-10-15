"""KubeGuardian 자체의 오류 코드 (템플릿 backend-agent 방식).

코드 체계: 첫 자리 1 = 클라이언트 오류(4XX), 9 = 서버 오류(5XX).
둘째 자리 = 영역: 0 공통 · 1 수집 · 2 점검 · 3 에이전트·LLM · 4 가드레일
               · 5 알림 · 6 저장소 · 9 시스템.
API 응답, CLI 종료 코드, Slack 오류 메시지(M8)에 같은 코드를 쓴다.
진단 규칙(S04 등)은 오류가 아니라 점검 결과이므로 여기에 두지 않는다.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any

from fastapi import status


@dataclass(frozen=True)
class ErrorInfo:
    http_status: int
    code: int
    message: str


class ErrorCode(Enum):
    # 공통 (클라이언트)
    VALIDATION_ERROR = ErrorInfo(status.HTTP_400_BAD_REQUEST, 10001, "입력값 검증에 실패했습니다")
    NOT_FOUND = ErrorInfo(status.HTTP_404_NOT_FOUND, 10004, "대상을 찾을 수 없습니다")
    # 점검 (클라이언트): 동시 1건, 1분 간격 (UI 정의서 §6.5)
    CHECK_ALREADY_RUNNING = ErrorInfo(
        status.HTTP_409_CONFLICT, 12001, "이미 실행 중인 점검이 있습니다"
    )
    CHECK_TOO_FREQUENT = ErrorInfo(
        status.HTTP_429_TOO_MANY_REQUESTS, 12002, "점검은 1분에 한 번만 실행할 수 있습니다"
    )
    # 서버
    COLLECT_FAILED = ErrorInfo(
        status.HTTP_502_BAD_GATEWAY, 91001, "Kubernetes API에서 수집하지 못했습니다"
    )
    LLM_CALL_FAILED = ErrorInfo(status.HTTP_502_BAD_GATEWAY, 93001, "LLM 호출에 실패했습니다")
    NOTIFY_FAILED = ErrorInfo(status.HTTP_502_BAD_GATEWAY, 95001, "Slack 발송에 실패했습니다")
    STORAGE_ERROR = ErrorInfo(
        status.HTTP_500_INTERNAL_SERVER_ERROR, 96001, "저장소 오류가 발생했습니다"
    )
    INTERNAL_ERROR = ErrorInfo(
        status.HTTP_500_INTERNAL_SERVER_ERROR, 99999, "내부 오류가 발생했습니다"
    )

    @property
    def http_status(self) -> int:
        return self.value.http_status

    @property
    def code(self) -> int:
        return self.value.code

    @property
    def message(self) -> str:
        return self.value.message


class ServiceException(Exception):
    def __init__(self, error_code: ErrorCode, detail: str | None = None):
        self.error_code = error_code
        self.detail = detail  # 로그용. 응답에는 넣지 않는다
        super().__init__(f"{error_code.name}: {detail or error_code.message}")

    @property
    def exit_code(self) -> int:
        """CLI 종료 코드: 클라이언트 오류 2, 서버 오류 1. 코드 번호는 출력으로 알린다."""
        return 2 if self.error_code.http_status < 500 else 1

    def to_dict(self) -> dict[str, Any]:
        return error_body(self.error_code)


def error_body(error_code: ErrorCode) -> dict[str, Any]:
    return {
        "success": False,
        "error": {"code": error_code.code, "name": error_code.name, "message": error_code.message},
    }
