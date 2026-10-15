"""근거 (기획서 §6.4). 도구·감지기가 수집한 사실 하나. 마스킹한 뒤 저장한다."""

from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel

EvidenceKind = Literal["resource", "event", "log_excerpt", "probe_result", "app_api", "detector"]


class Evidence(BaseModel):
    id: str | None = None  # 저장소가 발급한다 (ev-0042)
    kind: EvidenceKind
    subject: str  # dev/Pod/agent-7c9f-x2k
    collected_at: AwareDatetime
    source: str  # GET /api/v1/namespaces/dev/pods/agent-7c9f-x2k
    data: dict[str, Any]
