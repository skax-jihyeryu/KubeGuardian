"""조사 상한 (기획서 §6.5). 분기는 프롬프트가 아니라 코드가 강제한다.

도구 호출은 Tool.run이 세므로 LangGraph 구현과 직접 구현 루프가 같은 기준으로 집계된다.
"""

import time
from collections.abc import Callable

from pydantic import BaseModel


class Budget(BaseModel):
    per_issue_tool_calls: int = 8
    total_tool_calls: int = 20
    seconds: float = 150


class BudgetTracker:
    def __init__(self, budget: Budget, clock: Callable[[], float] = time.monotonic):
        self.budget = budget
        self._clock = clock
        self._started = clock()
        self.tool_calls = 0
        self._issue_calls = 0

    def start_issue(self) -> None:
        self._issue_calls = 0

    def count_tool_call(self) -> None:
        self.tool_calls += 1
        self._issue_calls += 1

    def can_call_tool(self) -> bool:
        b = self.budget
        return (
            self._issue_calls < b.per_issue_tool_calls
            and self.tool_calls < b.total_tool_calls
            and not self.time_over()
        )

    def elapsed(self) -> float:
        return self._clock() - self._started

    def time_over(self) -> bool:
        return self.elapsed() >= self.budget.seconds

    def remaining_seconds(self) -> float:
        return self.budget.seconds - self.elapsed()

    def issue_exhausted(self) -> bool:
        """문제당 상한에 닿았다 (전체 상한·시간과 무관)."""
        return self._issue_calls >= self.budget.per_issue_tool_calls

    def total_left(self) -> bool:
        """전체 호출 수와 시간이 남아 있다 (문제당 상한은 보지 않는다)."""
        return self.tool_calls < self.budget.total_tool_calls and not self.time_over()
