import pytest

from scoring import (
    cause_record,
    classify,
    key_evidence_record,
    resolve_expected,
    resource_matches,
)

ALL = {"key_all_found": True, "key_missing": [], "tool_limited": False}
MISSING = {"key_all_found": False, "key_missing": ["k"], "tool_limited": False}
LIMITED = {"key_all_found": True, "key_missing": [], "tool_limited": True}

DEPLOY_AGENT = {"kind": "Deployment", "name": "agent"}
TARGET_POD = {"expected_cause": [{"kind": "Pod", "name": "${injected_target}"}]}


def test_owner_rule_matches_pod_of_deployment():
    assert resource_matches({"kind": "pod", "name": "agent-7c9f-x2k"}, DEPLOY_AGENT)
    assert not resource_matches({"kind": "Pod", "name": "agentx-1"}, DEPLOY_AGENT)


def test_exact_entry_disables_owner_rule():
    exp = resolve_expected(TARGET_POD, "agent-abc-1")
    want = exp["expected_cause"][0]
    assert want == {"kind": "Pod", "name": "agent-abc-1", "exact": True}
    assert not resource_matches({"kind": "Pod", "name": "agent-abc-2"}, want)


def test_resolve_without_target_raises():
    with pytest.raises(ValueError):
        resolve_expected(TARGET_POD, None)


def test_forbidden_cause_only_checks_resource_items():
    exp = {
        "expected_cause": [{"kind": "Deployment", "name": "llm-gateway"}],
        "not_a_cause": [
            "문장만 있는 항목",
            {"text": "agent 응답 처리", "resource": DEPLOY_AGENT},
        ],
    }
    got = cause_record(DEPLOY_AGENT, exp)
    assert got == {"cause_match": False, "forbidden_cause": True}
    assert cause_record(None, exp) == {"cause_match": False, "forbidden_cause": False}


def test_key_evidence_contains_all_and_obtainable():
    key = [
        {
            "id": "k1",
            "obtainable": True,
            "any_of": [
                {
                    "kind": "log_excerpt",
                    "subject": "*/Pod/llm-gateway-*",
                    "contains": ["429", "quota"],
                }
            ],
        },
        {
            "id": "k2",
            "obtainable": False,
            "any_of": [{"kind": "resource", "subject": "*/NetworkPolicy/*"}],
        },
    ]
    ev = [
        {
            "id": "ev-0001",
            "kind": "log_excerpt",
            "subject": "dev/Pod/llm-gateway-b8-x",
            "data": {"lines": ["HTTP 429 insufficient_QUOTA"]},
        }
    ]
    got = key_evidence_record(key, ev)
    assert got == {
        "key_found": ["k1"],
        "key_missing": [],
        "key_all_found": True,
        "tool_limited": False,
    }
    assert key_evidence_record(key, [])["key_missing"] == ["k1"]
    assert key_evidence_record([key[1]], [])["tool_limited"] is True


def _kw(cause, judge, key, support=None, forbidden=False):
    return dict(
        cause_match=cause,
        judge_match=judge,
        forbidden_cause=forbidden,
        key=key,
        conclusion_support=support,
    )


@pytest.mark.parametrize(
    "kw,expected",
    [
        (_kw(True, True, ALL, "supported"), "pass"),
        (_kw(True, True, ALL, "unsupported"), "F5"),
        (_kw(False, False, LIMITED), "F1"),
        (_kw(False, False, MISSING), "F2"),
        (_kw(False, True, ALL), "F3"),
        (_kw(True, False, ALL), "F4"),
    ],
)
def test_classify_order(kw, expected):
    assert classify(**kw) == expected
