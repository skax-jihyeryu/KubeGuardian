from core.guard.budget import Budget, BudgetTracker


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_per_issue_and_total_tool_call_limits():
    tracker = BudgetTracker(Budget(per_issue_tool_calls=2, total_tool_calls=3))
    tracker.start_issue()
    tracker.count_tool_call()
    tracker.count_tool_call()

    assert not tracker.can_call_tool()  # 문제별 상한
    tracker.start_issue()
    assert tracker.can_call_tool()
    tracker.count_tool_call()
    assert not tracker.can_call_tool()  # 전체 상한


def test_deadline():
    clock = Clock()
    tracker = BudgetTracker(Budget(seconds=150), clock=clock)

    clock.t = 149
    assert not tracker.time_over()
    clock.t = 150
    assert tracker.time_over()
