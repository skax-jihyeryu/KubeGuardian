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


def test_judge_failure_reason_is_redacted(monkeypatch):
    # invoke_structured가 이미 가리지만, 그 밖의 예외 문구도 결과 파일에 비밀을 남기지 않아야 한다
    import judge

    def boom(*args, **kwargs):
        raise RuntimeError("401 Authorization: Bearer abcdefgh12345678")

    monkeypatch.setattr(judge, "invoke_structured", boom)

    result = judge_top1("결론", EXPECTED, None)

    assert result.match is None
    assert "abcdefgh12345678" not in result.reason


def test_judge_prompt_mentions_same_target_rule():
    from judge import PROMPT

    assert "원인 대상(리소스·설정)이 같으면 같은 원인" in PROMPT
    assert "같은 주입이 만든 서로 다른 오류 문구" in PROMPT
