"""에이전트 레지스트리. "<이름>:<버전>" 키로 구현을 고른다 (평가·비교 실험도 같은 키를 쓴다)."""

from service.agent.base import BaseAgent

_AGENTS: dict[str, type[BaseAgent]] = {}


def register(cls: type[BaseAgent]) -> type[BaseAgent]:
    key = cls.spec.key
    if key in _AGENTS:
        raise ValueError(f"이미 등록된 에이전트: {key}")
    _AGENTS[key] = cls
    return cls


def unregister(key: str) -> None:
    _AGENTS.pop(key, None)


def get(key: str) -> type[BaseAgent]:
    if key not in _AGENTS:
        raise KeyError(f"등록되지 않은 에이전트: {key}")
    return _AGENTS[key]


def list_agents() -> list[str]:
    return sorted(_AGENTS)
