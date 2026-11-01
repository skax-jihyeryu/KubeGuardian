"""history_qa v1: 저장된 점검·이슈·알림 이력으로 답하는 채팅 에이전트."""

from service.model import Evidence


class ReadOnlyEvidence:
    """RunContext의 evidence 자리에 쓰는 읽기 전용 저장소. 채팅은 근거를 쓰지 않는다."""

    def add(self, evidence: Evidence) -> str:
        raise RuntimeError("history_qa는 근거를 쓰지 않는다")
