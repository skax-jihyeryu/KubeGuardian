"""도구 테스트 공통 fixture."""

import pytest

from core.guard.budget import Budget, BudgetTracker
from service.tools.base import ToolContext
from tests.service.tools.fakes import NOW, MemoryEvidence


@pytest.fixture
def evidence():
    return MemoryEvidence()


@pytest.fixture
def make_ctx(evidence):
    def make(k8s=None, http=None):
        return ToolContext(
            namespace="dev",
            evidence=evidence,
            budget=BudgetTracker(Budget()),
            k8s=k8s,
            now=lambda: NOW,
            http=http,
        )

    return make
