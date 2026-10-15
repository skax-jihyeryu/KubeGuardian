"""LLM 사용량 집계 (평가 KPI 조사 비용, 화면 P5)."""

from langchain_core.messages import AIMessage


class UsageTracker:
    def __init__(self):
        self.llm_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.by_model: dict[str, dict[str, int]] = {}

    def add(self, reply: AIMessage) -> None:
        self.llm_calls += 1
        meta = reply.usage_metadata or {}
        model = (reply.response_metadata or {}).get("model_name", "unknown")
        tokens = self.by_model.setdefault(model, {"input": 0, "output": 0})
        for key, attr in (("input_tokens", "input"), ("output_tokens", "output")):
            n = int(meta.get(key, 0))
            setattr(self, key, getattr(self, key) + n)
            tokens[attr] += n
