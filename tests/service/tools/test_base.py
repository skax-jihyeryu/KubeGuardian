import json
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel

from core.error.service_exception import ErrorCode, ServiceException
from service.agent._common.tool_adapter import to_langchain_tool
from service.model import Evidence
from service.tools.base import Tool, ToolContext, ToolObservation

NOW = datetime(2026, 10, 20, 1, 0, tzinfo=UTC)


class MemoryEvidence:
    def __init__(self):
        self.saved: list[Evidence] = []

    def add(self, evidence: Evidence) -> str:
        self.saved.append(evidence)
        return f"ev-{len(self.saved):04d}"


class GetEnv(Tool):
    name = "get_env"
    description = "Pod의 env를 본다. 서비스 주소 설정을 확인할 때 쓰세요."

    class Input(BaseModel):
        pod: str
        namespace: str = "dev"

    def collect(self, args: Input, ctx: ToolContext) -> ToolObservation:
        return ToolObservation(
            kind="resource",
            subject=f"{args.namespace}/Pod/{args.pod}",
            source=f"GET /api/v1/namespaces/{args.namespace}/pods/{args.pod}",
            summary="DB_PASSWORD=hunter2 포함 env 2개",
            data={
                "env": [
                    {"name": "DB_PASSWORD", "value": "hunter2"},
                    {"name": "MODE", "value": "dev"},
                ]
            },
        )


@pytest.fixture
def ctx():
    return ToolContext(namespace="dev", evidence=MemoryEvidence(), now=lambda: NOW)


def test_tool_result_is_redacted_and_stored_as_evidence(ctx):
    result = GetEnv().run({"pod": "gateway-1"}, ctx)

    assert result.evidence_id == "ev-0001"
    assert "hunter2" not in result.model_dump_json()
    assert "hunter2" not in ctx.evidence.saved[0].model_dump_json()
    assert ctx.evidence.saved[0].collected_at == NOW


def test_tool_rejects_subject_outside_target_namespace(ctx):
    with pytest.raises(ServiceException) as exc:
        GetEnv().run({"pod": "coredns", "namespace": "kube-system"}, ctx)

    assert exc.value.error_code is ErrorCode.TOOL_SCOPE_VIOLATION
    assert ctx.evidence.saved == []


def test_tool_rejects_invalid_arguments(ctx):
    with pytest.raises(ServiceException) as exc:
        GetEnv().run({}, ctx)

    assert exc.value.error_code is ErrorCode.VALIDATION_ERROR


def test_langchain_adapter_wraps_output_as_data(ctx):
    lc_tool = to_langchain_tool(GetEnv(), ctx)

    out = lc_tool.invoke({"pod": "gateway-1"})

    assert lc_tool.name == "get_env"
    assert lc_tool.description.startswith("Pod의 env")
    assert out.startswith("<tool_output>") and out.endswith("</tool_output>")
    assert (
        json.loads(out.removeprefix("<tool_output>").removesuffix("</tool_output>"))["evidence_id"]
        == "ev-0001"
    )
