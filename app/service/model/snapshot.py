"""한 번의 점검에서 수집한 클러스터 상태 (정규화한 Kubernetes 객체)."""

from typing import Any

from pydantic import AwareDatetime, BaseModel


class ClusterSnapshot(BaseModel):
    namespace: str
    collected_at: AwareDatetime
    resources: dict[str, list[dict[str, Any]]] = {}  # kind → 객체 목록
    failed: dict[str, str] = {}  # 수집에 실패한 kind → 사유

    def items(self, kind: str) -> list[dict[str, Any]] | None:
        """수집에 실패한 kind는 None. 감지기는 이를 "확인하지 못함"으로 판정한다 (05-rules §0)."""
        if kind in self.failed:
            return None
        return self.resources.get(kind, [])
