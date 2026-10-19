"""한 번의 점검에서 수집한 클러스터 상태 (정규화한 Kubernetes 객체)."""

from typing import Any

from pydantic import AwareDatetime, BaseModel


class ClusterSnapshot(BaseModel):
    namespace: str
    collected_at: AwareDatetime
    # kind → 객체 목록. 수집한 kind는 0개여도 빈 목록으로 넣는다
    resources: dict[str, list[dict[str, Any]]] = {}
    failed: dict[str, str] = {}  # 수집에 실패한 kind → 사유

    def items(self, kind: str) -> list[dict[str, Any]] | None:
        """수집에 실패했거나 수집하지 않은 kind는 None.

        감지기는 None을 "확인하지 못함"으로 판정한다 (05-rules §0). 0개와 구분하기 위해서다.
        """
        if kind in self.failed:
            return None
        return self.resources.get(kind)
