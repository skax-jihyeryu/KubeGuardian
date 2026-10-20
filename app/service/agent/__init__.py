"""에이전트. <이름>/v<N>/ 폴더로 구현을 나누고 registry에서 고른다."""

from service.agent.investigator.v1 import agent as _investigator_v1  # noqa: F401  레지스트리 등록
