"""도구 구현이 함께 쓰는 조회·제한 도우미."""

from typing import Annotated, Any

from pydantic import Field

from core.error.service_exception import ErrorCode, ServiceException
from service.model import ClusterSnapshot
from service.sensors.collector import collect
from service.tools.base import ToolContext

MAX_ITEMS = 20  # 목록 결과 상한. 넘으면 truncated
MAX_TEXT = 300  # 문자열 필드 하나의 상한

# Kubernetes 객체 이름 (DNS subdomain). `/`·공백이 든 값(경로 등)은 거부한다
K8sName = Annotated[str, Field(pattern=r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")]
# 같음 조건만 쓰는 라벨 셀렉터: `app=gateway,tier=web`
LabelSelector = Annotated[
    str,
    Field(pattern=r"^[A-Za-z0-9._/-]+=[A-Za-z0-9._-]*(,[A-Za-z0-9._/-]+=[A-Za-z0-9._-]*)*$"),
]


def read(ctx: ToolContext, *kinds: str) -> ClusterSnapshot:
    """ctx.k8s(대상 네임스페이스에 묶인 클라이언트)로 필요한 kind만 읽어 정규화·마스킹한다."""
    return collect(ctx.k8s, ctx.now(), kinds)


def require(snapshot: ClusterSnapshot, kind: str) -> list[dict[str, Any]]:
    """kind 목록. 수집에 실패했으면 사유와 함께 COLLECT_FAILED (Tool.run이 실패 근거로 남긴다)."""
    items = snapshot.items(kind)
    if items is None:
        raise ServiceException(ErrorCode.COLLECT_FAILED, f"{kind}: {snapshot.failed.get(kind)}")
    return items


def parse_selector(selector: str) -> dict[str, str]:
    return dict(part.split("=", 1) for part in selector.split(","))


def matches(labels: dict[str, str] | None, selector: dict[str, str]) -> bool:
    labels = labels or {}
    return all(labels.get(k) == v for k, v in selector.items())


def clip(text: Any, limit: int = MAX_TEXT) -> Any:
    if isinstance(text, str) and len(text) > limit:
        return text[:limit] + "…"
    return text
