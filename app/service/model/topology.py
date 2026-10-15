"""서비스 호출 관계 (기획서 §7.1)."""

from typing import Literal

from pydantic import BaseModel

EdgeStatus = Literal["declared", "unresolved", "inferred"]


class TopologyEdge(BaseModel):
    source: str  # 호출하는 서비스
    target: str  # env 값의 호스트 (unresolved면 실제 Service가 없음)
    port: int | None = None
    env_name: str | None = None  # 주소를 읽은 env (예: AGENT_SERVICE_URL)
    status: EdgeStatus


class Topology(BaseModel):
    services: list[str] = []
    edges: list[TopologyEdge] = []
    unchecked: list[
        str
    ] = []  # 주소를 읽지 못한 호출 서비스 (envFrom ConfigMap 수집 실패) → 확인하지 못함
