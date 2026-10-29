import judge
from judge import EVIDENCE_CHARS, Claim, ClaimVerdict, extract_claims, judge_claims, support_summary

REPORT = {
    "conclusion": "gateway env가 localhost",
    "evidence_ids": ["ev-0001"],
    "issues": [
        {"rank": 2, "title": "두 번째 문제", "evidence_ids": ["ev-0009"]},
        {"rank": 1, "title": "첫 번째 문제", "evidence_ids": ["ev-0002"]},
    ],
    "hypotheses": [
        {"statement": "A", "status": "inconclusive", "reason": "r0", "evidence_ids": ["ev-0003"]},
        {"statement": "B", "status": "supported", "reason": "r1", "evidence_ids": ["ev-0004"]},
        {"statement": "C", "status": "refuted", "reason": "r2", "evidence_ids": ["ev-0005"]},
    ],
}


def _fake(monkeypatch, behavior=None):
    calls = []

    def fake(llm, schema, messages):
        calls.append(messages)
        if behavior:
            return behavior(len(calls))
        return schema(verdict="supported", reason="ok")

    monkeypatch.setattr(judge, "invoke_structured", fake)
    return calls


def test_extract_claims_picks_conclusion_top_issue_and_decided_hypotheses():
    claims = extract_claims(REPORT)

    assert [(c.source, c.text, c.evidence_ids) for c in claims] == [
        ("conclusion", "gateway env가 localhost", ["ev-0001"]),
        ("issue", "첫 번째 문제", ["ev-0002"]),
        ("hypothesis:1", "B / r1", ["ev-0004"]),
        ("hypothesis:2", "C / r2", ["ev-0005"]),
    ]


def test_claim_without_known_evidence_is_unsupported_without_llm_call(monkeypatch):
    calls = _fake(monkeypatch)

    out = judge_claims([Claim(source="conclusion", text="t", evidence_ids=["ev-0099"])], {}, None)

    assert calls == []
    assert (out[0].verdict, out[0].reason) == ("unsupported", "인용 근거 없음")


def test_evidence_is_truncated_to_4000_chars(monkeypatch):
    calls = _fake(monkeypatch)
    ev = {
        "ev-0001": {
            "id": "ev-0001",
            "kind": "resource",
            "subject": "s",
            "data": {"x": "가" * 10000},
        }
    }

    judge_claims([Claim(source="conclusion", text="t", evidence_ids=["ev-0001"])], ev, None)

    prompt = calls[0][-1][1]
    assert "ev-0001" in prompt
    # 직렬화 `{"x": "` 접두 포함 4000자까지만 들어간다
    assert "가" * (EVIDENCE_CHARS - 20) in prompt
    assert "가" * EVIDENCE_CHARS not in prompt


def test_llm_error_marks_only_that_claim_none(monkeypatch):
    def behavior(n):
        if n == 1:
            raise RuntimeError("boom")
        return judge.ClaimJudgement(verdict="partial", reason="일부")

    _fake(monkeypatch, behavior)
    ev = {"ev-0001": {"id": "ev-0001", "kind": "resource", "subject": "s", "data": {}}}
    claims = [Claim(source=s, text="t", evidence_ids=["ev-0001"]) for s in ("conclusion", "issue")]

    out = judge_claims(claims, ev, None)

    assert [v.verdict for v in out] == [None, "partial"]
    assert "boom" in out[0].reason


def test_support_summary_partial_counts_half():
    vs = [
        ClaimVerdict(source="conclusion", verdict="supported"),
        ClaimVerdict(source="issue", verdict="partial"),
        ClaimVerdict(source="hypothesis:0", verdict="unsupported"),
        ClaimVerdict(source="hypothesis:1", verdict=None),
    ]

    assert support_summary(vs) == {"support_rate": 0.5, "conclusion_support": "supported"}


def test_support_summary_without_judged_claims():
    vs = [ClaimVerdict(source="conclusion", verdict=None)]

    assert support_summary(vs) == {"support_rate": None, "conclusion_support": None}
    assert support_summary([]) == {"support_rate": None, "conclusion_support": None}
