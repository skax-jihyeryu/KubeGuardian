import json

from judge import JUDGE_MODEL, judge_top1
from tests.service.agent.investigator.test_v1_graph import ScriptedLLM

EXPECTED = {
    "expected_top1": "gateway의 서비스 주소 env가 localhost를 가리켜 모든 API가 실패",
    "root_cause": "gateway의 하위 서비스 주소 설정이 localhost를 가리킨다",
}


def test_judge_parses_match():
    llm = ScriptedLLM(script=[json.dumps({"match": True, "reason": "같은 원인"})], calls=[])

    result = judge_top1("gateway ADMIN_SERVICE_URL이 localhost", EXPECTED, llm)

    assert (result.match, result.reason) == (True, "같은 원인")
    prompt = llm.calls[0][-1].content
    assert EXPECTED["expected_top1"] in prompt and "localhost" in prompt


def test_judge_failure_is_recorded_not_raised():
    llm = ScriptedLLM(script=[RuntimeError("LLM 오류"), RuntimeError("LLM 오류")], calls=[])

    result = judge_top1("결론", EXPECTED, llm)

    assert result.match is None
    assert "LLM" in result.reason


def test_judge_uses_a_different_model_from_investigator():
    from service.agent.investigator.v1.config import SPEC

    assert JUDGE_MODEL not in SPEC.models.values()
